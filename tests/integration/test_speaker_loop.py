"""The speaker identification loop on a real database (services/speaker_loop.py, MEETINGS.md D42, MOM-08): a round's
changes go into the transcript in place under the recording's lock, the user's own edits stay, each stage is stored
and published, and each applied change is undone where the user hasn't changed it since."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text

from snapbak_backend import worker
from snapbak_backend.ai.chain import Tiers
from snapbak_backend.ai.providers.base import Usage
from snapbak_backend.ai.providers.fake import Fake
from snapbak_backend.api import share_page
from snapbak_backend.api.v1 import speakers as speakers_api
from snapbak_backend.config.settings import DAILY_SECONDS
from snapbak_backend.db.models import (
    Device,
    Meeting,
    MeetingChunk,
    Recording,
    Share,
    Speaker,
    TranscriptionJob,
    TranscriptLine,
    Upload,
)
from snapbak_backend.db.session import engine, sessions
from snapbak_backend.schemas.cloud import SpeakerChangeRequest, SpeakerPass
from snapbak_backend.services import meetings as meetings_service
from snapbak_backend.services import qa, speaker_loop, usage
from snapbak_backend.services.embeddings import HashedEmbeddings
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.transcription.merge import Turn
from snapbak_backend.transcription.strategy import VendorError


class Scripted:
    """A model that answers the speakers call from a script, one answer a round."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        self.engine = "fake:speakers"
        self.answers = [json.dumps(answer) for answer in answers]
        self.asked = 0

    async def complete(self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = "") -> Any:
        self.asked += 1
        said = self.answers.pop(0) if self.answers else json.dumps({"count": 0, "people": [], "moves": []})
        return said, Usage(0, 0)

    async def aclose(self) -> None:
        pass


def words(text: str, at: int) -> list[dict[str, Any]]:
    return [
        {"w": w, "s": at + n * 300, "e": at + n * 300 + 250, "filler": False, "conf": 1.0}
        for n, w in enumerate(text.split())
    ]


# Speaker 1 is Joanne; Speakers 2 and 4 are Noah's one voice split in two; Speaker 3 is the user's own "Ian".
SAID = [
    (2, "Joanne, did you get the mezuzah?"),
    (1, "Yes Noah, I got it today."),
    (2, "Thanks Joanne, it looks great."),
    (1, "Noah, can you hang it?"),
    (2, "Sure, will do."),
    (3, "Mazel tov to you both."),
    (4, "Thank you Ian."),
    (3, "You're welcome."),
    (2, "I will hang it tomorrow morning."),
]
# The rerun at three voices: Noah's lines in one voice (Speaker 4's too), and line 8 in Joanne's.
VOICES = ["NOAH", "JOANNE", "NOAH", "JOANNE", "NOAH", "IAN", "NOAH", "IAN", "JOANNE"]


async def seeded(url: str, edited: int | None = None) -> tuple[UUID, dict[int, UUID], list[UUID]]:
    """A recording with four speakers (Speaker 3 named "Ian" by the user) and the lines above; line `edited` was
    changed by the user (rev 2)."""
    bound = engine(url)
    async with sessions(bound)() as db:
        device = Device(id=uuid4(), install_id=uuid4(), token_hash=os.urandom(16))
        recording = Recording(
            id=uuid4(), device_id=device.id, title="t", duration_ms=60_000, created_at=datetime.now(UTC)
        )
        db.add_all([device, recording])
        await db.flush()
        known = {
            n: Speaker(
                id=uuid4(),
                recording_id=recording.id,
                label=f"Speaker {n}",
                vendor_label=f"S{n}",
                name="Ian" if n == 3 else None,
                name_source="user" if n == 3 else None,
                color_idx=n,
            )
            for n in (1, 2, 3, 4)
        }
        db.add_all(known.values())
        await db.flush()
        lines = []
        for idx, (who, text) in enumerate(SAID):
            made = words(text, idx * 10_000)
            lines.append(
                TranscriptLine(
                    id=uuid4(),
                    recording_id=recording.id,
                    speaker_id=known[who].id,
                    idx=idx,
                    start_ms=made[0]["s"],
                    end_ms=made[-1]["e"],
                    text=text,
                    words=made,
                    rev=2 if idx == edited else 1,
                )
            )
        db.add_all(lines)
        await db.commit()
    await bound.dispose()
    return recording.id, {n: each.id for n, each in known.items()}, [line.id for line in lines]


ANSWER = {
    "count": 3,
    "people": [
        {
            "p": 1,
            "name": "Joanne",
            "cues": [{"i": 0, "quote": "Joanne"}, {"i": 2, "quote": "Thanks Joanne"}],
            "same": [],
        },
        {
            "p": 2,
            "name": "Noah",
            "cues": [{"i": 1, "quote": "Yes Noah"}, {"i": 3, "quote": "Noah, can you"}],
            "same": [4],
        },
        {"p": 3, "name": "Ian", "cues": [{"i": 6, "quote": "Thank you Ian"}], "same": []},
    ],
    "moves": [{"i": 8, "to": 1, "cue": {"i": 8, "quote": "I will hang it"}}],
}


def rerun_at(voices: list[str], same: list[frozenset[UUID]] | None = None, voiceprints: bool = False) -> Any:
    async def heard(count: int) -> speaker_loop.Heard | None:
        turns = [Turn(voice, idx * 10_000, idx * 10_000 + 9_000) for idx, voice in enumerate(voices)]
        return speaker_loop.Heard(turns, same or [])

    return speaker_loop.Rerun(voiceprints=voiceprints, run=heard)


def looped(
    url: str, recording: UUID, model: Scripted, rerun: Any, run: UUID | None = None
) -> tuple[speaker_loop.Outcome, list[str]]:
    stages: list[str] = []

    async def progress(stage: str) -> None:
        stages.append(stage)

    async def go() -> speaker_loop.Outcome:
        bound = engine(url)
        try:
            tiers = Tiers(polish=model, command=model, fallback=None)  # type: ignore[arg-type]
            return await speaker_loop.run(tiers, sessions(bound), recording, run or uuid4(), None, rerun, progress)
        finally:
            await bound.dispose()

    return asyncio.run(go()), stages


async def read(url: str, recording: UUID) -> tuple[dict[UUID, Speaker], dict[UUID, UUID], speaker_loop.Stored]:
    bound = engine(url)
    async with sessions(bound)() as db:
        found = {each.id: each for each in await db.scalars(select(Speaker).where(Speaker.recording_id == recording))}
        lines = {
            line.id: line.speaker_id
            for line in await db.scalars(select(TranscriptLine).where(TranscriptLine.recording_id == recording))
        }
        state = speaker_loop.pass_of(await db.get_one(Recording, recording))
    await bound.dispose()
    return found, lines, state


def test_mom08_a_round_names_merges_and_moves_in_place_and_stores_each_stage(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    outcome, stages = looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    found, lines, state = asyncio.run(read(database, recording))

    assert stages == ["reading", "listening", "naming"]
    assert (state.listened, state.failed) == (True, False)
    assert (found[speaker[1]].name, found[speaker[1]].name_source) == ("Joanne", "guessed")
    assert (found[speaker[2]].name, found[speaker[2]].name_source) == ("Noah", "guessed")
    # The user's "Ian" stays theirs; Speaker 4 joined Speaker 2 and is gone, its line with it.
    assert (found[speaker[3]].name, found[speaker[3]].name_source) == ("Ian", "user")
    assert found[speaker[4]].deleted_at is not None and lines[line[6]] == speaker[2]
    # Line 8 moved to Joanne: the model and the rerun agree.
    assert lines[line[8]] == speaker[1]
    assert outcome.merges == {4: 2} and outcome.moved == {line[8]}
    kinds = sorted(change.kind for change in state.changes if change.state == "applied")
    assert (state.rev, state.round, kinds) == (1, 1, ["merge", "move", "name", "name"])


def test_mom08_a_line_the_user_edited_never_moves_and_without_a_rerun_only_names_change(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database, edited=8))
    outcome, stages = looped(database, recording, Scripted(ANSWER), None)
    found, lines, state = asyncio.run(read(database, recording))
    assert stages == ["reading", "naming"]
    # No rerun: the story never says it listened again.
    assert state.listened is False
    assert lines[line[8]] == speaker[2] and found[speaker[4]].deleted_at is None
    assert [change.kind for change in state.changes] == ["name", "name"]
    assert outcome.merges == {} and outcome.moved == set()


