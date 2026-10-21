"""The dictation-only server (no DATABASE_URL) loads none of the cloud's libraries, so its start time and memory
stay where they were before cloud transcripts (CLAUDE.md rule 5)."""

import subprocess
import sys

PROGRAM = """
import os, sys
os.environ.update({"SNAPBAK_TOKEN": "dev", "SNAPBAK_POLISH": "fake:echo", "SNAPBAK_BIG": "fake:echo"})
os.environ.pop("DATABASE_URL", None)
from fastapi.testclient import TestClient
from snapbak_backend import main
with TestClient(main.app) as client:
    assert client.get("/healthz").status_code == 200
    assert client.post("/v1/devices", json={}).status_code == 404
cloud = ("sqlalchemy", "boto3", "arq", "redis", "asyncpg", "cryptography")
print(",".join(name for name in cloud if name in sys.modules))
"""


def test_cld01_the_dictation_only_server_imports_no_cloud_library() -> None:
    env = {"PATH": "/usr/bin:/bin"}
    loaded = subprocess.run([sys.executable, "-c", PROGRAM], capture_output=True, text=True, env=env, check=True)
    assert loaded.stdout.strip() == ""
