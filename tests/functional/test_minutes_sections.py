"""Minutes v2 through the worker and the routes (MEETINGS.md 7, 26 Oct 2026): one call per section, each stored and
published as it lands, a section that fails marked and retried alone, a crashed run resumed where it stopped, the
user's own edits with their version check, and the meeting's title taken from the minutes unless the user set it."""

import json
import logging
import threading
from typing import Any
from uuid import UUID

import pytest
import test_meeting_flow as flow
from conftest import RECORDING, Sql, follow, register, upload_audio
from test_meeting_flow import (
    RUN,
    Start,
    as_generating,
    drain,
    drain_wait,
    meeting_detail,
    meeting_row,
    put_recording,
    ready_meeting,
    run_task,
    wait_ready,
)

# The meeting flow's fixtures, shared.
meetings_env = flow.meetings_env
meetings_api = flow.meetings_api

CALLS = [
    "summary",
    "mom",
    "actionItems",
    "decisions",
    "openQuestions",
    "risks",
    "followUps",
    "nextAgenda",
    "agenda",
    "contradictions",
]


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The sections the minutes asked the model for, in order."""
    from snapbak_backend.services import minutes

    seen: list[str] = []
    real = minutes.write_section

    async def spied(tiers: Any, section: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(section)
        return await real(tiers, section, *args, **kwargs)

    monkeypatch.setattr(minutes, "write_section", spied)
    return seen


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], str]]:
    """Each save of a run: the sections still pending, and the meeting's state while it was stored."""
    from snapbak_backend.services import meetings

    seen: list[tuple[list[str], str]] = []
    real = meetings.save_minutes

    async def spied(session: Any, meeting_id: UUID, run: UUID, body: dict[str, Any]) -> Any:
        made = await real(session, meeting_id, run, body)
        which = meetings.select(meetings.Meeting.state).where(meetings.Meeting.id == meeting_id)
        state = (await session.execute(which)).scalar_one()
        seen.append((list(body["pending"]), state))
        return made

    monkeypatch.setattr(meetings, "save_minutes", spied)
    return seen


def failing(monkeypatch: pytest.MonkeyPatch, sections: set[str], code: str = "provider") -> None:
    from snapbak_backend.services import minutes

    real = minutes.write_section

    async def broken(tiers: Any, section: str, *args: Any, **kwargs: Any) -> Any:
        if section in sections:
            raise minutes.SectionFailed(code)
        return await real(tiers, section, *args, **kwargs)

    monkeypatch.setattr(minutes, "write_section", broken)