def test_mom08_each_applied_change_is_undone_where_the_user_hasn_t_changed_it_since(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))

    async def undo_all() -> None:
        bound = engine(database)
        async with sessions(bound)() as db:
            for change in state.changes:
                if change.state == "applied":
                    await speaker_loop.undo(db, change)
            await db.commit()
        await bound.dispose()

    asyncio.run(undo_all())
    found, lines, _ = asyncio.run(read(database, recording))
    assert [found[speaker[n]].name for n in (1, 2, 3, 4)] == [None, None, "Ian", None]
    assert found[speaker[4]].deleted_at is None
    assert [lines[each] for each in line] == [speaker[n] for n, _ in SAID]


@pytest.mark.parametrize("count", [0, 1, 4])
def test_mom08_no_rerun_unless_the_count_is_fewer_voices_than_speakers_and_at_least_two(
    database: str, count: int
) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    asked: list[int] = []

    async def rerun(voices: int) -> speaker_loop.Heard | None:
        asked.append(voices)
        return None

    looped(database, recording, Scripted({**ANSWER, "count": count}), speaker_loop.Rerun(voiceprints=False, run=rerun))
    assert asked == []


def settled(url: str, recording: UUID, change_id: str, action: str) -> SpeakerPass:
    async def go() -> SpeakerPass:
        bound = engine(url)
        async with sessions(bound)() as db:
            state, _ = await speaker_loop.settle_change(db, recording, change_id, action)
            await db.commit()
        await bound.dispose()
        return state

    return asyncio.run(go())


def test_mom08_a_suggested_name_accepted_is_the_user_s_and_one_rejected_stays_unnamed(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    # One cue only: a suggestion.
    unsure = {"count": 0, "people": [{"p": 1, "name": "Joanne", "cues": [{"i": 2, "quote": "Thanks Joanne"}]}]}
    looped(database, recording, Scripted(unsure), None)
    _, _, state = asyncio.run(read(database, recording))
    [offered] = state.changes
    assert offered.state == "suggested"

    taken = settled(database, recording, offered.id, "accept")
    found, _, _ = asyncio.run(read(database, recording))
    assert (found[speaker[1]].name, found[speaker[1]].name_source) == ("Joanne", "user")
    assert (taken.rev, taken.changes[0].state) == (state.rev + 1, "applied")
    with pytest.raises(CloudFailure):
        settled(database, recording, offered.id, "reject")


def test_mom08_undo_all_takes_back_every_applied_change_and_moves_rev(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))

    async def go() -> SpeakerPass:
        bound = engine(database)
        async with sessions(bound)() as db:
            state = await speaker_loop.undo_all(db, recording)
            await db.commit()
        await bound.dispose()
        return state

    state = asyncio.run(go())
    found, lines, _ = asyncio.run(read(database, recording))
    assert {change.state for change in state.changes} == {"undone"} and state.rev == 2
    assert found[speaker[4]].deleted_at is None and lines[line[8]] == speaker[2]


def test_mom08_the_model_sees_when_each_speaker_talks_so_a_voice_split_in_two_shows() -> None:
    sent: list[dict[str, Any]] = []

    class Seeing(Scripted):
        async def complete(
            self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = ""
        ) -> Any:
            sent.append(json.loads(turns[1][1]))
            return await super().complete(turns, max_tokens, timeout, reason)

    first, second = (
        Speaker(id=uuid4(), recording_id=uuid4(), label=f"Speaker {n}", vendor_label=f"S{n}") for n in (1, 2)
    )
    said = [(first, "Hello there."), (first, "And more."), (second, "Hi."), (first, "Bye.")]
    lines = [
        TranscriptLine(
            id=uuid4(),
            recording_id=first.recording_id,
            speaker_id=who.id,
            idx=n,
            start_ms=n,
            end_ms=n + 1,
            text=text,
            words=[],
            rev=1,
        )
        for n, (who, text) in enumerate(said)
    ]
    model = Seeing()
    tiers = Tiers(polish=model, command=model, fallback=None)  # type: ignore[arg-type]
    asyncio.run(speaker_loop.proposed(tiers, lines, {first.id: first, second.id: second}, None, []))
    assert sent[0]["speakers"] == [
        {"p": 1, "sname": "Speaker 1", "locked": False, "lines": 3, "first": 0, "last": 3},
        {"p": 2, "sname": "Speaker 2", "locked": False, "lines": 1, "first": 2, "last": 2},
    ]


def test_mom08_with_voiceprints_a_voice_split_in_two_is_joined_though_the_model_never_said_so(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    unaware = {**ANSWER, "count": 0, "people": ANSWER["people"][:1], "moves": []}
    one_voice = [frozenset({speaker[2], speaker[4]})]
    outcome, stages = looped(database, recording, Scripted(unaware), rerun_at(VOICES, one_voice, voiceprints=True))
    found, lines, _ = asyncio.run(read(database, recording))
    assert stages == ["reading", "listening", "naming"]
    assert found[speaker[4]].deleted_at is not None and lines[line[6]] == speaker[2]
    assert outcome.merges == {4: 2}


def run_sql(url: str, statement: str, **params: Any) -> None:
    async def go() -> None:
        bound = engine(url)
        async with sessions(bound)() as db:
            await db.execute(text(statement), params)
            await db.commit()
        await bound.dispose()

    asyncio.run(go())


# Line 4 is Joanne's voice too: two of the nine lines move, a big change.
MOVED_TWO = [*VOICES[:4], "JOANNE", *VOICES[5:]]
BIG = {**ANSWER, "moves": [*ANSWER["moves"], {"i": 4, "to": 1, "cue": {"i": 4, "quote": "Sure, will do."}}]}


def test_mom08_a_big_change_runs_a_second_round_and_only_one(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    model = Scripted(BIG, ANSWER, ANSWER)
    looped(database, recording, model, rerun_at(MOVED_TWO))
    _, _, state = asyncio.run(read(database, recording))
    assert (model.asked, state.round) == (2, 2)


def test_mom08_a_user_s_answer_landing_during_round_two_stays(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))

    class Undoing(Scripted):
        async def complete(
            self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = ""
        ) -> Any:
            if self.asked == 1:
                # While round 2's model runs, the user undoes Joanne's name.
                _, _, state = await asyncio.to_thread(read_now, database, recording)
                [joanne] = [each for each in state.changes if each.name == "Joanne"]
                await asyncio.to_thread(settled, database, recording, joanne.id, "undo")
            return await super().complete(turns, max_tokens, timeout, reason)

    looped(database, recording, Undoing(BIG), rerun_at(MOVED_TWO))
    found, _, state = asyncio.run(read(database, recording))
    [joanne] = [each for each in state.changes if each.name == "Joanne"]
    assert (joanne.state, found[speaker[1]].name) == ("undone", None)
    # Round 1 and the undo each moved it once; round 2 changed nothing.
    assert (state.rev, state.round) == (2, 2)


def read_now(url: str, recording: UUID) -> tuple[dict[UUID, Speaker], dict[UUID, UUID], SpeakerPass]:
    return asyncio.run(read(url, recording))


def test_mom08_a_speaker_the_user_names_while_the_round_runs_is_never_merged_away(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))

    async def heard(count: int) -> speaker_loop.Heard | None:
        # The rerun takes a while; meanwhile the user names Speaker 4.
        await asyncio.to_thread(
            run_sql,
            database,
            "UPDATE speakers SET name = 'Noa', name_source = 'user' WHERE id = :id",
            id=speaker[4],
        )
        turns = [Turn(voice, idx * 10_000, idx * 10_000 + 9_000) for idx, voice in enumerate(VOICES)]
        return speaker_loop.Heard(turns)

    looped(database, recording, Scripted(ANSWER), speaker_loop.Rerun(voiceprints=False, run=heard))
    found, lines, state = asyncio.run(read(database, recording))
    assert found[speaker[4]].deleted_at is None and lines[line[6]] == speaker[4]
    assert "merge" not in [change.kind for change in state.changes]


