"""A GIF share from the app's side (GIFS-AND-REPLAY-60, G7/G8): its own recording of type `gif`, the upload as-is
with a 50 MB cap, the same slug lifecycle as a video's, and the object S3 keeps."""

from typing import Any

from conftest import Sql, create_share, failure, gif_live, put_recording, register, s3, upload_gif


def test_g7_a_gif_shares_as_its_own_recording_of_type_gif(api: Any, sql: Sql) -> None:
    auth = register(api)
    made = gif_live(api, auth, b"GIF89a loop")
    assert made["posterUploadUrl"] is None, "a GIF page animates the GIF itself: no poster to offer"
    body = made
    assert body["type"] == "gif"
    assert body["state"] == "uploading", "live comes when the upload completes"
    assert sql("SELECT recording_type, duration_ms FROM recordings") == [{"recording_type": "gif", "duration_ms": 0}]
    share = sql("SELECT s3_key, state, version, width, height, duration_ms, poster_s3_key FROM shares")[0]
    assert share["state"] == "live"
    assert share["version"] == 1
    assert (share["width"], share["height"]) == (600, 386)
    assert share["duration_ms"] is None
    assert share["poster_s3_key"] is None
    assert share["s3_key"].startswith(f"shares/{made['shareId']}/v1/animation.gif")


def test_g7_the_gif_object_sits_in_s3_as_image_gif_and_immutable(api: Any, sql: Sql) -> None:
    auth = register(api)
    gif_live(api, auth, b"GIF89a loop")
    share = sql("SELECT id, s3_key FROM shares")[0]
    object = s3().head_object(Bucket=api.app.state.cloud.config.bucket, Key=share["s3_key"])
    assert (object["ContentType"], object["CacheControl"]) == (
        "image/gif",
        "public, max-age=31536000, immutable",
    )
    assert sql("SELECT kind FROM uploads") == [{"kind": "gif"}]


def test_g7_resharing_the_gif_keeps_the_slug_and_takes_the_next_version(api: Any, sql: Sql) -> None:
    auth = register(api)
    recording = "9c2d4c52-0f0e-4d55-9a44-2f1a7e6c3b11"
    made = gif_live(api, auth, b"GIF89a take 1", recording=recording)
    again = create_share(api, auth, recording=recording, bytes=12)
    assert again.status_code == 200
    assert again.json()["slug"] == made["slug"]
    upload_gif(api, auth, made["shareId"], b"GIF89a take 2", recording=recording)
    share = sql("SELECT version, state FROM shares")[0]
    assert (share["version"], share["state"]) == (2, "live")


def test_g7_a_gif_past_the_cap_is_refused_at_the_share_and_the_upload(api: Any) -> None:
    auth = register(api)
    recording = "9c2d4c52-0f0e-4d55-9a44-2f1a7e6c3b12"
    put_recording(api, auth, recording=recording, duration_ms=0, recording_type="gif")
    made = create_share(api, auth, recording=recording, bytes=50_000_001)
    assert failure(made) == (413, "gif_too_large")
    shared = create_share(api, auth, recording=recording, bytes=50_000_000).json()
    body = {
        "kind": "gif",
        "bytes": 50_000_001,
        "contentType": "image/gif",
        "shareId": shared["shareId"],
        "width": 600,
        "height": 386,
    }
    over = api.post(f"/v1/recordings/{recording}/uploads", json=body, headers=auth)
    assert failure(over) == (413, "gif_too_large")


def test_g7_a_gif_upload_of_the_wrong_type_or_a_stale_share_fails(api: Any) -> None:
    auth = register(api)
    recording = "9c2d4c52-0f0e-4d55-9a44-2f1a7e6c3b13"
    put_recording(api, auth, recording=recording, duration_ms=0, recording_type="gif")
    made = create_share(api, auth, recording=recording, bytes=100).json()
    body = {
        "kind": "gif",
        "bytes": 100,
        "contentType": "video/mp4",
        "shareId": made["shareId"],
        "width": 600,
        "height": 386,
    }
    wrong = api.post(f"/v1/recordings/{recording}/uploads", json=body, headers=auth)
    assert wrong.status_code == 422
    body = {
        "kind": "gif",
        "bytes": 100,
        "contentType": "image/gif",
        "shareId": "e2b94f1a-3c5d-4e6f-8a7b-9c0d1e2f3a4b",
        "width": 600,
        "height": 386,
    }
    stranger = api.post(f"/v1/recordings/{recording}/uploads", json=body, headers=auth)
    assert stranger.status_code == 404
