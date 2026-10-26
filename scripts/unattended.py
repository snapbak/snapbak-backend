"""D39's unattended success, read from the worker's own log: `just unattended < worker.log` (or file paths).

A meeting counts once, on the UTC day of its first minutes verdict: a success when that first verdict is
`ready:unattended` (minutes no click asked for), a miss when it is `failed:<code>` (whatever came after it, such as a
regenerate) or `ready:clicked`. It prints each day's rate and the overall one, and exits 1 under 99 %. The lines carry
IDs and verdicts only (CLAUDE.md rule 3), so the input holds no word of any meeting.
"""

import re
import sys
from collections.abc import Iterable
from pathlib import Path

TARGET = 99.0
VERDICT = re.compile(r"^(\d{4}-\d{2}-\d{2}) .*task=generate_minutes job=(\S+) .*verdict=(ready:\w+|failed:\w+)")


def rates(lines: Iterable[str]) -> dict[str, tuple[int, int]]:
    """Per UTC day, the meetings whose first verdict was unattended success, and all the meetings."""
    first: dict[str, tuple[str, bool]] = {}
    for line in lines:
        found = VERDICT.match(line)
        if found is None or found.group(2) in first:
            continue
        day, meeting, verdict = found.groups()
        # A run ready with a section missing (`ready:unattended:incomplete:N`) needed more: a miss.
        first[meeting] = (day, verdict == "ready:unattended" and ":incomplete" not in line)
    days: dict[str, tuple[int, int]] = {}
    for day, success in first.values():
        wins, total = days.get(day, (0, 0))
        days[day] = (wins + success, total + 1)
    return days


def read(paths: list[str]) -> list[str]:
    """The log lines of the files named, or of stdin."""
    if not paths:
        return sys.stdin.readlines()
    return [line for path in paths for line in Path(path).read_text(encoding="utf-8").splitlines()]


def main(paths: list[str], lines: Iterable[str] | None = None) -> int:
    days = rates(read(paths) if lines is None else lines)
    wins = sum(win for win, _ in days.values())
    total = sum(count for _, count in days.values())
    for day, (win, count) in sorted(days.items()):
        print(f"{day}: {win} of {count} unattended, {100 * win / count:.1f} %")
    # Nothing counted is no evidence: a log in another format must fail the gate, never pass it.
    overall = 100 * wins / total if total else 0.0
    print(f"overall: {wins} of {total}, {overall:.1f} % (target {TARGET:.0f} %)")
    return 0 if total and overall >= TARGET else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
