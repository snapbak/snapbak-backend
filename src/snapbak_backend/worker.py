"""The arq worker (D10; docs/cloud.md, "arq worker"): `arq snapbak_backend.worker.WorkerSettings`. Every task takes
IDs only, reads the row, and returns early when the row is already past its step, so a duplicate or a re-run is a
no-op. A vendor's 5xx or a dropped network retries the task; a vendor's 4xx fails the job."""

import asyncio
import contextlib
import logging
import os
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

import sqlalchemy
from arq import Retry, cron, func
from arq.connections import RedisSettings
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from snapbak_backend.ai.chain import ProviderFailed
from snapbak_backend.ai.providers.factory import make_tiers
from snapbak_backend.config.settings import MAX_JOB_SECONDS, code_version, load
from snapbak_backend.db.models import (
    Device,
    Failure,
    Meeting,
    Recording,
    Speaker,
    TranscriptionJob,
    TranscriptLine,
    Upload,
)
from snapbak_backend.schemas.cloud import MinutesOut, PinnedOut
from snapbak_backend.services import (
    jobs,
    meeting_index,
    meetings,
    minutes,
    purge,
    qa,
    recordings,
    sealing,
    transliteration,
)
from snapbak_backend.services.cloud import Cloud, open_cloud
from snapbak_backend.services.events import publish
from snapbak_backend.services.metrics import task_log
from snapbak_backend.services.signatures import assemblyai_header
from snapbak_backend.transcription import voiceprints
from snapbak_backend.transcription.factory import make_strategy
from snapbak_backend.transcription.languages import U35_PRO, base
from snapbak_backend.transcription.merge import Merged, text
from snapbak_backend.transcription.strategy import (
    VENDORS,
    JobInput,
    TranscriptionStrategy,
    Vendor,
    VendorError,
    VendorRefused,
    VendorUnavailable,
)

type Ctx = dict[str, Any]

MAX_TRIES = 5
VOICEPRINT_TRIES = 3
# A voiceprint whose webhook hasn't come by then is fetched anyway.
VOICEPRINT_CHECK_S = 60
# The vendors are asked directly if no webhook came by then, and again every minute.
FIRST_CHECK_S = 120
CHECK_EVERY_S = 60
# A minutes run that stopped moving (a lost enqueue, a worker that died) is started again after this long.
STALLED_MINUTES = timedelta(minutes=5)
# A minutes run marks itself alive this often while a section's call runs (up to its tries and its repair).
HEARTBEAT_S = 60
# A provider failure waits this long before the sweep starts the minutes again (an outage outlasted the run's own
# retries, about 11 minutes).
RETRY_FAILED = timedelta(minutes=15)
# A minutes run's whole time: ten sections, each normally under a minute. Past it arq cancels the task; the sweep
# then hands the meeting a new run, which resumes at the first section still pending.
MINUTES_TIMEOUT_S = 1800
AUDIO_URL_S = 7200
CLIP_URL_S = 3600
# AssemblyAI's own duration may pass 2 h by this much before the job fails `too_long`.
DURATION_SLACK_MS = 60_000
UPLOAD_EXPIRES = timedelta(hours=24)
# A queued job with its audio is started again after this long without a change.
STALLED_START = timedelta(minutes=5)
# The sweep fails a processing job this long after the timeout check would have.
SWEEP_MARGIN = timedelta(minutes=5)


def cloud_of(ctx: Ctx) -> Cloud:
    return ctx["cloud"]


def strategy_of(ctx: Ctx) -> TranscriptionStrategy:
    return ctx["strategy"]


async def load_job(db: AsyncSession, job_id: UUID) -> TranscriptionJob | None:
    job = await db.scalar(select(TranscriptionJob).where(TranscriptionJob.id == job_id))
    return job


class Moved(Exception):
    """Another writer changed the job after this task read it."""


async def fail_and_publish(
    ctx: Ctx, db: AsyncSession, job: TranscriptionJob, reason: Failure, task: str, **values: Any
) -> bool:
    """Fails the job, refunds it, publishes, and deletes the vendors' copies (HLD, privacy). False when another
    writer moved the job first."""
    started = time.monotonic()
    await recordings.lock(db, job.recording_id)
    won = await jobs.fail(db, job, reason, **values)
    # A meeting waiting on this transcript would wait forever: it fails with the job's code.
    waiting = await meetings.fail_waiting(db, job.recording_id, reason) if won else None
    # Ends the transaction either way, so the recording's lock goes now: a sweep fails many jobs, and a vendor
    # cleanup may follow. A lost fail wrote nothing, and a commit keeps `job` readable.
    await db.commit()
    if waiting is not None:
        await publish(cloud_of(ctx).redis, *waiting)
        # The meeting's own verdict line, so the unattended count sees a meeting that ended before its minutes.
        task_log("generate_minutes", str(waiting[0]), "-", elapsed(started), f"failed:{reason}")
    if won:
        await publish(cloud_of(ctx).redis, job.id, job.version)
        if job.vendor_refs:
            await strategy_of(ctx).cleanup(job.vendor_refs)
    task_log(task, str(job.id), "-", elapsed(started), f"failed:{reason}" if won else "lost")
    return won


