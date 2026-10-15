"""The real server process, as `just serve` starts it, answered over HTTP on localhost."""

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

CONTRACT = Path(__file__).parent.parent / "contract"
POLISH = json.loads((CONTRACT / "polish.request.json").read_text())
COMMAND = json.loads((CONTRACT / "command.request.json").read_text())
AUTH = {"Authorization": "Bearer dev"}
JUSTFILE = Path(__file__).parents[2] / "justfile"
SERVE = next(line.split() for line in JUSTFILE.read_text().splitlines() if "uvicorn" in line)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    port = free_port()
    env = {**os.environ, "SNAPBAK_TOKEN": "dev", "SNAPBAK_SMALL": "fake:edit", "SNAPBAK_BIG": "fake:edit"}
    env.pop("SNAPBAK_FALLBACK", None)
    command = [sys.executable, "-m", *SERVE[2:]]
    command[command.index("${PORT:-8787}")] = str(port)
    process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                httpx.get(f"{base}/healthz")
                break
            except httpx.ConnectError:
                time.sleep(0.05)
        yield base
    finally:
        process.terminate()
        output, _ = process.communicate(timeout=5)
        for word in ["thursday", "friday", "deck", "eod"]:
            assert word not in output.decode().lower()


def test_the_server_keeps_a_connection_open_longer_than_any_hold() -> None:
    keep_alive = int(SERVE[SERVE.index("--timeout-keep-alive") + 1])
    assert keep_alive >= 60


def test_healthz(server: str) -> None:
    response = httpx.get(f"{server}/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


def test_polish_over_http(server: str) -> None:
    response = httpx.post(f"{server}/v1/polish", json=POLISH, headers=AUTH)
    body = response.json()
    assert response.status_code == 200
    # The fake echoes the piece: it keeps "no wait" with no revision, a kept correction, so the text goes in as said.
    assert (body["text"], body["revision"], body["rejected"]) == ("no wait make it thursday not friday", None, True)
    assert (body["engine"], body["tier"]) == ("fake:edit", "big")


def test_command_over_http(server: str) -> None:
    response = httpx.post(f"{server}/v1/command", json=COMMAND, headers=AUTH)
    assert (response.status_code, response.json()["text"]) == (200, "HEY CAN U SEND THE DECK BY EOD")


def test_wrong_token_over_http(server: str) -> None:
    response = httpx.post(f"{server}/v1/polish", json=POLISH, headers={"Authorization": "Bearer nope"})
    assert (response.status_code, response.json()) == (401, {"error": "unauthorized"})


def test_invalid_request_over_http(server: str) -> None:
    response = httpx.post(f"{server}/v1/polish", json={**POLISH, "deadline_ms": 1}, headers=AUTH)
    assert (response.status_code, response.json()) == (422, {"error": "invalid"})
