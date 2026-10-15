"""Merges the v2 corpus rows into bench/cleanup/ (docs/bench.md, The v2 corpus).

`uv run scripts/gen_corpus_v2.py BUILD_DIR [BUILD_DIR ...]` reads each build's `rows.jsonl`, one v2 row per line, as
the category generators wrote them: a writer model drafts rows per kind, two independent checker models
must both pass the gold, static gold checks run, near-duplicates by 8-gram drop and at least 2 % per slice is read by
hand (the note says so). It applies the hand decisions below, drops a row that repeats the said text of a row in the
corpus or shares an 8-word run of said words with one, renames a "trap" kind whose gold changes more than formatting,
gives a new row its split, and writes `{area}.jsonl.gz` sorted by id. A build directory named after its
area (`v2-numbers`) or starting with it (`v2-addresses-marks-paragraphs`) fills that area; rows already in a file keep
their id and split.
"""

import gzip
import hashlib
import json
import sys
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import bench
from bench import CORPUS, NUMBER_WORDS, is_v2, words_of

AREAS = [
    "v2-stt-homophones",
    "v2-stt-nearsound",
    "v2-grammar",
    "v2-punctuation",
    "v2-lists",
    "v2-numbers",
    "v2-addresses",
    "v2-backtracking",
    "v2-dictionary",
    "v2-traps",
    "v2-long",
    "founder-variants",
    "suite-growth",
]
# Words said per length bucket: sentence is one sentence, 2-5 two to five sentences.
LENGTH_WORDS = {"1-4": (1, 4), "sentence": (5, 25), "2-5": (5, 79), "paragraph": (80, 150), "multi": (200, 400)}
DEV_SHARE, DEV_MIN = 3, 5

# Spoken marks, dropped when a trap's gold is compared with what was said, and the words of an amount or a time.
SPOKEN_MARKS = set(
    "comma period full stop question mark exclamation colon semicolon dash hyphen quote unquote new line "
    "paragraph".split()
)
AMOUNTS = set(
    "point hundred thousand lakh lakhs crore million billion dollars dollar rupees rupee euros euro pounds pound "
    "percent cents am pm".split()
)
LINKS = {"the", "of"}

# Hand decisions on built rows, each with its reason: a new expected list, or a drop.
EXPECTED = {
    "v2-numbers-en-00143": (0, "'one hour 30 minutes' breaks rule 4: a number before a unit of time is digits"),
}
ADDED = {
    "v2-addresses-en-00018": (
        "Ask Saoirse about a question mark over the budget before sending invoice.",
        "the speaker never said 'the'; the answer without it is right too",
    ),
}
DROPS: dict[str, str] = {}


def dev_by_id(row_id: str) -> bool:
    """Whether a row's id puts it in dev: sha256(id) mod 10 under 3, about 30 %."""
    digest = int(hashlib.sha256(row_id.encode()).hexdigest(), 16)
    return digest % 10 < DEV_SHARE


def amount(word: str) -> bool:
    return any(character.isdigit() for character in word) or word in NUMBER_WORDS or word in AMOUNTS


def formatted(text: str) -> list[str]:
    """The words with spoken marks and apostrophes gone, each run of number words, digits and units as "#", and a
    "the" or "of" around one gone ("the twenty second of" is "22")."""
    found: list[str] = []
    said = [word.replace("'", "").replace("\u2019", "") for word in words_of(text)]
    for index, bare in enumerate(said):
        dated = bare in LINKS and (found[-1:] == ["#"] or amount(said[index + 1] if index + 1 < len(said) else ""))
        if (bare in SPOKEN_MARKS and not amount(bare)) or dated:
            continue
        if amount(bare):
            if found[-1:] != ["#"]:
                found.append("#")
            continue
        found.append(bare)
    return found


def formatting_only(said: str, written: str, terms: list[str]) -> bool:
    """Whether the written text changes only marks, case, apostrophes, spoken marks, number forms and spoken forms of
    terms: what a trap's gold may change."""
    heard = formatted(said)
    wrote = formatted(written)
    term_words = {word for term in terms for word in words_of(term)}
    for op, start, end, written_start, written_end in SequenceMatcher(a=heard, b=wrote, autojunk=False).get_opcodes():
        old, new = set(heard[start:end]), set(wrote[written_start:written_end])
        numbers = (old | new) <= {"#"}
        spelled = op != "delete" and new <= term_words
        if op != "equal" and not numbers and not spelled:
            return False
    return True


def gold_refused(row: dict) -> bool:
    """Whether the guardrail refuses a row's first gold answer, judged as the server judges it: a piece after a
    prior answers with the text after the prior, or with a revision when the gold changes the prior."""
    item = bench.Item("", "", row["said"], row.get("prior", ""), row["dictionary"], "", row["expected"])
    refused = bench.gold_refused(item)
    return refused