def elapsed(started: float) -> int:
    seconds = time.monotonic() - started
    return int(seconds * 1000)


def webhook(ctx: Ctx, vendor: str, job_id: UUID, **query: str) -> str:
    extra = "".join(f"&{name}={value}" for name, value in query.items())
    return f"{cloud_of(ctx).config.public_url}/v1/webhooks/{vendor}?job={job_id}{extra}"


async def start_transcription(ctx: Ctx, job_id: UUID) -> None:
    """Presigns the audio for the vendors, submits it to them, and schedules the timeout check."""
    started = time.monotonic()
    cloud = cloud_of(ctx)
    async with cloud.sessions() as db:
        job = await load_job(db, job_id)
        if job is None or job.upload_id is None:
            return
        if job.state == "failed" and any(vendor in job.vendor_refs for vendor in VENDORS):
            # The job failed (its recording was trashed) while this submit was waiting to retry: the vendor that
            # had accepted is deleted, as fail_and_publish would have.
            await strategy_of(ctx).cleanup(job.vendor_refs)
            return
        if job.state != "queued":
            return
        if job.deleted_at is not None:
            # The delete fails a queued job itself; it misses one only when an upload's complete moved the job's
            # version in the same moment, and then this settles it and gives its minutes back.
            await fail_and_publish(ctx, db, job, "internal", "start_transcription")
            return
        upload = await db.get_one(Upload, job.upload_id)
        recording = await db.get_one(Recording, job.recording_id)
        request = JobInput(
            job_id=job.id,
            recording_id=job.recording_id,
            audio_url=cloud.storage.presign_get(upload.s3_key, AUDIO_URL_S),
            duration_ms=recording.duration_ms,
            language=job.language_requested,
            webhooks={
                "assemblyai": webhook(ctx, "assemblyai", job.id),
                "pyannote": webhook(ctx, "pyannote", job.id, kind="diarize"),
            },
            webhook_auth=assemblyai_header(cloud.config.webhook_secret, str(job.id)),
        )
        try:
            refs = await strategy_of(ctx).submit(request, job.vendor_refs)
        except VendorError as error:
            if isinstance(error, VendorUnavailable) and ctx["job_try"] < MAX_TRIES:
                # Keep the vendor that accepted, so the retry submits only the other.
                await db.execute(
                    update(TranscriptionJob).where(TranscriptionJob.id == job.id).values(vendor_refs=error.refs)
                )
                await db.commit()
                raise Retry(defer=2 ** ctx["job_try"]) from error
            reason: Failure = "vendor_error" if isinstance(error, VendorRefused) else "internal"
            if not await fail_and_publish(ctx, db, job, reason, "start_transcription", vendor_refs=error.refs):
                # A delete failed the job meanwhile; the vendor that accepted this try is deleted all the same.
                await strategy_of(ctx).cleanup(error.refs)
            return
        won = await jobs.advance(
            db, job, state="processing", stage="transcribing", vendor_refs=refs, submitted_at=datetime.now(UTC)
        )
        if not won:
            # Another start got there first; the vendors' jobs this one made would be orphans.
            await strategy_of(ctx).cleanup(refs)
            return
        await db.commit()
    await publish(cloud.redis, job_id, job.version)
    await cloud.redis.enqueue_job(
        "check_vendor_timeout", job_id, 1, _job_id=f"timeout:{job_id}:1", _defer_by=FIRST_CHECK_S
    )
    task_log("start_transcription", str(job_id), "-", elapsed(started), "processing")


