"""The app's speaker edits sent as one call (TRX-11): a merge, its undo, a line given to a new speaker, each as the
speakers and lines stand after it. The route is POST /v1/recordings/{id}/speakers/edits."""

import json
import uuid
from typing import Any

import pytest
from conftest import RECORDING, Sql, failure, put_recording, register

PRIYA = uuid.UUID("0b6f7c1e-2a3d-4e5f-8a9b-0c1d2e3f4a5b")
ARJUN = uuid.UUID("1c7a8d2f-3b4e-4f6a-9b0c-1d2e3f4a5b6c")
MAYA = uuid.UUID("2d8b9e3a-4c5f-4a7b-8c1d-2e3f4a5b6c7d")
LINES = [uuid.UUID(int=n) for n in range(1, 5)]
PATH = f"/v1/recordings/{RECORDING}/speakers/edits"
# Priya says Arjun owns the FAQ; the minutes name both by their tokens (D41).
MINUTES = {
    "title": None,
    "summary": "{{p:1}} said {{p:2}} owns the FAQ.",
    "mom": "",
    "actionItems": [],
    "decisions": [],
    "openQuestions": [],
    "pinned": [],
}


@pytest.fixture
def auth(api: Any, sql: Sql) -> dict[str, str]:
    """A meeting's ready transcript: Priya (Speaker 1, with a voiceprint) says lines 1 and 2, Arjun (Speaker 2) line
    3, Maya (Speaker 3) line 4."""
    made = register(api)
    put_recording(api, made)
    recording = uuid.UUID(RECORDING)
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, name, name_source, color_idx, voiceprint)"
        " VALUES ($1, $4, 'Speaker 1', 'S0', 'Priya', 'vendor', 0, 'print'::bytea),"
        " ($2, $4, 'Speaker 2', 'S1', NULL, NULL, 1, NULL), ($3, $4, 'Speaker 3', 'S2', 'Maya', 'user', 2, NULL)",
        PRIYA,
        ARJUN,
        MAYA,
        recording,
    )
    owners = [PRIYA, PRIYA, ARJUN, MAYA]
    for idx, (line, owner) in enumerate(zip(LINES, owners, strict=True)):
        sql(
            "INSERT INTO transcript_lines (id, recording_id, speaker_id, idx, start_ms, end_ms, text, words)"
            " VALUES ($1, $2, $3, $4, 0, 1, 'Hi', '[]')",
            line,
            recording,
            owner,
            idx,
        )
    sql(
        "INSERT INTO transcription_jobs (id, recording_id, state, strategy, language_requested, script, billed_seconds)"
        " VALUES ($1, $2, 'ready', 'fake', 'auto', 'native', 60)",
        uuid.uuid4(),
        recording,
    )
    sql(
        "INSERT INTO meetings (id, recording_id, title, state, minutes) VALUES ($1, $2, 'Sync', 'ready', $3::jsonb)",
        uuid.uuid4(),
        recording,
        json.dumps(MINUTES),
    )
    return made


def speaker(id: uuid.UUID, label: str, name: str | None, color: int, deleted: bool = False) -> dict[str, Any]:
    return {"id": str(id), "label": label, "name": name, "colorIdx": color, "deleted": deleted}


# Priya merged into Arjun under Priya's name: her lines are his, she is gone, he is "Priya" now.
MERGE = {
    "speakers": [speaker(PRIYA, "Speaker 1", "Priya", 0, deleted=True), speaker(ARJUN, "Speaker 2", "Priya", 1)],
    "lines": [{"id": str(LINES[0]), "speakerId": str(ARJUN)}, {"id": str(LINES[1]), "speakerId": str(ARJUN)}],
}


def owners(sql: Sql) -> list[uuid.UUID]:
    return [row["speaker_id"] for row in sql("SELECT speaker_id FROM transcript_lines ORDER BY idx")]


