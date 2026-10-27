"""The speaker identification loop's routes (api/v1/speakers.py, MOM-08): the user's answer to a change, undo all,
running the loop again, and the pass served with the transcript, without the worker."""

import json
import uuid
from typing import Any

import pytest
from conftest import RECORDING, Sql, failure, put_recording, queued_arq_jobs, register

NAMED = uuid.UUID("3f2e9a4c-1b7d-4e5f-8a90-1c2d3e4f5a6b")
UNSURE = uuid.UUID("e8a0c1d2-3b4f-4a5e-9c6d-7f8a9b0c1d2e")
PASS = {
    # The run holding the pass, as the loop stores it.
    "run": "6c1f8b2e-0d4a-4f3b-9e7a-5b2c8d1e3f40",
    "rev": 1,
    "stage": "done",
    "round": 1,
    "changes": [
        {"id": "chg_a", "kind": "name", "state": "applied", "speakerId": str(NAMED), "name": "Joanne", "before": None},
        {"id": "chg_b", "kind": "name", "state": "suggested", "speakerId": str(UNSURE), "name": "Noah", "before": None},
    ],
}


@pytest.fixture
def auth(api: Any, sql: Sql) -> dict[str, str]:
    """A recording whose loop named Speaker 1 "Joanne" and suggests "Noah" for Speaker 2, its transcript ready."""
    made = register(api)
    put_recording(api, made)
    recording = uuid.UUID(RECORDING)
    sql(
        "INSERT INTO speakers (id, recording_id, label, vendor_label, name, name_source, color_idx)"
        " VALUES ($1, $3, 'Speaker 1', 'S0', 'Joanne', 'guessed', 0), ($2, $3, 'Speaker 2', 'S1', NULL, NULL, 1)",
        NAMED,
        UNSURE,
        recording,
    )
    sql(
        "INSERT INTO transcription_jobs (id, recording_id, state, strategy, language_requested, script, billed_seconds)"
        " VALUES ($1, $2, 'ready', 'fake', 'auto', 'native', 613)",
        uuid.uuid4(),
        recording,
    )
    sql("UPDATE recordings SET speaker_pass = $1::jsonb WHERE id = $2", json.dumps(PASS), recording)
    return made


def answer(api: Any, auth: dict[str, str], change: str, action: str) -> Any:
    path = f"/v1/recordings/{RECORDING}/speakers/changes/{change}"
    return api.post(path, json={"action": action}, headers=auth)


def names(sql: Sql) -> dict[str, tuple[str | None, str | None]]:
    rows = sql("SELECT label, name, name_source FROM speakers ORDER BY label")
    return {row["label"]: (row["name"], row["name_source"]) for row in rows}


def test_mom08_undo_takes_a_guessed_name_back_and_moves_the_pass_rev(api: Any, auth: dict[str, str], sql: Sql) -> None:
    done = answer(api, auth, "chg_a", "undo")
    assert done.status_code == 200
    # The run that holds the pass is the server's own: never sent.
    assert "run" not in done.json()
    assert (done.json()["rev"], done.json()["changes"][0]["state"]) == (2, "undone")
    assert names(sql)["Speaker 1"] == (None, None)


def test_mom08_a_suggestion_accepted_is_the_user_s_name_and_rejected_is_history(
    api: Any, auth: dict[str, str], sql: Sql
) -> None:
    assert answer(api, auth, "chg_b", "accept").json()["changes"][1]["state"] == "applied"
    assert names(sql)["Speaker 2"] == ("Noah", "user")
    # Answered already: rejecting it now is the wrong state.
    assert failure(answer(api, auth, "chg_b", "reject")) == (409, "change_state")


def test_mom08_an_unknown_change_another_device_or_a_bad_action_is_refused(api: Any, auth: dict[str, str]) -> None:
    assert failure(answer(api, auth, "chg_nope", "undo")) == (404, "not_found")
    stranger = register(api, str(uuid.uuid4()))
    assert failure(answer(api, stranger, "chg_a", "undo")) == (404, "not_found")
    assert failure(answer(api, auth, "chg_a", "delete")) == (422, "invalid")


def test_mom08_undo_all_takes_back_every_applied_change(api: Any, auth: dict[str, str], sql: Sql) -> None:
    done = api.post(f"/v1/recordings/{RECORDING}/speakers/undo", headers=auth).json()
    assert "run" not in done
    assert [change["state"] for change in done["changes"]] == ["undone", "suggested"]
    assert names(sql)["Speaker 1"] == (None, None)


def test_mom08_the_transcript_carries_the_pass_and_identify_queues_the_loop(api: Any, auth: dict[str, str]) -> None:
    served = api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()
    assert served["speakerPass"]["changes"][0]["name"] == "Joanne"
    asked = api.post(f"/v1/recordings/{RECORDING}/speakers/identify", headers=auth)
    assert (asked.status_code, asked.json()["stage"]) == (200, "reading")
    # Each ask is its own job (arq keeps a finished job's ID an hour), the run token in its ID.
    [job] = queued_arq_jobs()
    assert job.startswith(f"speakers:{RECORDING}:")
    # A loop is running now: another ask waits for it.
    again = api.post(f"/v1/recordings/{RECORDING}/speakers/identify", headers=auth)
    assert failure(again) == (409, "not_ready")