async def settle(ctx: Ctx, db: AsyncSession, job: TranscriptionJob, vendor: Vendor) -> None:
    """Asks the vendor how its part is going and moves the job: the next stage, merging once both are done, or
    failed. A vendor already done is a no-op, so duplicate webhooks change nothing. Raises VendorUnavailable, or
    Moved when another writer won; the caller decides when to ask again."""
    if job.vendor_refs[vendor]["status"] == "done":
        return
    status = await strategy_of(ctx).status(job.vendor_refs, vendor)
    if status.state == "running":
        return
    if status.state == "failed":
        if not await fail_and_publish(ctx, db, job, status.failure or "vendor_error", f"vendor:{vendor}"):
            raise Moved(job.id)
        return
    refs = {**job.vendor_refs, vendor: {**job.vendor_refs[vendor], "status": "done"}}
    both = all(refs[each]["status"] == "done" for each in VENDORS)
    stage = "merging" if both else ("diarizing" if vendor == "assemblyai" else job.stage)
    if not await jobs.advance(db, job, vendor_refs=refs, stage=stage):
        raise Moved(job.id)
    await db.commit()
    await publish(cloud_of(ctx).redis, job.id, job.version)
    if both:
        await cloud_of(ctx).redis.enqueue_job("merge_and_store", job.id, _job_id=f"merge:{job.id}")


async def on_vendor_webhook(ctx: Ctx, job_id: UUID, vendor: Vendor) -> None:
    started = time.monotonic()
    async with cloud_of(ctx).sessions() as db:
        job = await load_job(db, job_id)
        if job is not None and job.state == "queued":
            # The vendor answered before the submit was recorded.
            raise Retry(defer=1)
        if job is None or job.state != "processing":
            return
        try:
            await settle(ctx, db, job, vendor)
        except (VendorUnavailable, Moved) as error:
            if ctx["job_try"] < MAX_TRIES:
                raise Retry(defer=2 ** ctx["job_try"]) from error
            # The timeout check asks this vendor again within a minute.
            task_log("on_vendor_webhook", str(job_id), vendor, elapsed(started), "gave_up")
            return
    task_log("on_vendor_webhook", str(job_id), vendor, elapsed(started), job.stage or job.state)


async def check_vendor_timeout(ctx: Ctx, job_id: UUID, n: int) -> None:
    """The fallback for a lost webhook: asks both vendors, then again in a minute, until the deadline. It never
    retries itself: a vendor outage or a lost race waits for the next check, so the chain can't run out of tries and
    leave the job processing forever."""
    cloud = cloud_of(ctx)
    async with cloud.sessions() as db:
        job = await load_job(db, job_id)
        if job is None or job.state != "processing" or job.stage == "merging":
            return
        recording = await db.get_one(Recording, job.recording_id)
        if datetime.now(UTC) > deadline(job, recording.duration_ms):
            await fail_and_publish(ctx, db, job, "vendor_timeout", "check_vendor_timeout")
            return
        with contextlib.suppress(VendorUnavailable, Moved):
            for vendor in VENDORS:
                if job.state == "processing":
                    await settle(ctx, db, job, vendor)
        if job.state == "processing" and job.stage != "merging":
            await cloud.redis.enqueue_job(
                "check_vendor_timeout", job_id, n + 1, _job_id=f"timeout:{job_id}:{n + 1}", _defer_by=CHECK_EVERY_S
            )


def refusal(merged: Merged) -> Failure | None:
    """The failure a finished transcript still earns: a language outside the 18, over 2 h, or no words."""
    if base(merged.language) not in U35_PRO:
        return "unsupported_language"
    if merged.audio_ms > MAX_JOB_SECONDS * 1000 + DURATION_SLACK_MS:
        return "too_long"
    if not merged.lines:
        return "no_speech"
    return None


async def store(db: AsyncSession, job: TranscriptionJob, merged: Merged) -> None:
    """Speakers and lines in the caller's transaction. Rows a crashed earlier run left are replaced, so a re-run
    writes the same transcript."""
    await db.execute(delete(TranscriptLine).where(TranscriptLine.recording_id == job.recording_id))
    await db.execute(delete(Speaker).where(Speaker.recording_id == job.recording_id))
    ids: dict[str, UUID] = {}
    for n, speaker in enumerate(merged.speakers):
        ids[speaker.key] = uuid.uuid4()
        db.add(
            Speaker(
                id=ids[speaker.key],
                recording_id=job.recording_id,
                label=f"Speaker {n + 1}",
                vendor_label=speaker.key,
                name=speaker.name,
                name_source="vendor" if speaker.name else None,
                color_idx=n % 8,
            )
        )
    await db.flush()
    for idx, line in enumerate(merged.lines):
        db.add(
            TranscriptLine(
                id=uuid.uuid4(),
                recording_id=job.recording_id,
                speaker_id=ids[line[0].speaker],
                idx=idx,
                start_ms=line[0].s,
                end_ms=line[-1].e,
                text=text(line, merged.language),
                words=[{"w": w.w, "s": w.s, "e": w.e, "filler": w.filler, "conf": w.conf} for w in line],
            )
        )