def test_mom05_each_section_is_stored_and_published_as_it_lands_in_order(
    meetings_api: Any, start_worker: Start, sql: Sql, stored: list[tuple[list[str], str]]
) -> None:
    """Title and summary first, then each section in turn; the meeting stays `generating` with what is left in
    `pending` until the last lands, then `ready`."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    stored.clear()
    as_generating(sql)
    sql("UPDATE meetings SET minutes = NULL")
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    pending = [left for left, _ in stored]
    assert pending[: len(CALLS)] == [CALLS[n + 1 :] for n in range(len(CALLS))]
    assert {state for _, state in stored} == {"generating"}
    detail = meeting_detail(meetings_api, auth)
    assert (detail["state"], detail["minutes"]["pending"], detail["minutes"]["incomplete"]) == ("ready", [], [])
    drain(detail, meeting_id)


def test_mom05_a_partial_document_is_served_while_the_rest_is_written(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    auth, _ = ready_meeting(meetings_api, start_worker)
    done = meeting_detail(meetings_api, auth)["minutes"]
    partial = {**done, "pending": CALLS[2:], "actionItems": [], "decisions": []}
    as_generating(sql)
    sql("UPDATE meetings SET minutes = $1::jsonb", json.dumps(partial))
    detail = meeting_detail(meetings_api, auth)
    assert detail["state"] == "generating"
    assert detail["minutes"]["summary"] == done["summary"] and detail["minutes"]["pending"] == CALLS[2:]


def test_mom05_a_section_that_fails_is_marked_and_the_minutes_are_ready_without_it(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    as_generating(sql)
    sql("UPDATE meetings SET minutes = NULL")
    failing(monkeypatch, {"risks"})
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    detail = meeting_detail(meetings_api, auth)
    assert detail["state"] == "ready"
    assert detail["minutes"]["incomplete"] == [{"section": "risks", "code": "provider"}]
    assert detail["minutes"]["risks"] == [] and detail["minutes"]["actionItems"]
    drain(detail, meeting_id)


def test_mom05_regenerate_with_sections_writes_only_those_and_keeps_the_rest(
    meetings_api: Any, start_worker: Start, sql: Sql, asked: list[str]
) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    before = meeting_detail(meetings_api, auth)["minutes"]
    marked = {**before, "risks": [], "incomplete": [{"section": "risks", "code": "provider"}]}
    sql("UPDATE meetings SET minutes = $1::jsonb", json.dumps(marked))
    asked.clear()
    again = meetings_api.post(
        f"/v1/meetings/{meeting_id}/minutes/regenerate", json={"sections": ["risks"]}, headers=auth
    )
    assert again.status_code == 200, again.json()
    detail = wait_ready(meetings_api, auth, meeting_id)
    drain(detail, meeting_id)
    assert asked == ["risks"]
    assert detail["minutes"]["incomplete"] == [] and detail["minutes"]["risks"]
    assert detail["minutes"]["actionItems"] == before["actionItems"]


def test_mom05_regenerate_refuses_a_section_that_does_not_exist(meetings_api: Any, start_worker: Start) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    again = meetings_api.post(
        f"/v1/meetings/{meeting_id}/minutes/regenerate", json={"sections": ["gossip"]}, headers=auth
    )
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "invalid"


def test_mom05_a_crashed_run_resumes_at_its_first_unfinished_section(
    meetings_api: Any, start_worker: Start, sql: Sql, asked: list[str]
) -> None:
    """The worker died after `openQuestions` landed: the retried task asks only for what is still pending."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    done = meeting_detail(meetings_api, auth)["minutes"]
    half = {**done, "pending": CALLS[5:], "risks": [], "followUps": []}
    as_generating(sql)
    sql("UPDATE meetings SET minutes = $1::jsonb", json.dumps(half))
    asked.clear()
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    assert asked == CALLS[5:]
    detail = meeting_detail(meetings_api, auth)
    assert detail["state"] == "ready" and detail["minutes"]["risks"]
    assert detail["minutes"]["summary"] == done["summary"]
    drain(detail, meeting_id)