def test_mom08_undo_leaves_what_the_user_changed_since(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    # Since the loop: the user renamed Joanne and edited the moved line 8.
    run_sql(database, "UPDATE speakers SET name = 'Jo', name_source = 'user' WHERE id = :id", id=speaker[1])
    run_sql(database, "UPDATE transcript_lines SET rev = 2 WHERE id = :id", id=line[8])

    async def go() -> None:
        bound = engine(database)
        async with sessions(bound)() as db:
            await speaker_loop.undo_all(db, recording)
            await db.commit()
        await bound.dispose()

    asyncio.run(go())
    found, lines, _ = asyncio.run(read(database, recording))
    assert (found[speaker[1]].name, lines[line[8]]) == ("Jo", speaker[1])
    # What the user left alone is undone: Noah's name, and Speaker 4 back with its line.
    assert found[speaker[2]].name is None and lines[line[6]] == speaker[4]


def test_mom08_a_resumed_run_asks_nothing_again_and_still_knows_what_it_changed(database: str) -> None:
    recording, _, line = asyncio.run(seeded(database))
    run = uuid4()
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES), run)
    again = Scripted(ANSWER)
    outcome, _ = looped(database, recording, again, rerun_at(VOICES), run)
    assert again.asked == 0
    assert (outcome.merges, outcome.moved) == ({4: 2}, {line[8]})
    # Past rewriting, the moved lines' sections were already stored pending: nothing is left to write again.
    run_sql(database, "UPDATE recordings SET speaker_pass = jsonb_set(speaker_pass, '{stage}', '\"rewriting\"')")
    outcome, _ = looped(database, recording, Scripted(ANSWER), rerun_at(VOICES), run)
    assert (outcome.merges, outcome.moved) == ({4: 2}, set())


def test_mom08_a_suggestion_for_a_speaker_the_user_named_since_cannot_be_accepted(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    unsure = {"count": 0, "people": [{"p": 1, "name": "Joanne", "cues": [{"i": 2, "quote": "Thanks Joanne"}]}]}
    looped(database, recording, Scripted(unsure), None)
    _, _, state = asyncio.run(read(database, recording))
    run_sql(database, "UPDATE speakers SET name = 'Jo', name_source = 'user' WHERE id = :id", id=speaker[1])
    with pytest.raises(CloudFailure) as refused:
        settled(database, recording, state.changes[0].id, "accept")
    assert refused.value.code == "change_state"
    found, _, _ = asyncio.run(read(database, recording))
    assert found[speaker[1]].name == "Jo"


class Minutes(Scripted):
    """The meetings fake for the minutes, a script for the speakers call, and a summary naming whoever thanked Ian
    (Speaker 4 before the merge), so a retold token shows."""

    def __init__(self, *answers: dict[str, Any]) -> None:
        super().__init__(*answers)
        self.fake = Fake("meetings")
        self.summaries = 0

    async def complete(self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = "") -> Any:
        request = json.loads(
            next(content for role, content in reversed(turns) if role == "user" and content[:1] == "{")
        )
        if request.get("section") == "speakers":
            return await super().complete(turns, max_tokens, timeout, reason)
        if request.get("section") == "summary":
            self.summaries += 1
            thanked = next(line["p"] for line in request["lines"] if line["text"] == "Thank you Ian.")
            said = {"title": "Housewarming", "summary": f"{{{{p:{thanked}}}}} thanked Ian."}
            return json.dumps(said), Usage(0, 0)
        return await self.fake.complete(turns, max_tokens, timeout, reason)


class Quiet:
    """Redis as the worker uses it here: publishes and queued tasks counted, never sent."""

    def __init__(self) -> None:
        self.queued: list[str] = []
        self.ids: list[str] = []

    async def publish(self, channel: str, message: Any) -> None:
        pass

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.queued.append(name)
        self.ids.append(kwargs.get("_job_id", ""))


async def with_meeting(url: str, recording: UUID, job_state: str) -> tuple[UUID, UUID, UUID]:
    """A transcription job in `job_state` for the recording, and a meeting generating under a run's token."""
    bound = engine(url)
    async with sessions(bound)() as db:
        job = TranscriptionJob(
            id=uuid4(), recording_id=recording, state=job_state, language_requested="en", billed_seconds=60
        )
        run = uuid4()
        meeting = Meeting(id=uuid4(), recording_id=recording, title="t", state="generating", minutes_run=run)
        db.add_all([job, meeting])
        await db.commit()
    await bound.dispose()
    return job.id, meeting.id, run


def worked(url: str, model: Scripted, voices: list[str], task: str, *args: Any) -> Any:
    """One worker task on the test database, its rerun hearing `voices`."""

    async def heard(*_: Any) -> speaker_loop.Heard:
        turns = [Turn(voice, idx * 10_000, idx * 10_000 + 9_000) for idx, voice in enumerate(voices)]
        return speaker_loop.Heard(turns)

    async def go() -> Any:
        bound = engine(url)
        cloud = SimpleNamespace(sessions=sessions(bound), redis=Quiet(), config=SimpleNamespace(voiceprint_keys={}))
        ctx = {"cloud": cloud, "tiers": Tiers(polish=model, command=model, fallback=None)}  # type: ignore[arg-type]
        try:
            with patch.object(worker, "heard_again", heard):
                return await getattr(worker, task)(ctx, *args)
        finally:
            await bound.dispose()

    return asyncio.run(go())


@pytest.mark.parametrize(("voices", "summaries", "stages"), [([*VOICES[:8], "NOAH"], 1, False), (VOICES, 2, True)])
def test_mom08_the_minutes_run_retells_merges_writes_moved_sections_again_and_ends_done_not_stale(
    database: str, voices: list[str], summaries: int, stages: bool
) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    _, meeting, run = asyncio.run(with_meeting(database, recording, "ready"))
    model = Minutes(ANSWER)
    worked(database, model, voices, "generate_minutes", meeting, run)

    async def meeting_now() -> Meeting:
        bound = engine(database)
        async with sessions(bound)() as db:
            row = await db.get_one(Meeting, meeting)
        await bound.dispose()
        return row

    row = asyncio.run(meeting_now())
    found, _, state = asyncio.run(read(database, recording))
    assert row.state == "ready" and row.minutes is not None
    # Speaker 4 joined Speaker 2: the draft's token for Speaker 4 is Speaker 2's now; a moved line wrote it again.
    assert row.minutes["summary"] == "{{p:2}} thanked Ian." and found[speaker[4]].deleted_at is not None
    assert model.summaries == summaries and state.stage == "done"
    assert row.minutes_transcript_version == row.transcript_version
    assert ("move" in [change.kind for change in state.changes]) is stages


def test_mom08_a_studio_transcript_is_ready_after_its_speaker_loop(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    job, _, _ = asyncio.run(with_meeting(database, recording, "processing"))
    won = worked(database, Scripted(ANSWER), VOICES, "identified_then_ready", job, recording)
    found, _, state = asyncio.run(read(database, recording))
    assert won is True and state.stage == "done"
    assert found[speaker[1]].name == "Joanne"


def test_mom06_every_reader_that_prints_words_names_people_as_they_are_now(database: str) -> None:
    """The share page, Ask's notes and the search sections read tokens as names, never `{{` (D41)."""
    recording, speaker, _ = asyncio.run(seeded(database))
    run_sql(database, "UPDATE speakers SET name = 'Joane', name_source = 'user' WHERE id = :id", id=speaker[1])
    doc = {
        "summary": "{{p:1}} thanked {{p:2}} and {{n:Rabbi Eden}}.",
        "mom": "### Gifts\n\n{{p:1}} got the mezuzah.",
        "openQuestions": [],
        "decisions": [],
        "actionItems": [{"id": "a", "text": "{{p:2}} hangs it", "owner": "{{p:1}}", "derivedFrom": []}],
    }

    async def go() -> tuple[str, list[str], list[str]]:
        bound = engine(database)
        cloud = SimpleNamespace(sessions=sessions(bound), embeddings=HashedEmbeddings())
        async with sessions(bound)() as db:
            meeting = Meeting(
                id=uuid4(),
                recording_id=recording,
                title="t",
                state="ready",
                minutes=doc,
                minutes_generated_at=datetime.now(UTC),
            )
            share = Share(id=uuid4(), recording_id=recording, slug=uuid4().hex, title="t", type="meeting")
            db.add_all([meeting, share])
            await db.commit()
            page, _, _ = await share_page.sections_of(db, share)
            asked = await qa.gather(db, meeting, "who got the mezuzah?")
        await worker.embed_minutes_task({"cloud": cloud, "job_try": 1}, meeting.id)
        async with sessions(bound)() as db:
            chunks = list(await db.scalars(select(MeetingChunk.text).where(MeetingChunk.meeting_id == meeting.id)))
        await bound.dispose()
        return page, asked.notes, chunks

    page, notes, chunks = asyncio.run(go())
    for words in (page, " ".join(notes), " ".join(chunks)):
        assert "Joane thanked Speaker 2 and Rabbi Eden." in words and "{{" not in words
    assert any("Speaker 2 hangs it owner Joane" in chunk for chunk in chunks)


def test_mom08_undoing_a_move_writes_again_the_sections_it_touched_and_embeds_the_names(database: str) -> None:
    recording, _, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))
    doc = {
        "summary": "s",
        "mom": "m",
        "openQuestions": [],
        "decisions": [],
        "actionItems": [
            {
                "id": "a",
                "text": "t",
                "owner": None,
                "derivedFrom": [{"lineId": str(line[8]), "speaker": "S", "atMs": 0, "quote": "q"}],
            }
        ],
    }

    async def go() -> dict[str, Any]:
        bound = engine(database)
        async with sessions(bound)() as db:
            meeting = Meeting(id=uuid4(), recording_id=recording, title="t", state="ready", minutes=doc)
            db.add(meeting)
            await db.commit()
            state = speaker_loop.pass_of(await db.get_one(Recording, recording))
            [move] = [each for each in state.changes if each.kind == "move"]
            _, settled = await speaker_loop.settle_change(db, recording, move.id, "undo")
            await db.commit()
            await speakers_api.rewritten(db, request, recording, [settled])  # type: ignore[arg-type]
            await db.refresh(meeting)
            minutes = meeting.minutes or {}
        await bound.dispose()
        return minutes

    minutes = asyncio.run(go())
    # The line went back: the summary, the minutes and the action items citing it are written again.
    assert minutes["pending"] == ["summary", "mom", "actionItems"]
    assert redis.queued == ["embed_minutes_task", "generate_minutes"]