def area_of(build: Path) -> str:
    found = next(area for area in AREAS if build.name.startswith(area))
    return found


def decided(row: dict) -> dict:
    """The row after the hand decisions and with a non-clean trap renamed."""
    keep, _ = EXPECTED.get(row["id"], (None, ""))
    if keep is not None:
        row = {**row, "expected": [row["expected"][keep]]}
    if row["id"] in ADDED:
        row = {**row, "expected": [*row["expected"], ADDED[row["id"]][0]]}
    verbatim = [
        keep for keep in row.get("keeps", []) if words_of(keep) and all(holds(out, keep) for out in row["expected"])
    ]
    if verbatim != row.get("keeps", verbatim):
        lost = [keep for keep in row["keeps"] if keep not in verbatim]
        row = {
            **row,
            "keeps": verbatim,
            "note": f"{row['note']} [merge: keeps not verbatim in the gold dropped: {lost}]",
        }
    said = f"{row.get('prior', '')} {row['said']}"
    clean = all(formatting_only(said, out, row["dictionary"]) for out in row["expected"])
    if row["kind"].startswith("trap") and not clean:
        note = f"{row['note']} [merge: kind was '{row['kind']}'; the gold fixes more than formatting]"
        row = {**row, "kind": f"fixed text with a trap: {row['kind'].removeprefix('trap ')}", "note": note}
    return row


def holds(text: str, phrase: str) -> bool:
    found = bench.holds(words_of(text), words_of(phrase))
    return found


def grams(text: str) -> set[tuple[str, ...]]:
    found = words_of(text)
    runs = {tuple(found[start : start + 8]) for start in range(len(found) - 7)}
    return runs


def read(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as lines:
        rows = [json.loads(line) for line in lines]
    return rows


def with_splits(rows: list[dict]) -> list[dict]:
    """Rows with a split: a new row by its id, then new heldout rows moved to dev, lowest hash first, until every
    length cell has 5 dev rows (or all of a smaller cell). A row that has a split keeps it."""
    rows = [row if "split" in row else {**row, "split": "dev" if dev_by_id(row["id"]) else "heldout"} for row in rows]
    cells = Counter(row["length"] for row in rows)
    dev = Counter(row["length"] for row in rows if row["split"] == "dev")
    movable = sorted(
        (row for row in rows if row["split"] == "heldout"),
        key=lambda row: hashlib.sha256(row["id"].encode()).hexdigest(),
    )
    moved = set()
    for row in movable:
        cell = row["length"]
        if dev[cell] < min(DEV_MIN, cells[cell]) and row.get("new"):
            dev[cell] += 1
            moved.add(row["id"])
    placed = [{**row, "split": "dev"} if row["id"] in moved else row for row in rows]
    return placed


def merge(builds: list[Path]) -> dict[str, list[dict]]:
    old = [
        row
        for path in sorted(CORPUS.glob("*.jsonl.gz"))
        if not is_v2(path.name.removesuffix(".jsonl.gz"))
        for row in read(path)
    ]
    said = {" ".join(words_of(row["said"])) for row in old}
    seen = set().union(*(grams(row["said"]) for row in old))
    files: dict[str, list[dict]] = {}
    for path in sorted(CORPUS.glob("*.jsonl.gz")):
        name = path.name.removesuffix(".jsonl.gz")
        if is_v2(name):
            files[name] = read(path)
    kept = {row["id"] for rows in files.values() for row in rows}
    for rows in files.values():
        for row in rows:
            said.add(" ".join(words_of(row["said"])))
            seen |= grams(row["said"])
    dropped = Counter()
    for build in builds:
        area = area_of(build)
        rows = [json.loads(line) for line in (build / "rows.jsonl").read_text(encoding="utf-8").splitlines() if line]
        for row in rows:
            text = " ".join(words_of(row["said"]))
            runs = grams(row["said"])
            reason = DROPS.get(row["id"]) or ("repeat" if text in said else "near" if not runs.isdisjoint(seen) else "")
            if row["id"] in kept:
                continue
            if reason:
                dropped[f"{area} {reason[:40]}"] += 1
                continue
            said.add(text)
            seen |= runs
            files.setdefault(area, []).append({**decided(row), "new": True})
    for reason, count in sorted(dropped.items()):
        print(f"dropped {count}: {reason}")
    merged = {}
    for name, rows in files.items():
        placed = with_splits(rows)
        merged[name] = sorted(
            ({k: v for k, v in row.items() if k != "new"} for row in placed), key=lambda row: row["id"]
        )
    return merged


def main() -> int:
    builds = [Path(arg) for arg in sys.argv[1:]]
    for name, rows in merge(builds).items():
        with gzip.open(CORPUS / f"{name}.jsonl.gz", "wt", encoding="utf-8") as out:
            out.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        print(f"{name}: {len(rows)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
