"""The bucket's lifecycle rules (13 HLD, "Bucket rules"; 12 HLD, "Deployment"): voiceprint clips (`clips/`) expire after
a day, and `audio/` never expires (Q15); the share objects a re-export superseded, tagged `superseded=true`, expire
after a day; multipart uploads nobody completed are aborted after two. Putting a configuration replaces it, so running
this again changes nothing.

`just bucket-rules` applies them to S3_BUCKET in S3_REGION, once (SETUP.md).
"""

import os
from typing import Any

import boto3

from snapbak_backend.services.storage import SUPERSEDED

ABORT_UPLOADS: dict[str, Any] = {
    "ID": "abort-incomplete-mpu",
    "Status": "Enabled",
    "Filter": {"Prefix": ""},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2},
}
EXPIRE_CLIPS: dict[str, Any] = {
    "ID": "expire-clips",
    "Status": "Enabled",
    "Filter": {"Prefix": "clips/"},
    "Expiration": {"Days": 1},
}
EXPIRE_SUPERSEDED: dict[str, Any] = {
    "ID": "expire-superseded",
    "Status": "Enabled",
    "Filter": {"Tag": SUPERSEDED},
    "Expiration": {"Days": 1},
}
RULES = [ABORT_UPLOADS, EXPIRE_CLIPS, EXPIRE_SUPERSEDED]


def apply_rules(client: Any, bucket: str, rules: list[dict[str, Any]]) -> None:
    client.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": rules})


if __name__ == "__main__":
    s3 = boto3.client(
        "s3",
        region_name=os.environ["S3_REGION"],
        aws_access_key_id=os.environ["SNAPBAK_AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["SNAPBAK_AWS_SECRET_ACCESS_KEY"],
    )
    apply_rules(s3, os.environ["S3_BUCKET"], RULES)
    print(f"lifecycle rules set on {os.environ['S3_BUCKET']}")
