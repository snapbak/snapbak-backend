"""Every failure code the server raises has its message (api/errors.py): a code without one answers 500 `internal`
instead of its own status."""

import re
from pathlib import Path

from snapbak_backend.api.errors import MESSAGES

SOURCE = Path(__file__).parents[2] / "src" / "snapbak_backend"
RAISED = re.compile(r'CloudFailure\(\s*\d+,\s*"([a-z_]+)"')


def test_every_code_the_server_raises_has_a_message() -> None:
    raised = {code for path in SOURCE.rglob("*.py") for code in RAISED.findall(path.read_text())}
    missing = sorted(raised - MESSAGES.keys())
    assert raised and missing == []
