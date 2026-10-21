"""The cloud transcript over HTTP against the real processes: the server as `just serve` starts it and the worker as
`just worker` starts it, on the compose stack with the fake strategy."""

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from conftest import RECORDING

JUSTFILE = Path(__file__).parents[2] / "justfile"
LINES = Path(JUSTFILE).read_text().splitlines()
SERVE = next(line.split() for line in LINES if "uvicorn" in line)
WORKER = next(line.split() for line in LINES if "arq snapbak_backend" in line)
SECRETS = ("priya", "arjun", "dashboard", "joining", "recording 2026")


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def running(output: list[str]) -> Iterator[str]:
    """The server and the worker as `just serve` and `just worker` start them; their base URL, then everything they
    printed, appended to `output` once they stop."""
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "SNAPBAK_PUBLIC_URL": base, "SNAPBAK_SHARE_BASE_URL": base}
    serve = [sys.executable, "-m", *SERVE[2:]]
    serve[serve.index("${PORT:-8787}")] = str(port)
    worker = [sys.executable, "-m", *WORKER[2:]]
    started = [
        subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for command in (serve, worker)
    ]
    try:
        for _ in range(200):
            try:
                httpx.get(f"{base}/healthz")
                break
            except httpx.ConnectError:
                time.sleep(0.05)
        yield base
    finally:
        for process in started:
            process.terminate()
            out, _ = process.communicate(timeout=10)
            output.append(out.decode())


@pytest.fixture
def processes(cloud: dict[str, str]) -> Iterator[str]:
    output: list[str] = []
    with running(output) as base:
        yield base
    logged = "".join(output).lower()
    assert "task=merge_and_store" in logged
    for secret in SECRETS:
        assert secret not in logged, secret


def test_trx01_generate_to_transcript_over_http_with_the_real_server_and_worker(processes: str) -> None:
    base = processes
    token = httpx.post(f"{base}/v1/devices", json={"installId": RECORDING, "appVersion": "1.0.0"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    recording = {
        "title": "Recording 2026-10-17 at 10.02.11",
        "durationMs": 120_000,
        "createdAt": "2026-10-17T04:32:11Z",
    }
    assert httpx.put(f"{base}/v1/recordings/{RECORDING}", json=recording, headers=auth).status_code == 201
    job = httpx.post(
        f"{base}/v1/recordings/{RECORDING}/transcription", json={"language": "auto", "script": "native"}, headers=auth
    ).json()
    assert job["needsUpload"] is True
    data = b"m4a" * 1000
    upload = httpx.post(
        f"{base}/v1/recordings/{RECORDING}/uploads",
        json={"kind": "audio", "bytes": len(data), "contentType": "audio/mp4"},
        headers=auth,
    ).json()
    etag = httpx.put(upload["urls"][0]["url"], content=data).headers["etag"]
    done = httpx.post(
        f"{base}/v1/uploads/{upload['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth
    ).json()
    assert done["jobId"] == job["jobId"]
    since, state = 0, ""
    deadline = time.monotonic() + 20
    while state not in ("ready", "failed") and time.monotonic() < deadline:
        answer = httpx.get(f"{base}/v1/jobs/{job['jobId']}?wait=25&since={since}", headers=auth, timeout=35).json()
        since, state = answer["version"], answer["state"]
    assert state == "ready"
    transcript = httpx.get(f"{base}/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    assert [line["text"] for line in transcript["lines"]] == [
        "Hi Arjun, um, thanks for joining.",
        "Thanks Priya.",
        "Uh, shall we start with the dashboard?",
        "Yes.",
        "So here you can see the numbers.",
    ]
    assert [speaker["name"] for speaker in transcript["speakers"]] == ["Priya", "Arjun"]
    again = httpx.post(
        f"{base}/v1/recordings/{RECORDING}/transcription", json={"language": "auto", "script": "native"}, headers=auth
    )
    assert (again.status_code, again.json()["job"]["state"]) == (409, "ready")
    assert httpx.delete(f"{base}/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    assert httpx.get(f"{base}/v1/recordings/{RECORDING}/transcript", headers=auth).status_code == 404


def test_shr01_share_then_watch_over_http_with_the_real_server(cloud: dict[str, str]) -> None:
    output: list[str] = []
    title = "Quarterly demo for Priya"
    with running(output) as base:
        token = httpx.post(f"{base}/v1/devices", json={"installId": RECORDING, "appVersion": "1.0.0"}).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        recording = {"title": title, "durationMs": 61_200, "createdAt": "2026-10-17T04:32:11Z"}
        httpx.put(f"{base}/v1/recordings/{RECORDING}", json=recording, headers=auth)
        made = httpx.post(
            f"{base}/v1/recordings/{RECORDING}/shares",
            json={"title": title, "bytes": 3000},
            headers=auth,
        ).json()
        poster = httpx.put(made["posterUploadUrl"], content=b"jpeg", headers={"Content-Type": "image/jpeg"})
        assert poster.status_code == 200
        data = b"mp4" * 1000
        size = {"durationMs": 61_200, "width": 1920, "height": 1080}
        body = {"kind": "share", "bytes": len(data), "contentType": "video/mp4", "shareId": made["shareId"], **size}
        upload = httpx.post(f"{base}/v1/recordings/{RECORDING}/uploads", json=body, headers=auth).json()
        etag = httpx.put(upload["urls"][0]["url"], content=data).headers["etag"]
        done = httpx.post(
            f"{base}/v1/uploads/{upload['uploadId']}/complete", json={"parts": [{"n": 1, "etag": etag}]}, headers=auth
        ).json()
        assert (done["share"]["state"], done["share"]["url"]) == ("live", f"{base}/v/{made['slug']}")
        page = httpx.get(done["share"]["url"])
        assert page.status_code == 200
        assert f'<meta property="og:video" content="{base}/v/{made["slug"]}/video.mp4">' in page.text
        assert httpx.get(f"{base}/v/{made['slug']}/video.mp4", follow_redirects=True).content == data
        first_bytes = sorted(httpx.get(done["share"]["url"]).elapsed.total_seconds() * 1000 for _ in range(30))
        print(f"share page over HTTP: median {first_bytes[15]:.1f} ms, p95 {first_bytes[28]:.1f} ms")
        assert first_bytes[15] < 200
        httpx.patch(f"{base}/v1/shares/{made['slug']}", json={"state": "disabled"}, headers=auth)
        assert httpx.get(done["share"]["url"]).status_code == 410
    logged = "".join(output)
    assert "route=/v/{slug} provider=- model=- tier=- ms=" in logged
    for secret in (made["slug"], title, "GET /v"):
        assert secret not in logged, secret