def test_mom08_a_merge_the_user_undid_before_the_retell_is_never_retold(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [merge] = [each for each in state.changes if each.kind == "merge"]
    settled(database, recording, merge.id, "undo")

    async def go() -> speaker_loop.Outcome:
        bound = engine(database)
        async with sessions(bound)() as db:
            made = await speaker_loop.outcome_of(db, recording)
        await bound.dispose()
        return made

    assert asyncio.run(go()).merges == {}


def test_mom08_a_run_after_the_loop_finished_never_runs_it_again_unless_a_whole_run_asks(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    run_sql(database, "UPDATE recordings SET speaker_pass = jsonb_set(speaker_pass, '{stage}', '\"done\"')")
    model = Scripted(ANSWER)

    async def again() -> Any:
        bound = engine(database)
        cloud = SimpleNamespace(sessions=sessions(bound), redis=Quiet(), config=SimpleNamespace(voiceprint_keys={}))
        tiers = Tiers(polish=model, command=model, fallback=None)  # type: ignore[arg-type]
        ctx = {"cloud": cloud, "tiers": tiers, "strategy": Vendor()}
        try:
            return await worker.identified(ctx, recording, None, uuid4(), None)
        finally:
            await bound.dispose()

    # A sections-only rewrite (after an undo) finds the loop done: it asks nothing and changes nothing.
    assert asyncio.run(again()) is None and model.asked == 0


def test_mom08_a_loop_run_again_never_brings_back_what_the_user_undid_or_turned_down(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [merge] = [each for each in state.changes if each.kind == "merge"]
    [move] = [each for each in state.changes if each.kind == "move"]
    [joanne] = [each for each in state.changes if each.name == "Joanne"]
    for change in (move, merge, joanne):
        settled(database, recording, change.id, "undo")

    # Regenerate: a whole new run, the same evidence.
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    found, lines, state = asyncio.run(read(database, recording))
    assert found[speaker[4]].deleted_at is None and lines[line[8]] == speaker[2]
    assert found[speaker[1]].name is None
    assert [each.state for each in state.changes if each.name == "Joanne"] == ["undone"]


def test_mom08_a_suggestion_turned_down_leaves_the_minutes_current(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    unsure = {"count": 0, "people": [{"p": 1, "name": "Joanne", "cues": [{"i": 2, "quote": "Thanks Joanne"}]}]}
    looped(database, recording, Scripted(unsure), None)
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))

    async def go() -> int:
        bound = engine(database)
        async with sessions(bound)() as db:
            owner = await db.get_one(Recording, recording)
            meeting = Meeting(id=uuid4(), recording_id=recording, title="t", state="ready")
            db.add(meeting)
            await db.commit()
            state = speaker_loop.pass_of(owner)
            device = SimpleNamespace(id=owner.device_id)
            body = SpeakerChangeRequest(action="reject")
            await speakers_api.change_speakers(recording, state.changes[0].id, body, device, db, request)  # type: ignore[arg-type]
            await db.refresh(meeting)
            version = meeting.transcript_version
        await bound.dispose()
        return version

    assert asyncio.run(go()) == 0 and redis.queued == []


def test_mom08_identify_on_a_meeting_runs_the_loop_and_writes_no_section_it_did_not_change(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    _, meeting, run = asyncio.run(with_meeting(database, recording, "ready"))
    worked(database, Minutes(ANSWER), VOICES, "generate_minutes", meeting, run)
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))

    async def identify() -> UUID:
        bound = engine(database)
        async with sessions(bound)() as db:
            owner = await db.get_one(Recording, recording)
            device = SimpleNamespace(id=owner.device_id)
            await speakers_api.identify_speakers(recording, device, db, request)  # type: ignore[arg-type]
            row = await db.get_one(Meeting, meeting)
            await db.refresh(row)
            token = row.minutes_run
        await bound.dispose()
        assert token is not None
        return token

    again = asyncio.run(identify())
    assert redis.queued == ["generate_minutes"]
    model = Minutes({"count": 0, "people": [], "moves": []})
    worked(database, model, VOICES, "generate_minutes", meeting, again)
    _, _, state = asyncio.run(read(database, recording))
    # The loop ran (asked once), found nothing new, and no section was written again.
    assert (model.asked, model.summaries, state.stage) == (1, 0, "done")


def test_mom08_undoing_a_merge_brings_back_every_line_it_moved_edited_ones_too(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database, edited=6))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [merge] = [each for each in state.changes if each.kind == "merge"]
    assert line[6] in merge.lineIds
    settled(database, recording, merge.id, "undo")
    _, lines, _ = asyncio.run(read(database, recording))
    assert lines[line[6]] == speaker[4]


