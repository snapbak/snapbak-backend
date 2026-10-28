"""The bucket through boto3 (docs/cloud.md). Calls that reach S3 run in a thread; presigning is
local."""

import asyncio
import json
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from snapbak_backend.config.settings import CloudConfig


class NoSuchUpload(Exception):
    """S3 no longer knows the multipart upload: completed earlier, or aborted."""


class BadParts(Exception):
    """The part list doesn't match what S3 holds: a wrong ETag, a missing part, or a part too small."""


# The tag a re-export puts on the share objects it replaced; the shares bucket's rule expires it.
SUPERSEDED = {"Key": "superseded", "Value": "true"}
# S3's answers to a part list that doesn't match the parts uploaded.
BAD_PARTS = ("InvalidPart", "InvalidPartOrder", "EntityTooSmall")
# S3's answers for a key with no object.
MISSING = ("404", "NoSuchKey", "NotFound")


def s3_client(config: CloudConfig) -> Any:
    """One client per process: making one costs tens of milliseconds and some memory."""
    boto = BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"})
    client = boto3.client(
        "s3",
        region_name=config.s3_region,
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
        config=boto,
    )
    return client


class Storage:
    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    async def create_multipart(self, key: str, content_type: str, cache_control: str | None = None) -> str:
        """`cache_control` becomes the object's own header, as for a share video, whose key never changes content."""
        cached = {} if cache_control is None else {"CacheControl": cache_control}
        answer = await asyncio.to_thread(
            self.client.create_multipart_upload, Bucket=self.bucket, Key=key, ContentType=content_type, **cached
        )
        return answer["UploadId"]

    async def abort(self, key: str, upload_id: str) -> None:
        """Aborts the multipart upload; one S3 no longer knows is already gone."""
        try:
            await asyncio.to_thread(self.client.abort_multipart_upload, Bucket=self.bucket, Key=key, UploadId=upload_id)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise

    def presign_part(self, key: str, upload_id: str, n: int, expires_s: int) -> str:
        params = {"Bucket": self.bucket, "Key": key, "UploadId": upload_id, "PartNumber": n}
        url = self.client.generate_presigned_url("upload_part", Params=params, ExpiresIn=expires_s)
        return url

    def presign_put(self, key: str, content_type: str, expires_s: int) -> str:
        """A single PUT; the client must send this `Content-Type`."""
        params = {"Bucket": self.bucket, "Key": key, "ContentType": content_type}
        url = self.client.generate_presigned_url("put_object", Params=params, ExpiresIn=expires_s)
        return url

    def presign_get(self, key: str, expires_s: int, disposition: str | None = None) -> str:
        """`disposition` becomes the answer's `Content-Disposition`, such as a download's `attachment`."""
        params = {"Bucket": self.bucket, "Key": key}
        if disposition is not None:
            params["ResponseContentDisposition"] = disposition
        url = self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_s)
        return url

    async def keys(self, prefix: str) -> list[str]:
        """Every key under `prefix`; a share holds a handful, so one page of 1,000 is all of them."""
        listed = await asyncio.to_thread(self.client.list_objects_v2, Bucket=self.bucket, Prefix=prefix)
        names = [item["Key"] for item in listed.get("Contents", [])]
        return names

    async def supersede(self, keys: list[str]) -> None:
        """Tags each object `superseded=true`, which the bucket's rule deletes a day later (12 HLD)."""
        await asyncio.gather(*[self.tag_superseded(key) for key in keys])

    async def tag_superseded(self, key: str) -> None:
        """A key with no object (an upload aborted before it completed) has nothing to delete, so it is skipped."""
        tags = {"TagSet": [SUPERSEDED]}
        try:
            await asyncio.to_thread(self.client.put_object_tagging, Bucket=self.bucket, Key=key, Tagging=tags)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in MISSING:
                raise

    async def complete(self, key: str, upload_id: str, parts: list[tuple[int, str]]) -> None:
        """Completes with the parts in ascending order, as S3 needs."""
        listed = [{"PartNumber": n, "ETag": etag} for n, etag in sorted(parts)]
        try:
            await asyncio.to_thread(
                self.client.complete_multipart_upload,
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": listed},
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "NoSuchUpload":
                raise NoSuchUpload(key) from error
            if code in BAD_PARTS:
                raise BadParts(code) from error
            raise

    async def size(self, key: str) -> int | None:
        """The object's size, or None when there is no object."""
        try:
            head = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in MISSING:
                return None
            raise
        return head["ContentLength"]

    async def put_json(self, key: str, data: Any) -> None:
        body = json.dumps(data).encode()
        await asyncio.to_thread(
            self.client.put_object, Bucket=self.bucket, Key=key, Body=body, ContentType="application/json"
        )

    async def get_json(self, key: str) -> Any | None:
        """The JSON object at `key`, or None when there is none."""
        try:
            answer = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise
        body = await asyncio.to_thread(answer["Body"].read)
        return json.loads(body)

    async def download(self, key: str, path: str) -> None:
        await asyncio.to_thread(self.client.download_file, self.bucket, key, path)

    async def upload(self, path: str, key: str, content_type: str) -> None:
        extra = {"ContentType": content_type}
        await asyncio.to_thread(self.client.upload_file, path, self.bucket, key, ExtraArgs=extra)