async def merge_and_store(ctx: Ctx, job_id: UUID) -> None:
    started = time.monotonic()
    cloud = cloud_of(ctx)
    strategy = strategy_of(ctx)
    async with cloud.sessions() as db:
        job = await load_job(db, job_id)
        if job is None or job.state != "processing":
            return
        try:
            merged = await strategy.result(job.vendor_refs)
        except VendorError as error:
            if isinstance(error, VendorUnavailable) and ctx["job_try"] < MAX_TRIES:
                raise Retry(defer=2 ** ctx["job_try"]) from error
            reason: Failure = "vendor_error" if isinstance(error, VendorRefused) else "internal"
            await fail_and_publish(ctx, db, job, reason, "merge_and_store")
            return
        refused = refusal(merged)
        if refused is not None:
            await fail_and_publish(ctx, db, job, refused, "merge_and_store")
            return
        recording = await recordings.lock(db, job.recording_id)
        await db.refresh(job)
        if job.state != "processing":
            return
        if recording.deleted_at is not None:
            await fail_and_publish(ctx, db, job, "internal", "merge_and_store")
            return
        await store(db, job, merged)
        if not await jobs.advance(db, job, state="ready", stage=None, language_detected=merged.language):
            await db.rollback()
            raise Retry(defer=0.1)
        await db.commit()
        device = await db.get_one(Device, recording.device_id)
    await publish(cloud.redis, job_id, job.version)
    await strategy.cleanup(job.vendor_refs)
    if device.voiceprint_consent:
        await cloud.redis.enqueue_job("create_voiceprints", job_id, _job_id=f"voiceprints:{job_id}")
    if job.script == "roman":
        await cloud.redis.enqueue_job("transliterate", job.recording_id, _job_id=f"translit:{job.recording_id}")
    await start_minutes(cloud, job.recording_id)
    task_log("merge_and_store", str(job_id), "-", elapsed(started), "ready")


async def start_minutes(cloud: Cloud, recording_id: UUID) -> None:
    """The meeting whose transcript just landed starts its minutes (D13); none, or one already generating, is a
    no-op. A session of its own: the merge's is closed by now, and reusing it would leave a transaction open."""
    async with cloud.sessions() as db:
        meeting = await meetings.live_meeting(db, recording_id)
        if meeting is not None:
            await meetings.begin(db, cloud.redis, meeting.id, clicked=False)


async def fail_minutes(cloud: Cloud, meeting_id: UUID, run: UUID, reason: str, started: float) -> None:
    """The meeting failed its minutes for good (the retries are over), committed and published; the app shows the
    reason and a retry."""
    async with cloud.sessions() as db:
        version = await meetings.fail_minutes(db, meeting_id, run, reason)
        await db.commit()
    if version is not None:
        await publish(cloud.redis, meeting_id, version)
    task_log("generate_minutes", str(meeting_id), "-", elapsed(started), f"failed:{reason}")


async def claimed(cloud: Cloud, meeting_id: UUID, run: UUID) -> bool:
    """The run marks the meeting alive, if it still holds its token."""
    async with cloud.sessions() as db:
        held = await meetings.claim(db, meeting_id, run)
        await db.commit()
    return held


class Lost(Exception):
    """The run's token was taken (the sweep replaced it, or the meeting moved on): it writes nothing more."""


async def saving(cloud: Cloud, meeting_id: UUID, run: UUID, pinned: list[PinnedOut], doc: MinutesOut) -> None:
    """One section's progress stored and published while the run holds its token; Lost when it no longer does."""
    body = stored(doc, pinned)
    async with cloud.sessions() as db:
        version = await meetings.save_minutes(db, meeting_id, run, body)
        await db.commit()
    if version is None:
        raise Lost
    await publish(cloud.redis, meeting_id, version)


async def heartbeat(cloud: Cloud, meeting_id: UUID, run: UUID) -> None:
    """Marks the run alive every HEARTBEAT_S while one section's call (up to its tries and repair) runs long, so the
    sweep never takes a run that is still working."""
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        await claimed(cloud, meeting_id, run)


def stored(doc: MinutesOut, pinned: list[PinnedOut]) -> dict[str, Any]:
    """The minutes as the meeting stores them: the pins go in as they are, beside the model's sections (MEETINGS.md
    7.1): no model judges a pin."""
    return doc.model_copy(update={"pinned": pinned}).model_dump(mode="json")