def test_mom08_a_merge_whose_kept_speaker_was_merged_since_cannot_be_undone_alone(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [merge] = [each for each in state.changes if each.kind == "merge"]
    # Since, Speaker 2 (the one kept) went too.
    run_sql(database, "UPDATE speakers SET deleted_at = now() WHERE id = :id", id=speaker[2])
    with pytest.raises(CloudFailure) as refused:
        settled(database, recording, merge.id, "undo")
    assert refused.value.code == "change_later"


def test_mom08_a_speaker_loop_that_breaks_never_holds_back_the_minutes(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    _, meeting, run = asyncio.run(with_meeting(database, recording, "ready"))

    async def broken(*_: Any) -> None:
        raise RuntimeError("the loop broke")

    with patch.object(worker, "identified", broken):
        worked(database, Minutes(ANSWER), VOICES, "generate_minutes", meeting, run)

    async def state_of() -> str:
        bound = engine(database)
        async with sessions(bound)() as db:
            row = await db.get_one(Meeting, meeting)
        await bound.dispose()
        return row.state

    assert asyncio.run(state_of()) == "ready"
    _, _, state = asyncio.run(read(database, recording))
    # Said as broken, not as "nothing found".
    assert (state.stage, state.failed) == ("done", True)


class Vendor:
    """pyannote for the rerun: counts the jobs started, answers at once, or fails to start (`fails`)."""

    def __init__(self, fails: bool = False, cancelled: bool = False) -> None:
        self.started = 0
        self.fails = fails
        self.cancelled = cancelled

    async def rediarize(self, audio_url: str, count: int) -> str:
        if self.cancelled:
            raise asyncio.CancelledError
        if self.fails:
            raise VendorError("down")
        self.started += 1
        return f"job-{self.started}"

    async def rediarization(self, vendor_job: str) -> list[dict[str, Any]]:
        return [{"speaker": voice, "start": idx * 10.0, "end": idx * 10.0 + 9} for idx, voice in enumerate(VOICES)]


async def with_upload(url: str, recording: UUID, used: int) -> None:
    """The recording's audio uploaded, and `used` seconds of the device's day spent already."""
    bound = engine(url)
    async with sessions(bound)() as db:
        owner = await db.get_one(Recording, recording)
        upload = Upload(
            id=uuid4(),
            recording_id=recording,
            kind="audio",
            s3_key="a.m4a",
            upload_id="u",
            part_size=5,
            bytes=5,
            content_type="audio/mp4",
            state="complete",
        )
        db.add(upload)
        await db.flush()
        job = TranscriptionJob(
            id=uuid4(),
            recording_id=recording,
            upload_id=upload.id,
            state="ready",
            language_requested="en",
            billed_seconds=60,
        )
        db.add(job)
        await db.execute(text("DELETE FROM usage_daily"))
        if used:
            await usage.reserve(db, owner.device_id, used)
        await db.commit()
    await bound.dispose()


def reheard(url: str, recording: UUID, vendor: Vendor, run: UUID) -> Any:
    async def go() -> Any:
        bound = engine(url)
        storage = SimpleNamespace(presign_get=lambda key, seconds: f"https://audio/{key}")
        cloud = SimpleNamespace(sessions=sessions(bound), storage=storage)
        try:
            return await worker.heard_again({"cloud": cloud, "strategy": vendor}, recording, run, {}, 2)
        finally:
            await bound.dispose()

    return asyncio.run(go())


def seconds_used(url: str) -> int:
    async def go() -> int:
        bound = engine(url)
        async with sessions(bound)() as db:
            used = await db.scalar(text("SELECT coalesce(sum(audio_seconds), 0) FROM usage_daily"))
        await bound.dispose()
        return int(used or 0)

    return asyncio.run(go())


def test_mom08_a_rerun_is_paid_once_a_run_and_a_retry_reuses_its_vendor_job(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    asyncio.run(with_upload(database, recording, 0))
    vendor, run = Vendor(), uuid4()
    first = reheard(database, recording, vendor, run)
    again = reheard(database, recording, vendor, run)
    assert first is not None and again is not None
    # One vendor job and one charge of the recording's minute, whatever the retries.
    assert (vendor.started, seconds_used(database)) == (1, 60)


def test_mom08_no_rerun_without_the_day_s_audio_left_and_a_vendor_that_fails_charges_nothing(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    asyncio.run(with_upload(database, recording, DAILY_SECONDS))
    vendor = Vendor()
    assert reheard(database, recording, vendor, uuid4()) is None and vendor.started == 0
    run_sql(database, "DELETE FROM usage_daily")
    assert reheard(database, recording, Vendor(fails=True), uuid4()) is None
    assert seconds_used(database) == 0


def test_mom08_a_rerun_cancelled_as_it_starts_gives_the_day_s_audio_back_and_stops(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    asyncio.run(with_upload(database, recording, 0))
    with pytest.raises(asyncio.CancelledError):
        reheard(database, recording, Vendor(cancelled=True), uuid4())
    assert seconds_used(database) == 0


def test_mom08_a_line_the_user_edits_while_the_round_runs_never_moves(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))

    async def heard(count: int) -> speaker_loop.Heard | None:
        # The rerun takes a while; meanwhile the user fixes line 8's words.
        await asyncio.to_thread(run_sql, database, "UPDATE transcript_lines SET rev = 2 WHERE id = :id", id=line[8])
        turns = [Turn(voice, idx * 10_000, idx * 10_000 + 9_000) for idx, voice in enumerate(VOICES)]
        return speaker_loop.Heard(turns)

    looped(database, recording, Scripted(ANSWER), speaker_loop.Rerun(voiceprints=False, run=heard))
    _, lines, state = asyncio.run(read(database, recording))
    assert lines[line[8]] == speaker[2]
    assert "move" not in [change.kind for change in state.changes]


def test_mom08_a_studio_loop_that_breaks_still_ends_done_and_the_transcript_goes_ready(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    job, _, _ = asyncio.run(with_meeting(database, recording, "processing"))

    async def broken(*_: Any) -> None:
        raise RuntimeError("the loop broke")

    with patch.object(worker, "identified", broken):
        won = worked(database, Scripted(ANSWER), VOICES, "identified_then_ready", job, recording)
    _, _, state = asyncio.run(read(database, recording))
    assert won is True and (state.stage, state.failed) == ("done", True)


def test_mom05_a_run_that_writes_only_some_sections_leaves_minutes_stale_from_the_user_s_edits(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    _, meeting, run = asyncio.run(with_meeting(database, recording, "ready"))
    worked(database, Minutes(ANSWER), VOICES, "generate_minutes", meeting, run)
    # The user edits a line: the minutes are stale.
    run_sql(database, "UPDATE meetings SET transcript_version = transcript_version + 1")
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))

    async def identify() -> tuple[UUID, int, int]:
        bound = engine(database)
        async with sessions(bound)() as db:
            owner = await db.get_one(Recording, recording)
            await speakers_api.identify_speakers(recording, SimpleNamespace(id=owner.device_id), db, request)  # type: ignore[arg-type]
            row = await db.get_one(Meeting, meeting)
            await db.refresh(row)
            made = (row.minutes_run, row.transcript_version, row.minutes_transcript_version)
        await bound.dispose()
        assert made[0] is not None and made[2] is not None
        return made[0], made[1], made[2]

    again, _, _ = asyncio.run(identify())
    worked(database, Minutes({"count": 0, "people": [], "moves": []}), VOICES, "generate_minutes", meeting, again)

    async def versions() -> tuple[int, int | None]:
        bound = engine(database)
        async with sessions(bound)() as db:
            row = await db.get_one(Meeting, meeting)
        await bound.dispose()
        return row.transcript_version, row.minutes_transcript_version

    current, written = asyncio.run(versions())
    # Identify wrote no section: what made the minutes stale is still not in them.
    assert written is not None and written < current


def test_mom08_a_loop_picked_up_under_a_new_token_carries_on_under_its_own(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    first = uuid4()
    stored = {"rev": 0, "stage": "listening", "round": 0, "changes": [], "run": str(first), "start": 0}
    run_sql(database, "UPDATE recordings SET speaker_pass = CAST(:pass AS jsonb)", **{"pass": json.dumps(stored)})
    model = Scripted(ANSWER)

    async def pick_up() -> Any:
        bound = engine(database)
        cloud = SimpleNamespace(sessions=sessions(bound), redis=Quiet(), config=SimpleNamespace(voiceprint_keys={}))
        tiers = Tiers(polish=model, command=model, fallback=None)  # type: ignore[arg-type]
        ctx = {"cloud": cloud, "tiers": tiers, "strategy": Vendor()}
        try:
            return await worker.identified(ctx, recording, None, uuid4(), None)
        finally:
            await bound.dispose()

    # The sweep hands the run a new token; the loop it found mid-way goes on as the same run (its rerun reused).
    asyncio.run(pick_up())
    assert asyncio.run(stored_run(database, recording)) == str(first)


async def stored_run(url: str, recording: UUID) -> str | None:
    bound = engine(url)
    async with sessions(bound)() as db:
        made = speaker_loop.pass_of(await db.get_one(Recording, recording)).run
    await bound.dispose()
    return made


def test_mom08_a_suggestion_still_waiting_is_never_offered_twice(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    unsure = {"count": 0, "people": [{"p": 1, "name": "Joanne", "cues": [{"i": 2, "quote": "Thanks Joanne"}]}]}
    looped(database, recording, Scripted(unsure), None)
    looped(database, recording, Scripted(unsure), None)
    _, _, state = asyncio.run(read(database, recording))
    assert [change.state for change in state.changes] == ["suggested"]


def test_mom08_a_move_whose_speaker_was_merged_since_cannot_be_undone_alone(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [move] = [each for each in state.changes if each.kind == "move"]
    run_sql(database, "UPDATE speakers SET deleted_at = now() WHERE id = :id", id=speaker[1])
    with pytest.raises(CloudFailure) as refused:
        settled(database, recording, move.id, "undo")
    assert refused.value.code == "change_later"


@pytest.mark.parametrize("asked", ["undo", "undo all", "redo"])
def test_mom08_a_merge_or_move_is_not_undone_or_redone_while_the_minutes_are_being_written(
    database: str, asked: str
) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    asyncio.run(with_meeting(database, recording, "ready"))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [merge] = [each for each in state.changes if each.kind == "merge"]
    if asked == "redo":
        settled(database, recording, merge.id, "undo")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=Quiet()))))

    async def answer() -> None:
        bound = engine(database)
        try:
            async with sessions(bound)() as db:
                owner = await db.get_one(Recording, recording)
                device = SimpleNamespace(id=owner.device_id)
                if asked == "undo all":
                    await speakers_api.undo_speakers(recording, device, db, request)  # type: ignore[arg-type]
                else:
                    body = SpeakerChangeRequest(action=asked)  # type: ignore[arg-type]
                    await speakers_api.change_speakers(recording, merge.id, body, device, db, request)  # type: ignore[arg-type]
        finally:
            await bound.dispose()

    # The meeting is still `generating` (with_meeting): the sections could not be written again now, by one undo,
    # by Undo all, or by a redo.
    with pytest.raises(CloudFailure) as refused:
        asyncio.run(answer())
    assert refused.value.code == "minutes_busy"
    _, _, after = asyncio.run(read(database, recording))
    [still] = [each for each in after.changes if each.id == merge.id]
    assert still.state == ("undone" if asked == "redo" else "applied")


def test_mom08_identify_waits_for_ready_minutes_and_one_loop_at_a_time(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    asyncio.run(with_meeting(database, recording, "ready"))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=Quiet()))))

    async def identify() -> None:
        bound = engine(database)
        try:
            async with sessions(bound)() as db:
                owner = await db.get_one(Recording, recording)
                await speakers_api.identify_speakers(recording, SimpleNamespace(id=owner.device_id), db, request)  # type: ignore[arg-type]
        finally:
            await bound.dispose()

    # The meeting's minutes are still being written: no minutes to work the loop on yet.
    with pytest.raises(CloudFailure) as refused:
        asyncio.run(identify())
    assert refused.value.code == "not_ready"
    # A recording that is no meeting, its loop mid-way: a second ask waits.
    run_sql(database, "UPDATE meetings SET deleted_at = now() WHERE recording_id = :id", id=recording)
    stored = {"rev": 0, "stage": "naming", "round": 1, "changes": [], "run": str(uuid4()), "start": 0}
    run_sql(database, "UPDATE recordings SET speaker_pass = CAST(:pass AS jsonb)", **{"pass": json.dumps(stored)})
    with pytest.raises(CloudFailure) as busy:
        asyncio.run(identify())
    assert busy.value.code == "not_ready"


