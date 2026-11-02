"""The speaker identification loop's runs (MEETINGS.md D42, MOM-08): the model's rounds, the pyannote rerun, and the
changes applied to and undone from the transcript in place, under the recording's lock. The rules are in
services/speakers.py. The loop's own changes never bump the meeting's transcript version: they are the server's,
not the user's, so they never leave the minutes stale."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.ai.chain import Call, ProviderFailed, Tiers
from snapbak_backend.ai.prompts import speakers as prompt
from snapbak_backend.db.models import Recording, Speaker, TranscriptLine
from snapbak_backend.schemas.cloud import Change, MinutesOut, SpeakerPass
from snapbak_backend.services import minutes, people, recordings, speakers
from snapbak_backend.services.failures import CloudFailure
from snapbak_backend.services.recordings import speaker_of
from snapbak_backend.transcription.merge import Turn

type Sessions = Callable[[], Any]
# The stage a run reached, stored and published by the caller.
type Progress = Callable[[str], Awaitable[None]]
# Stages after which a resumed run skips the loop: its changes are in, and the sections they touched were stored
# pending before they were written again.
SETTLED = ("rewriting", "done")


class Stored(SpeakerPass):
    """The loop's state as the recording stores it: the app's view, plus the run holding it (the minutes run's
    token, or the job's), so a resumed run carries on rather than starting over. Never sent: the routes answer with
    SpeakerPass."""

    run: str | None = None


@dataclass(frozen=True)
class Heard:
    """What a rerun heard: its turns, and the speakers whose voiceprints one voice matched (pyannote identify)."""

    turns: list[Turn]
    same: list[frozenset[UUID]] = field(default_factory=list)


@dataclass(frozen=True)
class Rerun:
    """pyannote again: `run(count)` hears the recording (None when it couldn't: no quota, the vendor down, too
    slow). With `voiceprints` (two speakers or more have one) it runs whatever the count, since the voiceprints
    show a voice split in two; without, only at a count of fewer voices than speakers."""

    voiceprints: bool
    run: Callable[[int], Awaitable[Heard | None]]


@dataclass
class Outcome:
    """What a run changed: its changes, the people tokens to retell (each merged speaker's `p` to the kept one's,
    every merge still applied, so a token from a crashed run's draft is retold too), and the lines that moved to
    another speaker, whose sections are written again."""

    changes: list[Change] = field(default_factory=list)
    merges: dict[int, int] = field(default_factory=dict)
    moved: set[UUID] = field(default_factory=set)


def pass_of(recording: Recording) -> Stored:
    return Stored.model_validate(recording.speaker_pass or {})


def pass_or_none(recording: Recording) -> SpeakerPass | None:
    """The loop's state as the app reads it: null before the loop ever ran."""
    return None if recording.speaker_pass is None else SpeakerPass.model_validate(recording.speaker_pass)


async def proposed(
    tiers: Tiers,
    lines: list[TranscriptLine],
    known: dict[UUID, Speaker],
    draft: MinutesOut | None,
    calls: list[Call],
) -> speakers.Proposal:
    """One round's model call over the transcript (and the draft, as words), every cue checked; nothing on a model
    that is down."""
    numbered = people.by_number(known.values())
    # When each speaker talks: a voice split in two shows as one going quiet where the other starts.
    said_at: dict[UUID, list[int]] = {}
    for line in lines:
        said_at.setdefault(line.speaker_id, []).append(line.idx)
    sent = [
        {
            "p": p,
            "sname": speaker_of(each),
            "locked": speakers.user_named(each),
            "lines": len(said_at.get(each.id, [])),
            "first": min(said_at.get(each.id, [-1])),
            "last": max(said_at.get(each.id, [-1])),
        }
        for p, each in numbered.items()
    ]
    said = [
        {"i": line.idx, "p": people.number_of(known[line.speaker_id]), "text": line.text}
        for line in lines
        if line.speaker_id in known
    ]
    plain = None if draft is None else people.rendered_doc(draft, known)
    words = None if plain is None else {"summary": plain.summary, "mom": plain.mom}
    try:
        answer = await minutes.asked(tiers.chain(tiers.polish), prompt.turns(sent, said, words), calls)
    except ProviderFailed:
        return speakers.Proposal()
    made = speakers.proposal_of(answer, {line.idx: line for line in lines}, known)
    return made


async def apply(db: AsyncSession, changes: list[Change]) -> list[Change]:
    """The changes into the transcript, the caller holding the recording's lock, each only where the transcript
    still allows it (the user may have renamed or edited since the round read it): a guessed name on a speaker the
    user didn't name; a merge of a speaker the user didn't name into one still there, every line it holds moving
    (those become its `lineIds`, so an undo finds them); a moved line the user hasn't edited. The changes that went
    in, and the suggested ones as they are."""
    now = datetime.now(UTC)
    kept: list[Change] = []
    for change in changes:
        if change.state != "applied":
            kept.append(change)
            continue
        if change.kind == "name":
            named = await db.scalars(
                update(Speaker)
                .where(
                    Speaker.id == change.speakerId,
                    Speaker.name_source.is_distinct_from("user"),
                    Speaker.name_source.is_distinct_from("guessed"),
                    Speaker.deleted_at.is_(None),
                )
                .values(name=change.name, name_source="guessed")
                .returning(Speaker.id)
            )
            went = named.first() is not None
        elif change.kind == "merge":
            into = await db.scalar(select(Speaker.id).where(Speaker.id == change.intoId, Speaker.deleted_at.is_(None)))
            gone = None
            if into is not None:
                gone = await db.scalar(
                    update(Speaker)
                    .where(
                        Speaker.id == change.speakerId,
                        Speaker.name_source.is_distinct_from("user"),
                        Speaker.deleted_at.is_(None),
                    )
                    .values(deleted_at=now)
                    .returning(Speaker.id)
                )
            went = gone is not None
            if went:
                moved = await db.scalars(
                    update(TranscriptLine)
                    .where(TranscriptLine.speaker_id == change.speakerId)
                    .values(speaker_id=change.intoId)
                    .returning(TranscriptLine.id)
                )
                change = change.model_copy(update={"lineIds": list(moved)})
        else:
            # Onto a speaker still there: one merged away since (a redo after a later run) would lose the line.
            live = select(Speaker.id).where(Speaker.id == change.intoId, Speaker.deleted_at.is_(None)).exists()
            line = await db.scalars(
                update(TranscriptLine)
                .where(
                    TranscriptLine.id == change.lineIds[0],
                    TranscriptLine.speaker_id == change.speakerId,
                    TranscriptLine.rev <= 1,
                    live,
                )
                .values(speaker_id=change.intoId)
                .returning(TranscriptLine.id)
            )
            went = line.first() is not None
        if went:
            kept.append(change)
    return kept


async def undo(db: AsyncSession, change: Change) -> None:
    """One applied change taken back, the caller holding the recording's lock, only where the user hasn't changed it
    since: the name the speaker had (unless they were renamed meanwhile), a merged speaker back with its lines, a
    moved line back."""
    if change.kind == "name":
        await db.execute(
            update(Speaker)
            .where(Speaker.id == change.speakerId, Speaker.name_source == "guessed", Speaker.name == change.name)
            .values(name=change.before, name_source="vendor" if change.before else None)
        )
        return
    # A merge moved every line its speaker held, edited or not, so its undo takes them all back; a move only moved
    # a line the user hadn't edited, and gives it back only if they still haven't.
    back = [TranscriptLine.id.in_(change.lineIds), TranscriptLine.speaker_id == change.intoId]
    if change.kind == "merge":
        # A live speaker took its label since (a new speaker numbered after the live ones, TRX-11): bringing it back
        # would make two of one label, so the undo waits, as under a later change.
        merged_away = await db.get_one(Speaker, change.speakerId)
        taken = await db.scalar(
            select(Speaker.id).where(
                Speaker.recording_id == merged_away.recording_id,
                Speaker.label == merged_away.label,
                Speaker.deleted_at.is_(None),
            )
        )
        if taken is not None:
            raise CloudFailure(409, "change_later")
        await db.execute(update(Speaker).where(Speaker.id == change.speakerId).values(deleted_at=None))
    else:
        back.append(TranscriptLine.rev <= 1)
    await db.execute(update(TranscriptLine).where(*back).values(speaker_id=change.speakerId))


async def save_pass(db: AsyncSession, recording_id: UUID, state: Stored) -> None:
    await db.execute(
        update(Recording).where(Recording.id == recording_id).values(speaker_pass=state.model_dump(mode="json"))
    )


async def restaged(db: AsyncSession, recording_id: UUID, **moved: Any) -> Stored:
    """The stored state with `moved` fields, read and written under the recording's lock, so a user's answer that
    landed meanwhile stays. The caller commits."""
    recording = await recordings.lock(db, recording_id)
    state = pass_of(recording).model_copy(update=moved)
    await save_pass(db, recording_id, state)
    return state


async def outcome_of(db: AsyncSession, recording_id: UUID) -> Outcome:
    """What the run holding the stored pass changed, read back as it is stored now (so a resumed run has it too, and
    a merge the user undid meanwhile is no longer retold): its changes, every applied merge's tokens, and the lines
    its applied moves took."""
    state = pass_of(await db.get_one(Recording, recording_id))
    labels = await db.execute(select(Speaker.id, Speaker.label).where(Speaker.recording_id == recording_id))
    numbers = {speaker: n for speaker, label in labels if (n := people.number_of_label(label)) is not None}
    ours = state.changes[state.start :]
    applied = [change for change in state.changes if change.state == "applied"]
    merges: dict[int, int] = {}
    for change in applied:
        if change.kind != "merge" or change.intoId is None:
            continue
        loser = numbers.get(change.speakerId)
        into = numbers.get(change.intoId)
        if loser is not None and into is not None:
            merges[loser] = into
    moved = {change.lineIds[0] for change in ours if change.kind == "move" and change.state == "applied"}
    made = Outcome(changes=ours, merges=merges, moved=moved)
    return made


async def run(
    tiers: Tiers,
    sessions: Sessions,
    recording_id: UUID,
    run_id: UUID,
    draft: MinutesOut | None,
    rerun: Rerun | None,
    progress: Progress,
) -> Outcome:
    """Up to two rounds: the model proposes, the rerun (round 1 only) gives the voices to agree with, and the changes
    go in; a second round only after a big change. The same `run_id` again (a retried task) carries on: past round
    1 it changes nothing more, and past SETTLED it has nothing left to write again. Each stage is handed to
    `progress` (stored and published) as it starts."""
    calls: list[Call] = []
    async with sessions() as db:
        state = pass_of(await db.get_one(Recording, recording_id))
    resumed = state.run == str(run_id)
    if not resumed:
        async with sessions() as db:
            fresh = {
                "stage": "reading",
                "round": 0,
                "listened": False,
                "failed": False,
                "run": str(run_id),
                "start": len(state.changes),
            }
            state = await restaged(db, recording_id, **fresh)
            await db.commit()
        await progress("reading")
    voices: dict[int, str] | None = None
    same: list[frozenset[UUID]] = []
    # A resumed run past round 1 changes nothing more (a second round is only ever a nice-to-have), and one whose loop
    # is over (done, or ended by the sweep) asks nothing again.
    rounds = () if resumed and (state.round >= 1 or state.stage == "done") else (1, 2)
    for round_number in rounds:
        async with sessions() as db:
            lines = await recordings.live_lines(db, recording_id)
            known = await recordings.speakers_of(db, recording_id)
        found = await proposed(tiers, lines, known, draft, calls)
        fewer = 2 <= found.count < len(known)
        if round_number == 1 and rerun is not None and (rerun.voiceprints or fewer):
            async with sessions() as db:
                await restaged(db, recording_id, listened=True)
                await db.commit()
            await progress("listening")
            heard = await rerun.run(found.count)
            voices = speakers.relabel(lines, heard.turns) if heard else None
            same = heard.same if heard else []
        changes = speakers.decide(found, known, lines, voices, same, state.changes)
        async with sessions() as db:
            latest = pass_of(await recordings.lock(db, recording_id))
            if latest.stage == "done" or latest.run != str(run_id):
                # Ended meanwhile (the sweep ended a loop it took for dead), or a newer run holds the pass now: this
                # run writes nothing more.
                break
            asked = [change for change in changes if not speakers.said_no(change, latest.changes)]
            went = await apply(db, asked)
            state = latest.model_copy(
                update={
                    "stage": "naming",
                    "round": round_number,
                    "rev": latest.rev + (1 if went else 0),
                    "changes": [*latest.changes, *went],
                }
            )
            await save_pass(db, recording_id, state)
            await db.commit()
        await progress("naming")
        if not speakers.big(went, len(lines)):
            break
    async with sessions() as db:
        outcome = await outcome_of(db, recording_id)
    if resumed and state.stage in SETTLED:
        outcome.moved = set()
    return outcome


async def merged_since(db: AsyncSession, change: Change) -> bool:
    """Whether a speaker the change would undo onto went since: the one a merge kept, or either end of a move (a
    merge's own speaker went with the merge, and comes back with its undo)."""
    ends = [change.intoId] if change.kind == "merge" else [change.intoId, change.speakerId]
    gone = await db.scalar(select(func.count()).where(Speaker.id.in_(ends), Speaker.deleted_at.is_not(None)))
    return bool(gone)


# What each action needs the change to be, and what it makes it.
ACTIONS = {
    "undo": ("applied", "undone"),
    "redo": ("undone", "applied"),
    "accept": ("suggested", "applied"),
    "reject": ("suggested", "rejected"),
}


async def settle_change(db: AsyncSession, recording_id: UUID, change_id: str, action: str) -> tuple[Stored, Change]:
    """The user's answer to one change, under the recording's lock: `undo` an applied one, `redo` an undone one
    (applied again as the loop applied it, where the transcript still allows), `accept` a suggested name (the user's
    own name from then on) or `reject` it; `rev` moves. 404 for an unknown change, 409 for one not in the state the
    action needs, or a suggestion for a speaker the user has named since. A redo the user's edits since rule out is
    stored `gone` (the caller answers 409 `change_gone` once committed). The caller commits."""
    recording = await recordings.lock(db, recording_id)
    state = pass_of(recording)
    change = next((each for each in state.changes if each.id == change_id), None)
    if change is None:
        raise CloudFailure(404, "not_found")
    needed, made = ACTIONS[action]
    if change.state != needed:
        raise CloudFailure(409, "change_state")
    if action == "undo" and change.kind in ("merge", "move") and await merged_since(db, change):
        # Its lines sit on the speaker that one joined: Undo all takes the chain back, latest first.
        raise CloudFailure(409, "change_later")
    if action == "undo":
        await undo(db, change)
    elif action == "redo":
        again = await apply(db, [change.model_copy(update={"state": "applied"})])
        # The transcript no longer allows it (the speaker renamed, or merged away, since): the change is `gone`, so no
        # Redo is offered again. A merge redone takes the lines its speaker holds now.
        change, made = (again[0], made) if again else (change, "gone")
    elif action == "accept":
        # Never over a name the user gave meanwhile: theirs wins, and the suggestion is past.
        accepted = await db.scalars(
            update(Speaker)
            .where(
                Speaker.id == change.speakerId,
                Speaker.name_source.is_distinct_from("user"),
                Speaker.deleted_at.is_(None),
            )
            .values(name=change.name, name_source="user")
            .returning(Speaker.id)
        )
        if accepted.first() is None:
            raise CloudFailure(409, "change_state")
    settled = change.model_copy(update={"state": made})
    changes = [settled if each.id == change_id else each for each in state.changes]
    state = state.model_copy(update={"rev": state.rev + 1, "changes": changes})
    await save_pass(db, recording_id, state)
    return state, settled


async def undo_all(db: AsyncSession, recording_id: UUID) -> Stored:
    """Every applied change taken back, the latest first, under the recording's lock; `rev` moves once. The caller
    commits."""
    recording = await recordings.lock(db, recording_id)
    state = pass_of(recording)
    for change in reversed(state.changes):
        if change.state == "applied":
            await undo(db, change)
    changes = [
        each.model_copy(update={"state": "undone"}) if each.state == "applied" else each for each in state.changes
    ]
    state = state.model_copy(update={"rev": state.rev + 1, "changes": changes})
    await save_pass(db, recording_id, state)
    return state