async def generate_minutes(ctx: Ctx, meeting_id: UUID, run: UUID) -> None:
    """Minutes one section call at a time, each stored and published as it lands, then the embedding task
    (MEETINGS.md 5.1, minutes v2), for the run holding `run`. The run claims the meeting before the model runs (a
    duplicate, a replaced run or a meeting that moved on returns at once) and resumes from the sections still
    pending, so a crashed run picks up where it stopped. A section that fails is marked and the rest go on; only
    when neither the summary nor the MoM could be written does the task retry (the model down) or fail."""
    started = time.monotonic()
    cloud = cloud_of(ctx)
    if not await claimed(cloud, meeting_id, run):
        return
    async with cloud.sessions() as db:
        meeting = await db.get_one(Meeting, meeting_id)
        recording_id, title = meeting.recording_id, meeting.title
        # The version the minutes are built from, read before the model runs, so an edit landing mid-call leaves
        # the minutes stale rather than silently current (D29).
        snapshot = meeting.transcript_version
        # The day the meeting was held, so "Friday" resolves to a real date (UTC: the recording's own clock).
        day = meeting.started_at.date().isoformat() if meeting.started_at else None
        doc = meetings.minutes_of(meeting) or minutes.empty(list(minutes.CALLS))
        job = await jobs.live(db, recording_id)
        ready = job is not None and job.state == "ready"
        lines = await recordings.live_lines(db, recording_id) if ready else []
        speakers = await recordings.speakers_of(db, recording_id)
        pins = await meetings.pins(db, meeting_id)
    if not lines:
        await fail_minutes(cloud, meeting_id, run, "no_speech" if ready else "no_transcript", started)
        return
    marked = [{"atMs": pin.at_ms, "note": pin.note} for pin in pins]
    # The pins go in as they are, beside the model's sections (MEETINGS.md 7.1): no model judges a pin.
    pinned = [PinnedOut(pinId=pin.id, atMs=pin.at_ms, note=pin.note) for pin in pins]
    alive = asyncio.create_task(heartbeat(cloud, meeting_id, run))
    try:
        saved = partial(saving, cloud, meeting_id, run, pinned)
        doc, engine = await minutes.run(ctx["tiers"], title, marked, lines, speakers, doc, saved, day)
    except Lost:
        task_log("generate_minutes", str(meeting_id), "-", elapsed(started), "lost")
        return
    finally:
        alive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await alive
    # Neither text written: the run fails with the summary's code; a provider outage is the sweep's to retry.
    code = minutes.failure(doc)
    if code:
        await fail_minutes(cloud, meeting_id, run, code, started)
        return
    body = stored(doc, pinned)
    async with cloud.sessions() as db:
        written = await meetings.finish(db, meeting_id, run, body, engine, snapshot)
        await db.commit()
    if written is None:
        return
    version, clicked = written
    await publish(cloud.redis, meeting_id, version)
    await cloud.redis.enqueue_job("embed_minutes_task", meeting_id, _job_id=f"embed:{meeting_id}:{version}")
    # Minutes no click asked for count toward unattended success (D39); a regenerate's are `clicked`. A section
    # left incomplete still counts the run as ready: its count goes in the verdict.
    left = f":incomplete:{len(doc.incomplete)}" if doc.incomplete else ""
    reached = ("ready:clicked" if clicked else "ready:unattended") + left
    task_log("generate_minutes", str(meeting_id), engine, elapsed(started), reached)


async def embed_minutes_task(ctx: Ctx, meeting_id: UUID) -> None:
    """The minutes' sections as vectors (D38): read, then embedded with no transaction open, then written unless
    newer minutes landed meanwhile (their own task embeds them). A failure retries and then gives up: the meeting
    stays ready, only search misses it."""
    started = time.monotonic()
    cloud = cloud_of(ctx)
    engine = cloud.embeddings.engine
    try:
        async with cloud.sessions() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None or meeting.state != "ready" or meeting.minutes is None:
                return
            doc = MinutesOut.model_validate(meeting.minutes)
            # The search's sections, and Ask's (kind `ask`, never a search hit), embedded once here.
            made_at, sections = meeting.minutes_generated_at, [*minutes.chunks_of(doc), *qa.ask_chunks(doc)]
        vectors = await cloud.embeddings.embed([body for _, _, body in sections])
        async with cloud.sessions() as db:
            meeting = await db.get(Meeting, meeting_id)
            if meeting is None or meeting.minutes_generated_at != made_at:
                return
            await meeting_index.write_sections(db, meeting_id, sections, vectors)
            await db.commit()
    except Exception as error:
        if ctx["job_try"] < MAX_TRIES:
            task_log("embed_minutes", str(meeting_id), engine, elapsed(started), "retry")
            raise Retry(defer=2 ** ctx["job_try"]) from error
        task_log("embed_minutes", str(meeting_id), engine, elapsed(started), "gave_up")
        return
    task_log("embed_minutes", str(meeting_id), engine, elapsed(started), f"chunks:{len(sections)}")