def test_mom05_a_run_whose_summary_and_minutes_both_failed_for_the_provider_fails_and_keeps_the_rest(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model was down for both texts: the run fails `provider` at once, the other sections kept for the sweep's
    restart of just the failed ones (grading 4: no arq retry on top of the sweep)."""
    _, meeting_id = ready_meeting(meetings_api, start_worker)
    as_generating(sql)
    sql("UPDATE meetings SET minutes = NULL")
    failing(monkeypatch, {"summary", "mom"})
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    row = sql("SELECT state, minutes_failure, minutes FROM meetings")[0]
    kept = json.loads(row["minutes"]) if isinstance(row["minutes"], str) else row["minutes"]
    assert (row["state"], row["minutes_failure"]) == ("failed", "provider")
    assert [item["section"] for item in kept["incomplete"]] == ["summary", "mom"]
    assert kept["actionItems"]


def test_mom05_a_run_whose_summary_and_minutes_both_failed_for_good_fails(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, meeting_id = ready_meeting(meetings_api, start_worker)
    as_generating(sql)
    sql("UPDATE meetings SET minutes = NULL")
    failing(monkeypatch, {"summary", "mom"}, code="bad_answer")
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    row = meeting_row(sql)
    assert (row["state"], row["minutes_failure"]) == ("failed", "bad_answer")


def test_mom05_a_run_that_lost_its_token_stops_at_its_next_save(
    meetings_api: Any, start_worker: Start, sql: Sql, asked: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep handed the meeting to a new run mid-way: the old one writes nothing more and asks for no more."""
    from snapbak_backend.services import minutes

    _, meeting_id = ready_meeting(meetings_api, start_worker)
    as_generating(sql)
    sql("UPDATE meetings SET minutes = NULL")
    real = minutes.write_section

    async def taken(tiers: Any, section: str, *args: Any, **kwargs: Any) -> Any:
        made = await real(tiers, section, *args, **kwargs)
        if section == "mom":
            # From another connection, as the sweep would (the sql helper runs its own event loop).
            other = "UPDATE meetings SET minutes_run = '00000000-0000-4000-8000-000000000001'"
            taking = threading.Thread(target=sql, args=(other,))
            taking.start()
            taking.join()
        return made

    monkeypatch.setattr(minutes, "write_section", taken)
    asked.clear()
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    assert meeting_row(sql)["state"] == "generating"
    assert "actionItems" not in [section for section in asked]


def edit(meetings_api: Any, auth: dict[str, str], meeting_id: str, base: int, minutes: dict[str, Any]) -> Any:
    body = {"baseVersion": base, "minutes": minutes}
    return meetings_api.put(f"/v1/meetings/{meeting_id}/minutes", json=body, headers=auth)


def test_mom05_an_edit_marks_the_user_s_items_and_texts_and_keeps_the_ai_s_citations(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    minutes = detail["minutes"]
    ai = minutes["actionItems"][0]
    mine = {"id": "act_user_1", "text": "Call the bank", "owner": "Priya", "due": None, "derivedFrom": []}
    changed = {**minutes["decisions"][0], "text": "We decided otherwise", "derivedFrom": []}
    sent = {
        **minutes,
        "summary": "My own summary.",
        "actionItems": [ai, mine],
        "decisions": [changed],
        "pending": ["risks"],
        "userEdited": [],
    }
    made = edit(meetings_api, auth, meeting_id, detail["version"], sent)
    assert made.status_code == 200, made.json()
    body = made.json()
    assert body["version"] > detail["version"]
    saved = body["minutes"]
    assert saved["userEdited"] == ["summary"] and saved["summary"] == "My own summary."
    assert [item["byUser"] for item in saved["actionItems"]] == [False, True]
    assert saved["decisions"][0]["byUser"] and saved["decisions"][0]["text"] == "We decided otherwise"
    # An edited AI item keeps the evidence the server checked; the client cannot hand it new citations.
    assert saved["decisions"][0]["derivedFrom"] == minutes["decisions"][0]["derivedFrom"]
    assert saved["pending"] == [] and saved["actionItems"][0]["derivedFrom"] == ai["derivedFrom"]
    drain(body, meeting_id)


def test_mom05_an_edit_against_an_old_version_is_a_conflict(meetings_api: Any, start_worker: Start) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    made = edit(meetings_api, auth, meeting_id, detail["version"] - 1, detail["minutes"])
    assert made.status_code == 409
    assert made.json()["error"]["code"] == "version_conflict"


def test_mom05_an_edit_while_the_minutes_are_being_written_waits(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    version = as_generating(sql)
    made = edit(meetings_api, auth, meeting_id, version, detail["minutes"])
    assert made.status_code == 409
    assert made.json()["error"]["code"] == "minutes_not_ready"


def test_mom05_an_edit_over_2_mb_is_refused(meetings_api: Any, start_worker: Start) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    huge = {**detail["minutes"], "mom": "x" * (2 * 1024 * 1024 + 1)}
    made = edit(meetings_api, auth, meeting_id, detail["version"], huge)
    assert made.status_code == 413
    assert made.json()["error"]["code"] == "minutes_too_large"


def test_mom05_a_regenerate_after_an_edit_keeps_the_user_s_work(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    mine = {"id": "act_user_1", "text": "Call the bank", "owner": None, "due": None, "derivedFrom": []}
    sent = {**detail["minutes"], "summary": "Mine.", "actionItems": [*detail["minutes"]["actionItems"], mine]}
    assert edit(meetings_api, auth, meeting_id, detail["version"], sent).status_code == 200
    assert meetings_api.post(f"/v1/meetings/{meeting_id}/minutes/regenerate", headers=auth).status_code == 200
    again = wait_ready(meetings_api, auth, meeting_id)
    drain(again, meeting_id)
    assert again["minutes"]["summary"] == "Mine."
    assert [item["text"] for item in again["minutes"]["actionItems"]] == ["Do the thing", "Call the bank"]


def promoted_as(meetings_api: Any, start_worker: Start, title: str, by_user: bool) -> tuple[dict[str, str], str]:
    start_worker("two-speakers-en")
    auth = register(meetings_api)
    put_recording(auth, meetings_api)
    body = {"title": title, "titleByUser": by_user}
    made = meetings_api.post(f"/v1/recordings/{RECORDING}/meeting", json=body, headers=auth)
    assert made.status_code == 201
    _, done = upload_audio(meetings_api, auth, b"m4a")
    follow(meetings_api, auth, done.json()["jobId"])
    meeting_id = made.json()["meetingId"]
    drain_wait(meetings_api, auth, meeting_id)
    return auth, meeting_id


def test_mom05_the_meeting_takes_the_minutes_title_when_promoted_with_the_default(
    meetings_api: Any, start_worker: Start
) -> None:
    """Promoted under the recording's own title (the app's default), the meeting is named by its minutes."""
    auth, _ = promoted_as(meetings_api, start_worker, "Meeting 2026-10-24 at 14.02.11", False)
    detail = meeting_detail(meetings_api, auth)
    assert detail["title"] == detail["minutes"]["title"] == "About Meeting 2026-10-24 at 14.02.11"


def test_mom05_a_title_the_user_chose_is_never_replaced(meetings_api: Any, start_worker: Start) -> None:
    auth, meeting_id = promoted_as(meetings_api, start_worker, "Weekly sync", True)
    assert meeting_detail(meetings_api, auth)["title"] == "Weekly sync"
    renamed = meetings_api.patch(f"/v1/meetings/{meeting_id}", json={"title": "Board prep"}, headers=auth)
    assert renamed.status_code == 200
    assert meetings_api.post(f"/v1/meetings/{meeting_id}/minutes/regenerate", headers=auth).status_code == 200
    again = wait_ready(meetings_api, auth, meeting_id)
    drain(again, meeting_id)
    assert again["title"] == "Board prep"


def test_mom05_a_rename_after_auto_title_sticks(meetings_api: Any, start_worker: Start) -> None:
    auth, meeting_id = promoted_as(meetings_api, start_worker, "Meeting 2026-10-24 at 14.02.11", False)
    meetings_api.patch(f"/v1/meetings/{meeting_id}", json={"title": "Mine"}, headers=auth)
    assert meetings_api.post(f"/v1/meetings/{meeting_id}/minutes/regenerate", headers=auth).status_code == 200
    again = wait_ready(meetings_api, auth, meeting_id)
    drain(again, meeting_id)
    assert again["title"] == "Mine"


def test_mom05_a_run_that_keeps_stalling_ends_failed_not_restarted_for_ever(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    """A run that always dies (a worker killed on a huge transcript) was restarted every 5 min with no end, the
    meeting left `generating` for good (grading 3): after MAX_ATTEMPTS it fails, Try again starts it afresh."""
    from snapbak_backend.services.meetings import MAX_ATTEMPTS

    ready_meeting(meetings_api, start_worker)
    as_generating(sql, minutes_ago=6)
    sql("UPDATE meetings SET minutes_attempts = $1", MAX_ATTEMPTS)
    run_task("sweep_waiting_jobs")
    row = meeting_row(sql)
    assert (row["state"], row["minutes_failure"], row["run"]) == ("failed", "provider", RUN)


def test_mom05_a_provider_failure_is_started_again_unattended_a_few_times(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    """D39: an outage longer than a run's own retries left every meeting waiting for a click (grading 3). The sweep
    starts a provider failure again, as no click, until MAX_ATTEMPTS runs were spent."""
    from snapbak_backend.services.meetings import MAX_ATTEMPTS

    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    failed = "UPDATE meetings SET state = 'failed', minutes_failure = $1, minutes_attempts = $2, "
    failed += "updated_at = now() - interval '20 minutes'"
    sql(failed, "provider", MAX_ATTEMPTS)
    run_task("sweep_waiting_jobs")
    assert meeting_row(sql)["state"] == "failed"
    sql(failed, "bad_answer", 1)
    run_task("sweep_waiting_jobs")
    assert meeting_row(sql)["state"] == "failed"
    sql(failed, "provider", 1)
    run_task("sweep_waiting_jobs")
    detail = flow.eventually(flow.meeting_detail_read(meetings_api, auth), lambda body: body["state"] == "ready")
    assert detail["state"] == "ready"
    drain(detail, meeting_id)
    assert sql("SELECT minutes_attempts, minutes_clicked FROM meetings") == [
        {"minutes_attempts": 2, "minutes_clicked": False}
    ]


def test_mom05_a_failed_meeting_whose_new_transcript_landed_without_it_is_started(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    """The worker died between a new transcript's merge and the minutes' start, on a meeting still `failed`: the
    orphan sweep took only `awaiting_transcript` ones (grading 3)."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    sql(
        "UPDATE meetings SET state = 'failed', minutes_failure = 'vendor_error', updated_at = now() - interval '1 hour'"
    )
    sql("UPDATE transcription_jobs SET updated_at = now() - interval '6 minutes'")
    run_task("sweep_waiting_jobs")
    detail = flow.eventually(flow.meeting_detail_read(meetings_api, auth), lambda body: body["state"] == "ready")
    assert detail["state"] == "ready"
    drain(detail, meeting_id)


def test_mom05_an_edit_of_exactly_2_mb_in_bytes_is_taken_and_one_byte_more_refused(
    meetings_api: Any, start_worker: Start
) -> None:
    """The bound is bytes: Devanagari is three bytes a character, so a character count let ~6 MB in (grading 3)."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    base = {**detail["minutes"], "mom": ""}
    room = 2 * 1024 * 1024 - len(json.dumps(base, separators=(",", ":"), ensure_ascii=False).encode())
    hindi = {**base, "mom": "क" * (room // 3 + 1)}
    made = edit(meetings_api, auth, meeting_id, detail["version"], hindi)
    assert failure_of(made) == (413, "minutes_too_large")
    exact = {**base, "mom": "x" * room}
    assert edit(meetings_api, auth, meeting_id, detail["version"], exact).status_code == 200


def failure_of(response: Any) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def test_mom05_an_edit_logs_no_word_of_what_the_user_wrote(
    meetings_api: Any, start_worker: Start, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    detail = meeting_detail(meetings_api, auth)
    secret = "Zebra Budget Secret"
    mine = {"id": "act_user_9", "text": secret, "owner": None, "due": None, "derivedFrom": []}
    sent = {**detail["minutes"], "summary": secret, "actionItems": [*detail["minutes"]["actionItems"], mine]}
    made = edit(meetings_api, auth, meeting_id, detail["version"], sent)
    assert made.status_code == 200
    drain(made.json(), meeting_id)
    assert secret.lower() not in caplog.text.lower()


def test_mom05_a_line_deleted_before_the_minutes_is_never_cited(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    """MEETINGS.md 7.2 rule 1: a soft-deleted line is no evidence, for the minutes or for Ask (grading 3)."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    gone = sql("SELECT id::text AS id FROM transcript_lines WHERE idx = 0")[0]["id"]
    sql("UPDATE transcript_lines SET deleted_at = now() WHERE idx = 0")
    assert meetings_api.post(f"/v1/meetings/{meeting_id}/minutes/regenerate", headers=auth).status_code == 200
    detail = wait_ready(meetings_api, auth, meeting_id)
    drain(detail, meeting_id)
    cited = json.dumps(detail["minutes"])
    assert gone not in cited
    asked = meetings_api.post(f"/v1/meetings/{meeting_id}/ask", json={"question": "who joined?"}, headers=auth)
    assert gone not in asked.text


def test_mom06_search_finds_a_decision_as_its_own_kind_with_no_owner(
    meetings_api: Any, start_worker: Start, sql: Sql
) -> None:
    """D38 as changed on 26 Oct 2026: a decision is a search hit of its own kind, with no owner."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    assert sql("SELECT count(*) AS n FROM meeting_chunks WHERE kind = 'decision'") == [{"n": 1}]
    hits = meetings_api.post("/v1/meetings/search", json={"query": "we decided", "limit": 20}, headers=auth).json()
    decision = next(hit for hit in hits["hits"] if hit["kind"] == "decision")
    assert (decision["meetingId"], decision["owner"], decision["snippet"]) == (meeting_id, None, "We decided")


def test_mom05_the_worker_gives_the_calls_the_meeting_s_date(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A "Friday" in the meeting becomes a real date against the day it was held (the recording's own day)."""
    from snapbak_backend.services import minutes

    days: list[object] = []
    real = minutes.write_section

    async def spied(tiers: Any, section: str, *args: Any, **kwargs: Any) -> Any:
        days.append(args[-1])
        return await real(tiers, section, *args, **kwargs)

    _, meeting_id = ready_meeting(meetings_api, start_worker)
    as_generating(sql)
    monkeypatch.setattr(minutes, "write_section", spied)
    assert run_task("generate_minutes", UUID(meeting_id), UUID(RUN)) is None
    assert set(days) == {"2026-10-24"}


def test_mom05_a_ready_meeting_with_a_section_the_provider_failed_is_retried_unattended(
    meetings_api: Any, start_worker: Start, sql: Sql, asked: list[str]
) -> None:
    """A 3-minute outage during `decisions` left the minutes ready with it marked, until a click (grading 4): the
    sweep writes that section again, as no click, within MAX_ATTEMPTS."""
    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    done = meeting_detail(meetings_api, auth)["minutes"]
    marked = {**done, "decisions": [], "incomplete": [{"section": "decisions", "code": "provider"}]}
    sql(
        "UPDATE meetings SET minutes = $1::jsonb, minutes_attempts = 1, updated_at = now() - interval '20 minutes'",
        json.dumps(marked),
    )
    asked.clear()
    run_task("sweep_waiting_jobs")

    def again() -> dict[str, Any]:
        return meeting_detail(meetings_api, auth)

    detail = flow.eventually(again, lambda body: body["state"] == "ready" and not body["minutes"]["incomplete"])
    assert detail["minutes"]["incomplete"] == [] and detail["minutes"]["decisions"]
    assert [section for section in asked if section != "details"] == ["decisions"]
    assert sql("SELECT minutes_attempts, minutes_clicked FROM meetings") == [
        {"minutes_attempts": 2, "minutes_clicked": False}
    ]
    drain(detail, meeting_id)


def test_mom04_an_outage_across_three_sweeps_costs_max_attempts_runs_and_no_more(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calls never multiply in an outage (grading 4): each run asks each section TRIES times, the sweep starts a
    failed run again until MAX_ATTEMPTS, and the sweep after that asks nothing."""
    from snapbak_backend.ai.providers.base import ProviderError
    from snapbak_backend.ai.providers.fake import Fake
    from snapbak_backend.services.meetings import MAX_ATTEMPTS

    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    calls: list[int] = []

    async def down(self: Any, turns: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        raise ProviderError("fake: the provider is down")

    monkeypatch.setattr(Fake, "complete", down)
    assert meetings_api.post(f"/v1/meetings/{meeting_id}/minutes/regenerate", headers=auth).status_code == 200

    def ended() -> dict[str, Any]:
        return meeting_row(sql)

    flow.eventually(ended, lambda row: row["state"] == "failed")
    per_run = len(calls)
    assert per_run > 0
    for _ in range(3):
        sql("UPDATE meetings SET updated_at = now() - interval '20 minutes'")
        run_task("sweep_waiting_jobs")
        flow.eventually(ended, lambda row: row["state"] == "failed")
    row = meeting_row(sql)
    assert (row["state"], row["minutes_failure"]) == ("failed", "provider")
    assert sql("SELECT minutes_attempts FROM meetings") == [{"minutes_attempts": MAX_ATTEMPTS}]
    assert len(calls) == per_run * MAX_ATTEMPTS


def test_mom05_a_speaker_put_that_changes_nothing_leaves_the_minutes_current(
    meetings_api: Any, start_worker: Start
) -> None:
    """The app's outbox resends idempotent PUTs: one that changes nothing is no edit, so no stale mark (grading 4)."""
    auth, _ = ready_meeting(meetings_api, start_worker)
    speaker = meetings_api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()["speakers"][0]
    same = {"label": speaker["label"], "name": speaker["name"], "colorIdx": speaker["colorIdx"]}
    url = f"/v1/recordings/{RECORDING}/speakers/{speaker['id']}"
    assert meetings_api.put(url, json=same, headers=auth).status_code == 200
    assert meeting_detail(meetings_api, auth)["stale"] is False


def test_mom06_what_is_still_undecided_reaches_the_open_question_s_lines(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The founder's case, paraphrased (26 Oct 2026): "What's still undecided?" names no word of any line, so Ask sent
    the meeting's first lines while the open question cited its last ones, and answered "Not in this meeting.". The
    minutes' sections by meaning now lead the candidates, with the lines they cite."""
    from snapbak_backend.services import qa

    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    lines = meetings_api.get(f"/v1/recordings/{RECORDING}/transcript", headers=auth).json()["lines"]
    last = lines[-1]
    said = {"lineId": last["id"], "speaker": "Priya", "atMs": last["start"], "quote": last["text"]}
    doc = {
        "summary": "A short sync.",
        "mom": "### Sync\\n\\nA short sync.",
        "actionItems": [],
        "decisions": [],
        "openQuestions": [{"id": "q_1", "text": "Which dashboard to ship first", "derivedFrom": [said]}],
    }
    sql("UPDATE meetings SET minutes = $1::jsonb", json.dumps(doc))
    sent: list[list[str]] = []
    real = qa.answer

    async def spied(tiers: Any, question: str, asked: Any) -> Any:
        sent.append([str(line.id) for line in asked.lines])
        return await real(tiers, question, asked)

    monkeypatch.setattr(qa, "answer", spied)
    answered = meetings_api.post(
        f"/v1/meetings/{meeting_id}/ask", json={"question": "What's still undecided?"}, headers=auth
    )
    assert answered.status_code == 200
    assert sent and sent[0][0] == last["id"]


def test_mom06_the_minutes_ask_sections_are_stored_once_and_a_question_embeds_only_itself(
    meetings_api: Any, start_worker: Start, sql: Sql, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Embedding every section per question took half of Ask's 5 s (26 Oct 2026). The embed task stores them (kind
    `ask`, never a search hit); a question embeds only itself."""
    from snapbak_backend.schemas.cloud import MinutesOut
    from snapbak_backend.services import embeddings, qa

    auth, meeting_id = ready_meeting(meetings_api, start_worker)
    doc = MinutesOut.model_validate(meeting_detail(meetings_api, auth)["minutes"])
    stored = sql("SELECT count(*) AS n FROM meeting_chunks WHERE kind = 'ask'")
    assert stored == [{"n": len(qa.sections_of(doc))}]
    hits = meetings_api.post("/v1/meetings/search", json={"query": "we decided", "limit": 20}, headers=auth).json()
    assert hits["hits"] and all(hit["kind"] != "ask" for hit in hits["hits"])
    sizes: list[int] = []
    real = embeddings.HashedEmbeddings.embed

    async def counted(self: Any, texts: list[str]) -> list[list[float]]:
        sizes.append(len(texts))
        return await real(self, texts)

    monkeypatch.setattr(embeddings.HashedEmbeddings, "embed", counted)
    asked = meetings_api.post(f"/v1/meetings/{meeting_id}/ask", json={"question": "What's undecided?"}, headers=auth)
    assert asked.status_code == 200 and sizes == [1]