def test_mom05_a_whole_regenerate_picked_up_mid_way_ends_current(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    _, meeting, run = asyncio.run(with_meeting(database, recording, "ready"))
    worked(database, Minutes(ANSWER), VOICES, "generate_minutes", meeting, run)
    run_sql(database, "UPDATE meetings SET transcript_version = transcript_version + 1")
    redis = Quiet()

    async def regenerate() -> UUID:
        bound = engine(database)
        async with sessions(bound)() as db:
            await meetings_service.begin(db, redis, meeting, clicked=True)  # type: ignore[arg-type]
            row = await db.get_one(Meeting, meeting)
            await db.refresh(row)
            token = row.minutes_run
        await bound.dispose()
        assert token is not None
        return token

    again = asyncio.run(regenerate())
    # It died after the summary: picked up with fewer than all sections pending, it is still a whole run.
    run_sql(database, "UPDATE meetings SET minutes = jsonb_set(minutes, '{pending}', '[\"mom\"]')")
    worked(database, Minutes(ANSWER), VOICES, "generate_minutes", meeting, again)

    async def versions() -> tuple[int, int | None]:
        bound = engine(database)
        async with sessions(bound)() as db:
            row = await db.get_one(Meeting, meeting)
        await bound.dispose()
        return row.transcript_version, row.minutes_transcript_version

    current, written = asyncio.run(versions())
    assert written == current


def test_mom08_a_suggestion_for_a_speaker_merged_away_since_cannot_be_accepted(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    unsure = {"count": 0, "people": [{"p": 1, "name": "Joanne", "cues": [{"i": 2, "quote": "Thanks Joanne"}]}]}
    looped(database, recording, Scripted(unsure), None)
    _, _, state = asyncio.run(read(database, recording))
    run_sql(database, "UPDATE speakers SET deleted_at = now() WHERE id = :id", id=speaker[1])
    with pytest.raises(CloudFailure):
        settled(database, recording, state.changes[0].id, "accept")


def test_mom08_a_loop_that_breaks_after_a_round_merged_still_retells_the_draft(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    _, meeting, run = asyncio.run(with_meeting(database, recording, "ready"))

    class BreaksInRoundTwo(Minutes):
        async def complete(
            self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = ""
        ) -> Any:
            if '"section": "speakers"' in turns[-1][1] and self.asked == 1:
                raise RuntimeError("round two broke")
            return await super().complete(turns, max_tokens, timeout, reason)

    # Round 1 merges Speaker 4 into Speaker 2 and moves two lines (a big change); round 2 breaks.
    worked(database, BreaksInRoundTwo(BIG), MOVED_TWO, "generate_minutes", meeting, run)

    async def summary() -> str:
        bound = engine(database)
        async with sessions(bound)() as db:
            row = await db.get_one(Meeting, meeting)
        await bound.dispose()
        return (row.minutes or {}).get("summary", "")

    _, _, state = asyncio.run(read(database, recording))
    assert state.failed is True
    # The merge went in before the break: the draft's Speaker 4 is retold as Speaker 2, never "someone".
    assert asyncio.run(summary()) == "{{p:2}} thanked Ian."


def test_mom08_a_move_whose_source_speaker_was_merged_since_cannot_be_undone_alone(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [move] = [each for each in state.changes if each.kind == "move"]
    # The line moved from Speaker 2 to Speaker 1; since, Speaker 2 went.
    run_sql(database, "UPDATE speakers SET deleted_at = now() WHERE id = :id", id=speaker[2])
    with pytest.raises(CloudFailure) as refused:
        settled(database, recording, move.id, "undo")
    assert refused.value.code == "change_later"


class NoResult:
    """A strategy whose vendors have nothing more to give: a job past its merge never asks them again."""

    async def result(self, refs: Any) -> Any:
        raise AssertionError("the transcript was fetched again")

    async def cleanup(self, refs: Any) -> None:
        pass


def held(url: str, redis: Quiet, task: str, *args: Any) -> None:
    """One worker task on a Studio job held for its loop (stage `identifying`, no meeting)."""

    async def heard(*_: Any) -> speaker_loop.Heard:
        turns = [Turn(voice, idx * 10_000, idx * 10_000 + 9_000) for idx, voice in enumerate(VOICES)]
        return speaker_loop.Heard(turns)

    async def go() -> None:
        bound = engine(url)
        cloud = SimpleNamespace(sessions=sessions(bound), redis=redis, config=SimpleNamespace(voiceprint_keys={}))
        model = Scripted(ANSWER)
        ctx = {
            "cloud": cloud,
            "strategy": NoResult(),
            "job_try": 1,
            "tiers": Tiers(polish=model, command=model, fallback=None),  # type: ignore[arg-type]
        }
        try:
            with patch.object(worker, "heard_again", heard):
                await getattr(worker, task)(ctx, *args)
        finally:
            await bound.dispose()

    asyncio.run(go())


async def identifying(url: str, recording: UUID, late: bool) -> UUID:
    bound = engine(url)
    async with sessions(bound)() as db:
        submitted = datetime(2026, 1, 1, tzinfo=UTC) if late else datetime.now(UTC)
        job = TranscriptionJob(
            id=uuid4(),
            recording_id=recording,
            state="processing",
            stage="identifying",
            language_requested="en",
            billed_seconds=60,
            submitted_at=submitted,
        )
        db.add(job)
        await db.commit()
    await bound.dispose()
    return job.id


async def job_now(url: str, job_id: UUID) -> TranscriptionJob:
    bound = engine(url)
    async with sessions(bound)() as db:
        job = await db.get_one(TranscriptionJob, job_id)
    await bound.dispose()
    return job


def test_mom08_a_merge_that_runs_again_on_a_held_job_keeps_the_transcript_and_finishes_the_loop(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    job = asyncio.run(identifying(database, recording, late=False))
    held(database, Quiet(), "merge_and_store", job)
    found, lines, state = asyncio.run(read(database, recording))
    # The same rows: nothing stored again, and the loop's work is in them.
    assert set(lines) == set(line) and found[speaker[1]].name == "Joanne"
    assert state.stage == "done" and asyncio.run(job_now(database, job)).state == "ready"


def test_mom08_the_timeout_check_leaves_a_job_held_for_its_loop(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    job = asyncio.run(identifying(database, recording, late=True))
    redis = Quiet()
    held(database, redis, "check_vendor_timeout", job, 3)
    after = asyncio.run(job_now(database, job))
    assert (after.state, after.stage, redis.queued) == ("processing", "identifying", [])


def test_mom08_the_sweep_finishes_a_held_job_whose_loop_stopped_instead_of_failing_it(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    # Past the vendors' deadline, but held only now: its loop may still be running, so the sweep leaves it.
    job = asyncio.run(identifying(database, recording, late=True))
    redis = Quiet()
    held(database, redis, "sweep_waiting_jobs")
    assert not [each for each in redis.ids if each.startswith(f"merge:{job}:held")]
    # Held past the loop's own timeout: that loop is over. Its pass ends failed, and the merge runs once more (one
    # arq id per job, so a second sweep never doubles it) to make the transcript ready without asking again.
    run_sql(database, "UPDATE transcription_jobs SET updated_at = now() - interval '1 hour' WHERE id = :id", id=job)
    held(database, redis, "sweep_waiting_jobs")
    after = asyncio.run(job_now(database, job))
    _, _, state = asyncio.run(read(database, recording))
    assert (after.state, after.stage) == ("processing", "identifying")
    assert (state.stage, state.failed) == ("done", True) and any(
        each.startswith(f"merge:{job}:held:") for each in redis.ids
    )


def test_mom08_an_undone_change_can_be_redone_where_the_transcript_still_allows_it(database: str) -> None:
    recording, speaker, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    by_kind = {change.kind: change for change in state.changes if change.speakerId in (speaker[1], speaker[4])}
    for change in by_kind.values():
        settled(database, recording, change.id, "undo")
    # Redone: Joanne's name is a guess again, and Speaker 4 joins Speaker 2 again with its line.
    for change in by_kind.values():
        settled(database, recording, change.id, "redo")
    found, lines, after = asyncio.run(read(database, recording))
    assert (found[speaker[1]].name, found[speaker[1]].name_source) == ("Joanne", "guessed")
    assert found[speaker[4]].deleted_at is not None and lines[line[6]] == speaker[2]
    assert {each.state for each in after.changes if each.id in {c.id for c in by_kind.values()}} == {"applied"}
    # Undone and then renamed by the user: theirs wins, and the redo is the wrong state.
    settled(database, recording, by_kind["name"].id, "undo")
    run_sql(database, "UPDATE speakers SET name = 'Jo', name_source = 'user' WHERE id = :id", id=speaker[1])
    # It is gone for good: stored so, its Redo never offered again.
    after = settled(database, recording, by_kind["name"].id, "redo")
    assert [each.state for each in after.changes if each.id == by_kind["name"].id] == ["gone"]
    # Only an undone change is redone.
    with pytest.raises(CloudFailure):
        settled(database, recording, by_kind["merge"].id, "redo")


def test_mom08_redoing_a_merge_retells_its_speaker_s_tokens_without_writing_again(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [merge] = [each for each in state.changes if each.kind == "merge"]
    settled(database, recording, merge.id, "undo")
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))
    doc = {"summary": "{{p:4}} thanked Ian.", "mom": "m", "openQuestions": [], "decisions": [], "actionItems": []}

    async def go() -> dict[str, Any]:
        bound = engine(database)
        async with sessions(bound)() as db:
            meeting = Meeting(id=uuid4(), recording_id=recording, title="t", state="ready", minutes=doc)
            db.add(meeting)
            await db.commit()
            owner = await db.get_one(Recording, recording)
            body = SpeakerChangeRequest(action="redo")
            device = SimpleNamespace(id=owner.device_id)
            await speakers_api.change_speakers(recording, merge.id, body, device, db, request)  # type: ignore[arg-type]
            await db.refresh(meeting)
            minutes = meeting.minutes or {}
        await bound.dispose()
        return minutes

    # Speaker 4 joined Speaker 2 again: their token is Speaker 2's, as the loop retold it, and no model is asked.
    minutes = asyncio.run(go())
    assert (minutes["summary"], minutes.get("pending")) == ("{{p:2}} thanked Ian.", [])
    assert "generate_minutes" not in redis.queued


def test_mom08_a_held_job_whose_loop_ended_failed_goes_ready_without_asking_again(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    job = asyncio.run(identifying(database, recording, late=False))
    # The loop died inside round 1 and the sweep ended it failed, under the job's own run.
    stopped = {"rev": 0, "stage": "done", "round": 0, "failed": True, "run": str(job), "changes": []}
    run_sql(
        database,
        "UPDATE recordings SET speaker_pass = CAST(:p AS jsonb) WHERE id = :id",
        p=json.dumps(stopped),
        id=recording,
    )
    held(database, Quiet(), "merge_and_store", job)
    found, _, _ = asyncio.run(read(database, recording))
    assert asyncio.run(job_now(database, job)).state == "ready" and found[speaker[1]].name is None


def test_mom08_a_move_is_not_redone_onto_a_speaker_merged_away_since(database: str) -> None:
    recording, _, line = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [move] = [each for each in state.changes if each.kind == "move"]
    settled(database, recording, move.id, "undo")
    # A later run merged the speaker the line had moved to.
    run_sql(database, "UPDATE speakers SET deleted_at = now() WHERE id = :id", id=move.intoId)
    after = settled(database, recording, move.id, "redo")
    _, lines, _ = asyncio.run(read(database, recording))
    assert [each.state for each in after.changes if each.id == move.id] == ["gone"]
    assert lines[line[8]] == move.speakerId


def test_mom08_identify_ends_done_even_when_its_task_is_cancelled(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    run_sql(
        database,
        "UPDATE recordings SET speaker_pass = CAST(:p AS jsonb) WHERE id = :id",
        p=json.dumps({"stage": "reading", "changes": []}),
        id=recording,
    )

    async def cancelled(*_: Any) -> None:
        raise asyncio.CancelledError

    # The route stored `reading`; a task cancelled (a timeout) must not leave it there, or every Identify waits.
    with patch.object(worker.jobs, "live", cancelled), pytest.raises(asyncio.CancelledError):
        worked(database, Scripted(ANSWER), VOICES, "identify_speakers", recording, uuid4())
    _, _, state = asyncio.run(read(database, recording))
    assert state.stage == "done"


def test_mom08_a_loop_ended_by_the_sweep_mid_round_writes_nothing_more(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))

    class Ended(Scripted):
        async def complete(
            self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = ""
        ) -> Any:
            # While the model answers, the sweep takes this loop for dead and ends it.
            bound = engine(database)
            async with sessions(bound)() as db:
                ended = (
                    "UPDATE recordings SET speaker_pass = jsonb_set(speaker_pass, '{stage}', '\"done\"') WHERE id = :id"
                )
                await db.execute(text(ended), {"id": recording})
                await db.commit()
            await bound.dispose()
            return await super().complete(turns, max_tokens, timeout, reason)

    model = Ended(ANSWER)
    looped(database, recording, model, rerun_at(VOICES))
    found, _, state = asyncio.run(read(database, recording))
    assert state.stage == "done" and state.changes == [] and found[speaker[1]].name is None
    # It stops there: no second round asks the model again.
    assert model.asked == 1


def test_mom08_a_run_whose_loop_is_done_writes_no_moved_section_again(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    run = uuid4()
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES), run)
    run_sql(
        database,
        "UPDATE recordings SET speaker_pass = jsonb_set(speaker_pass, '{stage}', '\"done\"') WHERE id = :id",
        id=recording,
    )
    model = Scripted(ANSWER)
    outcome = worked(database, model, VOICES, "identified", recording, None, run, None)
    # The same run again after its loop ended (a restart before the minutes were stored): its merges, no moves.
    assert model.asked == 0 and outcome.merges == {4: 2} and outcome.moved == set()


def test_mom08_a_held_job_s_clock_starts_again_when_its_loop_resumes(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    job = asyncio.run(identifying(database, recording, late=False))
    run_sql(database, "UPDATE transcription_jobs SET updated_at = now() - interval '1 hour' WHERE id = :id", id=job)

    async def resumed() -> datetime:
        bound = engine(database)
        async with sessions(bound)() as db:
            held = await db.get_one(TranscriptionJob, job)
            await worker.resume_held(db, held)
            await db.commit()
            again = (
                await db.execute(select(TranscriptionJob.updated_at).where(TranscriptionJob.id == job))
            ).scalar_one()
        await bound.dispose()
        return again

    assert datetime.now(UTC) - asyncio.run(resumed()) < timedelta(minutes=1)


def test_mom08_undo_all_writes_again_what_a_merge_touched_and_never_retells_it(database: str) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))
    doc = {
        "summary": "{{p:2}} and {{p:4}} thanked Ian.",
        "mom": "m",
        "openQuestions": [],
        "decisions": [],
        "actionItems": [],
    }

    async def go() -> dict[str, Any]:
        bound = engine(database)
        async with sessions(bound)() as db:
            meeting = Meeting(id=uuid4(), recording_id=recording, title="t", state="ready", minutes=doc)
            db.add(meeting)
            await db.commit()
            owner = await db.get_one(Recording, recording)
            await speakers_api.undo_speakers(recording, SimpleNamespace(id=owner.device_id), db, request)  # type: ignore[arg-type]
            await db.refresh(meeting)
            minutes = meeting.minutes or {}
        await bound.dispose()
        return minutes

    minutes = asyncio.run(go())
    # Speaker 4 is back: the summary naming the speaker it had joined is written again, its tokens as they were.
    assert (minutes["summary"], minutes["pending"]) == ("{{p:2}} and {{p:4}} thanked Ian.", ["summary", "mom"])


@pytest.mark.parametrize(("held_for", "finished"), [(16, False), (21, True)])
def test_mom08_the_sweep_waits_out_the_loop_s_timeout_and_margin(database: str, held_for: int, finished: bool) -> None:
    recording, _, _ = asyncio.run(seeded(database))
    job = asyncio.run(identifying(database, recording, late=True))
    run_sql(
        database,
        "UPDATE transcription_jobs SET updated_at = now() - make_interval(mins => :m) WHERE id = :id",
        m=held_for,
        id=job,
    )
    redis = Quiet()
    held(database, redis, "sweep_waiting_jobs")
    # 15 minutes of loop and 5 of margin: still running at 16, over at 21.
    assert any(each.startswith(f"merge:{job}:held:") for each in redis.ids) is finished


def test_mom08_a_run_that_no_longer_holds_the_pass_writes_nothing_into_the_newer_one(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    newer = str(uuid4())

    class Overtaken(Scripted):
        async def complete(
            self, turns: list[tuple[str, str]], max_tokens: int, timeout: float, reason: str = ""
        ) -> Any:
            # While the model answers, Identify starts a newer run, which holds the pass now.
            bound = engine(database)
            async with sessions(bound)() as db:
                taken = (
                    "UPDATE recordings SET speaker_pass = jsonb_set(speaker_pass, '{run}', to_jsonb(CAST(:r AS text)))"
                )
                await db.execute(text(taken + " WHERE id = :id"), {"r": newer, "id": recording})
                await db.commit()
            await bound.dispose()
            return await super().complete(turns, max_tokens, timeout, reason)

    looped(database, recording, Overtaken(ANSWER), rerun_at(VOICES))
    found, _, state = asyncio.run(read(database, recording))
    assert (state.run, state.changes) == (newer, []) and found[speaker[1]].name is None


def test_mom08_a_speaker_put_that_changes_only_the_name_s_source_is_an_edit(database: str) -> None:
    """A source sent back alone (an Undo restoring Snapbak's guess on another Mac) counts: the minutes go stale and
    other Macs hear of it."""
    from snapbak_backend.api.v1 import transcript as transcript_api
    from snapbak_backend.schemas.cloud import SpeakerRequest

    recording, speaker, _ = asyncio.run(seeded(database))
    asyncio.run(with_meeting(database, recording, "ready"))
    run_sql(database, "UPDATE speakers SET name = 'Joanne', name_source = 'user' WHERE id = :id", id=speaker[1])
    redis = Quiet()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=redis))))

    async def go() -> int:
        bound = engine(database)
        async with sessions(bound)() as db:
            owner = await db.get_one(Recording, recording)
            before = await db.scalar(select(Meeting.transcript_version).where(Meeting.recording_id == recording))
            body = SpeakerRequest(label="Speaker 1", name="Joanne", colorIdx=1, nameSource="guessed")
            device: Any = SimpleNamespace(id=owner.device_id)
            asked: Any = request
            await transcript_api.put_speaker(recording, speaker[1], body, device, db, asked, SimpleNamespace())  # type: ignore[arg-type]
            after = await db.scalar(select(Meeting.transcript_version).where(Meeting.recording_id == recording))
        await bound.dispose()
        return (after or 0) - (before or 0)

    assert asyncio.run(go()) == 1


def test_mom08_a_redo_the_transcript_rules_out_is_refused_and_stays_gone(database: str) -> None:
    recording, speaker, _ = asyncio.run(seeded(database))
    looped(database, recording, Scripted(ANSWER), rerun_at(VOICES))
    _, _, state = asyncio.run(read(database, recording))
    [name] = [each for each in state.changes if each.kind == "name" and each.speakerId == speaker[1]]
    settled(database, recording, name.id, "undo")
    run_sql(database, "UPDATE speakers SET name = 'Jo', name_source = 'user' WHERE id = :id", id=speaker[1])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cloud=SimpleNamespace(redis=Quiet()))))

    async def redo() -> None:
        bound = engine(database)
        try:
            async with sessions(bound)() as db:
                owner = await db.get_one(Recording, recording)
                device: Any = SimpleNamespace(id=owner.device_id)
                asked: Any = request
                await speakers_api.change_speakers(
                    recording, name.id, SpeakerChangeRequest(action="redo"), device, db, asked
                )
        finally:
            await bound.dispose()

    # The route answers why, and the change it couldn't bring back is stored gone, so no Redo is offered again.
    with pytest.raises(CloudFailure) as refused:
        asyncio.run(redo())
    _, _, after = asyncio.run(read(database, recording))
    assert refused.value.code == "change_gone"
    assert [each.state for each in after.changes if each.id == name.id] == ["gone"]