def test_trx11_a_merge_moves_the_lines_drops_the_speaker_and_her_voiceprint_and_names_the_one_kept(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    done = api.post(PATH, json=MERGE, headers=auth)

    assert done.status_code == 200
    assert owners(sql) == [ARJUN, ARJUN, ARJUN, MAYA]
    [gone] = sql("SELECT deleted_at IS NOT NULL AS gone, voiceprint FROM speakers WHERE id = $1", PRIYA)
    assert (gone["gone"], gone["voiceprint"]) == (True, None)
    [kept] = sql("SELECT name, name_source FROM speakers WHERE id = $1", ARJUN)
    # The user's own merge: the speaker loop never renames or splits it (MOM-08).
    assert (kept["name"], kept["name_source"]) == ("Priya", "user")
    # Each moved line is the user's edit now (the loop moves only lines at rev 1), and its new rev comes back.
    revs = {row["id"]: row["rev"] for row in sql("SELECT id, rev FROM transcript_lines")}
    assert (revs[LINES[0]], revs[LINES[1]], revs[LINES[2]]) == (2, 2, 1)
    assert done.json()["lines"] == [{"id": str(LINES[0]), "rev": 2}, {"id": str(LINES[1]), "rev": 2}]


def test_trx11_after_a_merge_the_minutes_name_the_one_kept_at_once_and_are_stale(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    api.post(PATH, json=MERGE, headers=auth)

    [meeting] = sql("SELECT minutes, transcript_version FROM meetings")
    # Speaker 1's token is Speaker 2's now, so it never reads "someone" while the minutes wait for Regenerate.
    assert json.loads(meeting["minutes"])["summary"] == "{{p:2}} said {{p:2}} owns the FAQ."
    assert meeting["transcript_version"] == 1


def test_trx11_the_same_edits_sent_again_change_nothing_and_leave_the_minutes_fresh(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    api.post(PATH, json=MERGE, headers=auth)
    again = api.post(PATH, json=MERGE, headers=auth)

    assert again.status_code == 200
    assert [row["rev"] for row in sql("SELECT rev FROM transcript_lines ORDER BY idx")] == [2, 2, 1, 1]
    assert sql("SELECT transcript_version FROM meetings") == [{"transcript_version": 1}]


def test_trx11_undoing_a_merge_brings_her_back_with_her_lines_and_his_old_name(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    api.post(PATH, json=MERGE, headers=auth)
    undo = {
        "speakers": [
            {**speaker(PRIYA, "Speaker 1", "Priya", 0), "nameSource": "vendor"},
            speaker(ARJUN, "Speaker 2", None, 1),
        ],
        "lines": [{"id": str(LINES[0]), "speakerId": str(PRIYA)}, {"id": str(LINES[1]), "speakerId": str(PRIYA)}],
    }

    assert api.post(PATH, json=undo, headers=auth).status_code == 200
    assert owners(sql) == [PRIYA, PRIYA, ARJUN, MAYA]
    names = {row["id"]: (row["name"], row["name_source"]) for row in sql("SELECT id, name, name_source FROM speakers")}
    assert names[PRIYA] == ("Priya", "vendor")
    assert names[ARJUN] == (None, None)
    assert sql("SELECT count(*) AS live FROM speakers WHERE deleted_at IS NULL") == [{"live": 3}]


def test_trx11_a_line_given_to_a_new_speaker_makes_that_speaker(api: Any, auth: dict[str, str], sql: Sql) -> None:
    noah = uuid.uuid4()
    edits = {
        "speakers": [speaker(noah, "Speaker 4", "Noah", 3)],
        "lines": [{"id": str(LINES[3]), "speakerId": str(noah)}],
    }

    assert api.post(PATH, json=edits, headers=auth).status_code == 200
    # Maya's only line went to Noah, so she is gone: the app sends her deleted in the same edit; here she is not, and
    # a speaker with no line left stays until the app says so.
    assert owners(sql)[3] == noah
    [made] = sql("SELECT label, name, name_source, color_idx, vendor_label FROM speakers WHERE id = $1", noah)
    assert made == {"label": "Speaker 4", "name": "Noah", "name_source": "user", "color_idx": 3, "vendor_label": "user"}


def test_trx11_edits_that_would_leave_a_line_on_no_one_are_refused_and_change_nothing(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    # Priya deleted with a line still hers.
    half = {**MERGE, "lines": MERGE["lines"][:1]}
    assert failure(api.post(PATH, json=half, headers=auth)) == (422, "invalid")
    # A line moved to a speaker this recording doesn't have.
    stray = {"speakers": [], "lines": [{"id": str(LINES[0]), "speakerId": str(uuid.uuid4())}]}
    assert failure(api.post(PATH, json=stray, headers=auth)) == (422, "invalid")
    # A line moved onto a speaker deleted in the same edits.
    onto_gone = {
        "speakers": [speaker(ARJUN, "Speaker 2", None, 1, deleted=True)],
        "lines": [{"id": str(LINES[2]), "speakerId": str(ARJUN)}],
    }
    assert failure(api.post(PATH, json=onto_gone, headers=auth)) == (422, "invalid")
    assert owners(sql) == [PRIYA, PRIYA, ARJUN, MAYA]
    assert sql("SELECT count(*) AS live FROM speakers WHERE deleted_at IS NULL") == [{"live": 3}]


def test_trx11_while_the_minutes_are_being_written_speaker_edits_wait(api: Any, auth: dict[str, str], sql: Sql) -> None:
    sql("UPDATE meetings SET state = 'generating'")

    assert failure(api.post(PATH, json=MERGE, headers=auth)) == (409, "minutes_busy")
    assert owners(sql) == [PRIYA, PRIYA, ARJUN, MAYA]


def test_trx11_another_device_s_recording_is_404(api: Any, auth: dict[str, str]) -> None:
    stranger = register(api, str(uuid.uuid4()))
    assert failure(api.post(PATH, json=MERGE, headers=stranger)) == (404, "not_found")


def test_trx11_a_new_speaker_may_take_the_label_of_one_merged_away(api: Any, auth: dict[str, str], sql: Sql) -> None:
    """The app numbers a new speaker after the live ones, so after Speaker 1 merged away its next is Speaker 1 again
    (a 500 on 3 Nov: the label was unique across deleted speakers too)."""
    api.post(PATH, json=MERGE, headers=auth)
    noah = uuid.uuid4()
    edits = {
        "speakers": [speaker(noah, "Speaker 1", "Noah", 0)],
        "lines": [{"id": str(LINES[3]), "speakerId": str(noah)}],
    }

    assert api.post(PATH, json=edits, headers=auth).status_code == 200
    # Through the Mac's other speaker route too: a rename sends the label it has.
    renamed = {"label": "Speaker 1", "name": "Noah Smith", "colorIdx": 0}
    put = api.put(f"/v1/recordings/{RECORDING}/speakers/{noah}", json=renamed, headers=auth)
    assert put.status_code == 200
    live = sql("SELECT name FROM speakers WHERE label = 'Speaker 1' AND deleted_at IS NULL")
    assert live == [{"name": "Noah Smith"}]


def test_trx11_undoing_a_merge_whose_label_a_new_speaker_took_is_refused(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    api.post(PATH, json=MERGE, headers=auth)
    noah = uuid.uuid4()
    edits = {
        "speakers": [speaker(noah, "Speaker 1", "Noah", 0)],
        "lines": [{"id": str(LINES[3]), "speakerId": str(noah)}],
    }
    api.post(PATH, json=edits, headers=auth)
    undo = {
        "speakers": [speaker(PRIYA, "Speaker 1", "Priya", 0)],
        "lines": [{"id": str(LINES[0]), "speakerId": str(PRIYA)}],
    }

    assert failure(api.post(PATH, json=undo, headers=auth)) == (422, "invalid")
    assert sql("SELECT deleted_at IS NOT NULL AS gone FROM speakers WHERE id = $1", PRIYA) == [{"gone": True}]


def test_mom08_the_loop_s_merge_undo_is_refused_once_a_new_speaker_took_the_label(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    """The speaker loop merged Speaker 1 into Speaker 2; the user then made a new Speaker 1. Undo would bring two
    Speaker 1s back: it waits, as an undo under a later change does."""
    recording = uuid.UUID(RECORDING)
    sql("UPDATE speakers SET deleted_at = now() WHERE id = $1", PRIYA)
    sql("UPDATE transcript_lines SET speaker_id = $1 WHERE speaker_id = $2", ARJUN, PRIYA)
    noah = uuid.uuid4()
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, name, name_source, color_idx)"
        " VALUES ($1, $2, 'Speaker 1', 'user', 'Noah', 'user', 0)",
        noah,
        recording,
    )
    merge = {
        "id": "chg_m",
        "kind": "merge",
        "state": "applied",
        "speakerId": str(PRIYA),
        "intoId": str(ARJUN),
        "lineIds": [str(LINES[0]), str(LINES[1])],
        "before": "Priya",
    }
    state = {"run": str(uuid.uuid4()), "rev": 1, "stage": "done", "round": 1, "changes": [merge]}
    sql("UPDATE recordings SET speaker_pass = $1::jsonb WHERE id = $2", json.dumps(state), recording)

    undone = api.post(f"/v1/recordings/{RECORDING}/speakers/changes/chg_m", json={"action": "undo"}, headers=auth)

    assert failure(undone) == (409, "change_later")
    assert sql("SELECT deleted_at IS NOT NULL AS gone FROM speakers WHERE id = $1", PRIYA) == [{"gone": True}]
