import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from conftest import MIB, RECORDING, S3_HTTP, Sql, failure, put_recording, register, s3, upload_audio

AUDIO = {"kind": "audio", "contentType": "audio/mp4"}


def start(api: Any, auth: dict[str, str], size: int) -> Any:
    return api.post(f"/v1/recordings/{RECORDING}/uploads", json={**AUDIO, "bytes": size}, headers=auth)


def test_cld02_start_plans_8_mib_parts_with_presigned_urls_for_an_hour(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    response = start(api, auth, 20 * MIB)
    body = response.json()
    assert response.status_code == 201
    assert body["partSize"] == 8_388_608
    assert [item["n"] for item in body["urls"]] == [1, 2, 3]
    assert all("partNumber=" in item["url"] and "uploadId=" in item["url"] for item in body["urls"])
    expires = datetime.fromisoformat(body["expiresAt"])
    assert abs(expires - (datetime.now(UTC) + timedelta(hours=1))) < timedelta(seconds=5)
    row = sql("SELECT kind, s3_key, state, bytes, content_type FROM uploads")
    assert row == [
        {
            "kind": "audio",
            "s3_key": f"audio/{RECORDING}/{body['uploadId']}.m4a",
            "state": "uploading",
            "bytes": 20 * MIB,
            "content_type": "audio/mp4",
        }
    ]


def test_cld02_upload_puts_and_completes_in_part_order_then_a_retried_complete_is_a_no_op(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    data = bytes(range(256)) * (17 * MIB // 256 + 1)
    upload_id, done = upload_audio(api, auth, data)
    assert (done.status_code, done.json()) == (200, {"uploadId": upload_id, "state": "complete", "jobId": None})
    key = f"audio/{RECORDING}/{upload_id}.m4a"
    stored = s3().get_object(Bucket=_bucket(api), Key=key)["Body"].read()
    assert stored == data
    parts = sql("SELECT parts, state, completed_at IS NOT NULL AS done FROM uploads")[0]
    assert [part["n"] for part in _json(parts["parts"])] == [1, 2, 3]
    assert (parts["state"], parts["done"]) == ("complete", True)
    again = api.post(f"/v1/uploads/{upload_id}/complete", json={"parts": [{"n": 1, "etag": "x"}]}, headers=auth)
    assert (again.status_code, again.json()) == (200, done.json())


def test_cld02_a_lost_complete_answer_is_found_by_the_object_size(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    upload_id, _ = upload_audio(api, auth, b"m4a" * 100)
    sql("UPDATE uploads SET state = 'uploading', completed_at = NULL")
    again = api.post(f"/v1/uploads/{upload_id}/complete", json={"parts": [{"n": 1, "etag": '"x"'}]}, headers=auth)
    assert (again.status_code, again.json()["state"]) == (200, "complete")


def test_cld02_a_size_mismatch_is_refused_and_aborts_the_upload(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = start(api, auth, 10).json()
    etag = S3_HTTP.put(body["urls"][0]["url"], content=b"eleven byte").headers["etag"]
    done = api.post(f"/v1/uploads/{body['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth)
    assert (done.status_code, done.json()["error"]["code"]) == (422, "upload_size_mismatch")
    assert sql("SELECT state FROM uploads") == [{"state": "aborted"}]
    parts = api.post(f"/v1/uploads/{body['uploadId']}/parts", json={"parts": [1]}, headers=auth)
    complete = api.post(
        f"/v1/uploads/{body['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth
    )
    assert (parts.status_code, parts.json()["error"]["code"]) == (409, "upload_state")
    assert (complete.status_code, complete.json()["error"]["code"]) == (409, "upload_state")


def test_cld02_parts_gives_fresh_urls_only_for_the_parts_asked(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = start(api, auth, 20 * MIB).json()
    fresh = api.post(f"/v1/uploads/{body['uploadId']}/parts", json={"parts": [2, 3]}, headers=auth)
    outside = api.post(f"/v1/uploads/{body['uploadId']}/parts", json={"parts": [4]}, headers=auth)
    assert fresh.status_code == 200
    assert (fresh.json()["uploadId"], [item["n"] for item in fresh.json()["urls"]]) == (body["uploadId"], [2, 3])
    assert (outside.status_code, outside.json()["error"]["code"]) == (422, "invalid")


def test_cld03_audio_over_200_mb_is_refused_before_s3_is_asked(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    response = start(api, auth, 200_000_001)
    assert (response.status_code, response.json()["error"]["code"]) == (413, "upload_too_large")
    assert sql("SELECT count(*) AS n FROM uploads") == [{"n": 0}]


def test_cld02_uploads_of_unknown_or_other_devices_are_not_found(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = start(api, auth, 10).json()
    other = register(api, str(uuid.uuid4()))
    unknown = api.post(f"/v1/recordings/{uuid.uuid4()}/uploads", json={**AUDIO, "bytes": 10}, headers=auth)
    theirs = api.post(f"/v1/uploads/{body['uploadId']}/parts", json={"parts": [1]}, headers=other)
    assert (failure(unknown), failure(theirs)) == ((404, "not_found"), (404, "not_found"))


def _bucket(api: Any) -> str:
    return api.app.state.cloud.config.bucket


def _json(value: Any) -> Any:
    decoded = json.loads(value) if isinstance(value, str) else value
    return decoded


def test_cld02_a_wrong_etag_is_invalid_and_a_corrected_complete_still_succeeds(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = start(api, auth, 10).json()
    etag = S3_HTTP.put(body["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    complete = f"/v1/uploads/{body['uploadId']}/complete"
    wrong = api.post(complete, json={"parts": [{"n": 1, "etag": '"0000"'}]}, headers=auth)
    assert (wrong.status_code, wrong.json()["error"]["code"]) == (422, "invalid")
    assert sql("SELECT state FROM uploads") == [{"state": "uploading"}]
    right = api.post(complete, json={"parts": [{"n": 1, "etag": etag}]}, headers=auth)
    assert (right.status_code, right.json()["state"]) == (200, "complete")


def test_cld02_another_device_cannot_start_or_complete_uploads_of_a_recording_it_does_not_own(
    api: Any, sql: Sql
) -> None:
    auth = register(api)
    put_recording(api, auth)
    body = start(api, auth, 10).json()
    etag = S3_HTTP.put(body["urls"][0]["url"], content=b"ten bytes!").headers["etag"]
    other = register(api, str(uuid.uuid4()))
    started = api.post(f"/v1/recordings/{RECORDING}/uploads", json={**AUDIO, "bytes": 10}, headers=other)
    completed = api.post(
        f"/v1/uploads/{body['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=other
    )
    assert (failure(started), failure(completed)) == ((404, "not_found"), (404, "not_found"))
    assert sql("SELECT state, completed_at FROM uploads") == [{"state": "uploading", "completed_at": None}]