async def purge_expired(ctx: Ctx) -> None:
    """The undo window's end (D28), daily at 03:00 UTC; idempotent, so a slow pass costs nothing."""
    started = time.monotonic()
    count = await purge.purge_all(cloud_of(ctx))
    task_log("purge_expired", "-", "-", elapsed(started), f"recordings:{count}")


REMEMBER_VOICEPRINT = sqlalchemy.text(
    "UPDATE transcription_jobs SET vendor_refs = vendor_refs || jsonb_build_object('voiceprints',"
    " coalesce(vendor_refs->'voiceprints', '{}')"
    " || jsonb_build_object(CAST(:speaker AS text), CAST(:vendor_job AS text))) WHERE id = :job"
)


async def remember_voiceprint_job(cloud: Cloud, job_id: UUID, speaker_id: UUID, vendor_job: str) -> None:
    """The speaker's voiceprint job, which on_voiceprint must find before it stores anything; then a check a minute
    later, in case pyannoteAI's webhook never comes."""
    async with cloud.sessions() as db:
        await db.execute(REMEMBER_VOICEPRINT, {"speaker": str(speaker_id), "vendor_job": vendor_job, "job": job_id})
        await db.commit()
    await cloud.redis.enqueue_job(
        "on_voiceprint",
        str(job_id),
        str(speaker_id),
        vendor_job,
        _job_id=f"vp-check:{speaker_id}",
        _defer_by=VOICEPRINT_CHECK_S,
    )


async def create_voiceprints(ctx: Ctx, job_id: UUID) -> None:
    """A clip of each speaker's cleanest span, sent to pyannoteAI; only with the device's consent (D8, Q50). A
    failure never touches the transcript: after 3 tries it is logged and given up."""
    started = time.monotonic()
    cloud = cloud_of(ctx)
    strategy = strategy_of(ctx)
    async with cloud.sessions() as db:
        job = await load_job(db, job_id)
        if job is None or job.state != "ready" or job.upload_id is None:
            return
        recording = await db.get_one(Recording, job.recording_id)
        device = await db.get_one(Device, recording.device_id)
        # A speaker already sent on an earlier try isn't sent (and paid for) again.
        submitted = list(job.vendor_refs.get("voiceprints", {}))
        speakers = list(
            await db.scalars(
                select(Speaker).where(
                    Speaker.recording_id == job.recording_id,
                    Speaker.voiceprint.is_(None),
                    Speaker.deleted_at.is_(None),
                    Speaker.id.not_in([UUID(each) for each in submitted]),
                )
            )
        )
        upload = await db.get_one(Upload, job.upload_id)
    if not device.voiceprint_consent or recording.deleted_at is not None or not speakers:
        return
    try:
        chosen = voiceprints.spans(await strategy.diarization(job.vendor_refs))
        with tempfile.TemporaryDirectory() as folder:
            audio = Path(folder) / "audio.m4a"
            await cloud.storage.download(upload.s3_key, str(audio))
            for speaker in speakers:
                span = chosen.get(speaker.vendor_label)
                if span is None:
                    continue
                clip = Path(folder) / f"{speaker.id}.wav"
                await ctx["cut"](audio, span[0], span[1], clip)
                key = f"clips/{job_id}/{speaker.id}.wav"
                await cloud.storage.upload(str(clip), key, "audio/wav")
                hook = webhook(ctx, "pyannote", job_id, kind="voiceprint", speaker=str(speaker.id))
                vendor_job = await strategy.voiceprint(cloud.storage.presign_get(key, CLIP_URL_S), hook)
                await remember_voiceprint_job(cloud, job_id, speaker.id, vendor_job)
    except Exception as error:
        if ctx["job_try"] < VOICEPRINT_TRIES:
            task_log("create_voiceprints", str(job_id), "pyannote", elapsed(started), f"retry:{type(error).__name__}")
            raise Retry(defer=2 ** ctx["job_try"]) from error
        task_log("create_voiceprints", str(job_id), "pyannote", elapsed(started), f"gave_up:{type(error).__name__}")
        return
    task_log("create_voiceprints", str(job_id), "pyannote", elapsed(started), f"asked:{len(chosen)}")


async def on_voiceprint(ctx: Ctx, job_id: str, speaker_id: str, vendor_job: str) -> None:
    """Seals the voiceprint and stores it on the speaker, once `vendor_job` is the job recorded for that speaker,
    unless the speaker has one, was deleted, or consent was withdrawn since. A webhook can arrive before the record
    is written: it waits for it. Run by the webhook and by the check a minute after the request, so either one
    stores it."""
    started = time.monotonic()
    cloud = cloud_of(ctx)
    speaker_uuid = UUID(speaker_id)
    async with cloud.sessions() as db:
        job = await load_job(db, UUID(job_id))
    recorded = None if job is None else job.vendor_refs.get("voiceprints", {}).get(speaker_id)
    if recorded is None or recorded != vendor_job:
        if recorded is None and ctx["job_try"] < MAX_TRIES:
            raise Retry(defer=2)
        task_log("on_voiceprint", speaker_id, "pyannote", elapsed(started), "stray")
        return
    try:
        voiceprint = await strategy_of(ctx).voiceprint_output(vendor_job)
    except VendorUnavailable as error:
        if ctx["job_try"] < MAX_TRIES:
            raise Retry(defer=2 ** ctx["job_try"]) from error
        task_log("on_voiceprint", speaker_id, "pyannote", elapsed(started), "gave_up")
        return
    if voiceprint is None:
        if ctx["job_try"] < MAX_TRIES:
            raise Retry(defer=5)
        task_log("on_voiceprint", speaker_id, "pyannote", elapsed(started), "not_ready")
        return
    sealed, key_id = sealing.seal(cloud.config.voiceprint_keys, speaker_uuid, voiceprint.encode())
    consenting = (
        select(Device.id)
        .join(Recording, Recording.device_id == Device.id)
        .where(Recording.id == Speaker.recording_id, Device.voiceprint_consent.is_(True))
        .exists()
    )
    async with cloud.sessions() as db:
        stored = await db.execute(
            update(Speaker)
            .where(Speaker.id == speaker_uuid, Speaker.voiceprint.is_(None), Speaker.deleted_at.is_(None), consenting)
            .values(voiceprint=sealed, voiceprint_key_id=key_id)
            .returning(Speaker.id)
        )
        verdict = "stored" if stored.first() is not None else "skipped"
        await db.commit()
    task_log("on_voiceprint", speaker_id, "pyannote", elapsed(started), verdict)


async def transliterate(ctx: Ctx, recording_id: UUID) -> None:
    """Roman words for a transcript made with the Roman script (TRX-09)."""
    started = time.monotonic()
    async with cloud_of(ctx).sessions() as db:
        try:
            await transliteration.transliterate(db, ctx["tiers"], recording_id)
        except ProviderFailed as error:
            raise Retry(defer=2 ** ctx["job_try"]) from error
    task_log("transliterate", str(recording_id), "-", elapsed(started), "ok")


def deadline(job: TranscriptionJob, duration_ms: int) -> datetime:
    """When the vendors are late: max(15 min, 2 x the audio) after the submit."""
    allowed = max(timedelta(minutes=15), 2 * timedelta(milliseconds=duration_ms))
    submitted = job.submitted_at or job.updated_at
    return submitted + allowed


async def sweep_waiting_jobs(ctx: Ctx) -> None:
    """The backstop for every way a task can end without moving its job (arq drops a task on any error but Retry):
    - queued for 24 h with no audio: failed `upload_expired`;
    - queued with its audio for 5 min (start crashed, or its enqueue was lost): started again, and failed
      `internal` once that has gone on for 24 h;
    - processing past its deadline plus a margin, in any stage (a timeout chain or a merge that ended): failed
      `vendor_timeout`.
    Failures refund the minutes and delete the vendors' copies."""
    now = datetime.now(UTC)
    async with cloud_of(ctx).sessions() as db:
        expired = await db.scalars(
            select(TranscriptionJob).where(
                TranscriptionJob.state == "queued",
                TranscriptionJob.upload_id.is_(None),
                TranscriptionJob.created_at < now - UPLOAD_EXPIRES,
            )
        )
        for job in list(expired):
            await fail_and_publish(ctx, db, job, "upload_expired", "sweep_waiting_jobs")
        stuck = await db.scalars(
            select(TranscriptionJob).where(
                TranscriptionJob.state == "queued",
                TranscriptionJob.upload_id.is_not(None),
                TranscriptionJob.created_at < now - UPLOAD_EXPIRES,
            )
        )
        for job in list(stuck):
            await fail_and_publish(ctx, db, job, "internal", "sweep_waiting_jobs")
        stalled = await db.scalars(
            select(TranscriptionJob.id).where(
                TranscriptionJob.state == "queued",
                TranscriptionJob.upload_id.is_not(None),
                TranscriptionJob.updated_at < now - STALLED_START,
            )
        )
        for job_id in list(stalled):
            # A fresh arq job ID: a crashed start keeps its own ID's result for an hour.
            again = f"start:{job_id}:{int(now.timestamp())}"
            await cloud_of(ctx).redis.enqueue_job("start_transcription", job_id, _job_id=again)
        late = await db.execute(
            select(TranscriptionJob, Recording.duration_ms)
            .join(Recording, Recording.id == TranscriptionJob.recording_id)
            .where(TranscriptionJob.state == "processing")
        )
        for job, duration_ms in late.all():
            if now > deadline(job, duration_ms) + SWEEP_MARGIN:
                await fail_and_publish(ctx, db, job, "vendor_timeout", "sweep_waiting_jobs")
        # A minutes run that stopped moving gets a new token and its task again; the old token can't write.
        stuck_minutes = await db.scalars(select(Meeting.id).where(meetings.stalled(STALLED_MINUTES)))
        for meeting_id in list(stuck_minutes):
            await meetings.rerun(db, cloud_of(ctx).redis, meeting_id, STALLED_MINUTES)
        # A meeting waiting on a transcript that landed a while ago, or failed before a newer one landed (the
        # worker died between the merge and the minutes' start), starts them now.
        waited = (Meeting.state == "awaiting_transcript") | (
            (Meeting.state == "failed") & (TranscriptionJob.updated_at > Meeting.updated_at)
        )
        orphaned = await db.scalars(
            select(Meeting.id)
            .join(TranscriptionJob, TranscriptionJob.recording_id == Meeting.recording_id)
            .where(
                waited,
                Meeting.deleted_at.is_(None),
                TranscriptionJob.state == "ready",
                TranscriptionJob.deleted_at.is_(None),
                TranscriptionJob.updated_at < now - STALLED_MINUTES,
            )
        )
        for meeting_id in list(orphaned):
            await meetings.begin(db, cloud_of(ctx).redis, meeting_id, clicked=False)
        # Sections a provider outage failed (the whole run, or some sections of ready minutes) are written again, as
        # no click, a few times: the sweep is the one owner of minutes re-runs.
        down = await db.execute(select(Meeting.id, Meeting.minutes).where(meetings.retryable(RETRY_FAILED)))
        for meeting_id, stored_minutes in list(down):
            marked = (stored_minutes or {}).get("incomplete") or []
            sections = [item["section"] for item in marked if item["code"] == "provider"]
            await meetings.begin(db, cloud_of(ctx).redis, meeting_id, clicked=False, sections=sections or None)


async def startup(ctx: Ctx) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # The code this worker runs, so one left running on old code shows beside the server's /healthz.
    logging.getLogger("snapbak_backend").info("code=%s", code_version())
    config = load(os.environ)
    if config.cloud is None:
        raise ValueError("Set DATABASE_URL: the worker needs the cloud settings (see .env.example).")
    cloud = await open_cloud(config.cloud, config.keys)
    ctx["cloud"] = cloud
    ctx["strategy"] = make_strategy(config.cloud, cloud.storage, ctx["redis"].enqueue_job)
    ctx["tiers"] = make_tiers(config)
    ctx.setdefault("cut", voiceprints.cut)


async def shutdown(ctx: Ctx) -> None:
    await strategy_of(ctx).aclose()
    for provider in ctx["tiers"].providers():
        await provider.aclose()
    await cloud_of(ctx).close()


FUNCTIONS = (
    start_transcription,
    on_vendor_webhook,
    check_vendor_timeout,
    merge_and_store,
    create_voiceprints,
    on_voiceprint,
    transliterate,
    func(generate_minutes, name="generate_minutes", timeout=MINUTES_TIMEOUT_S),
    embed_minutes_task,
    purge_expired,
)


class WorkerSettings:
    functions = FUNCTIONS
    cron_jobs = (
        cron(sweep_waiting_jobs, minute=set(range(0, 60, 5))),
        cron(purge_expired, hour=3, minute=0),
    )
    redis_settings = RedisSettings.from_dsn(os.environ.get("REDIS_URL") or "redis://127.0.0.1:16379/0")
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 20
    job_timeout = 300
    max_tries = MAX_TRIES
    keep_result = 3600
