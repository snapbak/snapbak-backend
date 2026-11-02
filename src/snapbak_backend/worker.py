"""The arq worker (D10; docs/cloud.md, "arq worker"): `arq snapbak_backend.worker.WorkerSettings`. Every task takes
IDs only, reads the row, and returns early when the row is already past its step, so a duplicate or a re-run is a
no-op. A vendor's 5xx or a dropped network retries the task; a vendor's 4xx fails the job."""

import asyncio
import contextlib
import json
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
    people,
    purge,
    qa,
    recordings,
    sealing,
    speaker_loop,
    transliteration,
    usage,
)
from snapbak_backend.services import speakers as speaker_rules
from snapbak_backend.services.cloud import Cloud, open_cloud
from snapbak_backend.services.events import publish
from snapbak_backend.services.metrics import task_log
from snapbak_backend.services.signatures import assemblyai_header
from snapbak_backend.transcription import voiceprints
from snapbak_backend.transcription.factory import make_strategy
from snapbak_backend.transcription.languages import U35_PRO, base
from snapbak_backend.transcription.merge import Merged, text, turns_of
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
# A task that runs the speaker loop: up to two model rounds and a rerun polled for RERUN_DEADLINE_S.
SPEAKER_LOOP_TIMEOUT_S = 900
AUDIO_URL_S = 7200
CLIP_URL_S = 3600
# AssemblyAI's own duration may pass 2 h by this much before the job fails `too_long`.
DURATION_SLACK_MS = 60_000
UPLOAD_EXPIRES = timedelta(hours=24)
# A queued job with its audio is started again after this long without a change.
STALLED_START = timedelta(minutes=5)
# The sweep fails a processing job this long after the timeout check would have.
SWEEP_MARGIN = timedelta(minutes=5)
# Stages past the vendors: the transcript is merging or stored, so no timeout check asks them again.
# How often a vendor's part is sent again after it failed on the vendor's side and asked for a retry.
RESUBMITS = 2
HELD = ("merging", "identifying")
# A job held for its speaker loop this long is past the loop task's timeout: that loop is over.
HELD_LOOP_OVER = timedelta(seconds=SPEAKER_LOOP_TIMEOUT_S) + SWEEP_MARGIN


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
    if status.state == "retry":
        await resubmitted(ctx, db, job, vendor)
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


async def resubmitted(ctx: Ctx, db: AsyncSession, job: TranscriptionJob, vendor: Vendor) -> None:
    """A vendor that failed its part on its own side and asked for it again: the job is queued once more with the
    other vendor's part kept, so the start sends only this one, at most RESUBMITS times; then it fails. Raises Moved
    when another writer won."""
    count = int(job.vendor_refs.get("resubmits", 0))
    if count >= RESUBMITS:
        if not await fail_and_publish(ctx, db, job, "vendor_error", f"vendor:{vendor}"):
            raise Moved(job.id)
        return
    refs = {key: value for key, value in job.vendor_refs.items() if key != vendor}
    if not await jobs.advance(db, job, state="queued", stage=None, vendor_refs={**refs, "resubmits": count + 1}):
        raise Moved(job.id)
    await db.commit()
    await publish(cloud_of(ctx).redis, job.id, job.version)
    # Its own arq job ID: the first start's result is kept for an hour.
    again = f"start:{job.id}:{count + 1}"
    await cloud_of(ctx).redis.enqueue_job("start_transcription", job.id, _job_id=again)


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
        if job is None or job.state != "processing" or job.stage in HELD:
            return
        recording = await db.get_one(Recording, job.recording_id)
        if datetime.now(UTC) > deadline(job, recording.duration_ms):
            await fail_and_publish(ctx, db, job, "vendor_timeout", "check_vendor_timeout")
            return
        with contextlib.suppress(VendorUnavailable, Moved):
            for vendor in VENDORS:
                if job.state == "processing":
                    await settle(ctx, db, job, vendor)
        if job.state == "processing" and job.stage not in HELD:
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


async def resume_held(db: AsyncSession, job: TranscriptionJob) -> Device | None:
    """A job held for its speaker loop resuming it: its clock starts again (the caller commits), so the sweep times
    the loop that resumes now, not the first; the recording's device, or None when the job is not held."""
    if job.stage != "identifying":
        return None
    await db.execute(
        update(TranscriptionJob).where(TranscriptionJob.id == job.id).values(updated_at=sqlalchemy.func.now())
    )
    recording = await db.get_one(Recording, job.recording_id)
    device = await db.get_one(Device, recording.device_id)
    return device


async def merge_and_store(ctx: Ctx, job_id: UUID) -> None:
    started = time.monotonic()
    cloud = cloud_of(ctx)
    strategy = strategy_of(ctx)
    async with cloud.sessions() as db:
        job = await load_job(db, job_id)
        if job is None or job.state != "processing":
            return
        held = await resume_held(db, job)
        await db.commit()
    if held is not None:
        # A run again (a restart, the sweep) of a merge already stored: storing again would give every line a new id
        # under the loop's changes, so it only finishes the loop, outside any session (the loop runs for minutes).
        await finished(ctx, job, held, studio=True, started=started)
        return
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
        # A meeting's speaker loop runs after its draft; any other recording's runs now, before it is ready (MOM-08).
        studio = await meetings.live_meeting(db, job.recording_id) is None
        values: dict[str, Any] = {"stage": "identifying"} if studio else {"state": "ready", "stage": None}
        if not await jobs.advance(db, job, language_detected=merged.language, **values):
            await db.rollback()
            raise Retry(defer=0.1)
        await db.commit()
        device = await db.get_one(Device, recording.device_id)
    await publish(cloud.redis, job_id, job.version)
    await finished(ctx, job, device, studio=studio, started=started)


async def finished(ctx: Ctx, job: TranscriptionJob, device: Device, studio: bool, started: float) -> None:
    """After the store: a Studio transcript's loop and then ready, the vendors' copies deleted and the follow-on tasks
    queued."""
    cloud = cloud_of(ctx)
    if studio and not await identified_then_ready(ctx, job.id, job.recording_id):
        return
    await strategy_of(ctx).cleanup(job.vendor_refs)
    if device.voiceprint_consent:
        await cloud.redis.enqueue_job("create_voiceprints", job.id, _job_id=f"voiceprints:{job.id}")
    if job.script == "roman":
        await cloud.redis.enqueue_job("transliterate", job.recording_id, _job_id=f"translit:{job.recording_id}")
    await start_minutes(cloud, job.recording_id)
    task_log("merge_and_store", str(job.id), "-", elapsed(started), "ready")


async def identified_then_ready(ctx: Ctx, job_id: UUID, recording_id: UUID) -> bool:
    """A recording's speaker loop, then its transcript ready: the user chose to hold it until the loop is done
    (MOM-08). A loop that fails leaves the transcript as it came; False when the job moved on meanwhile (trashed)."""
    cloud = cloud_of(ctx)
    # The transcript is never held by a loop that breaks: it goes ready as the vendors gave it.
    await loop_or_none(ctx, recording_id, None, job_id, None)
    with contextlib.suppress(Exception):
        await speaker_progress(cloud, recording_id, None, "done")
    async with cloud.sessions() as db:
        job = await load_job(db, job_id)
        if job is None or job.state != "processing":
            return False
        won = await jobs.advance(db, job, state="ready", stage=None)
        await db.commit()
    if won:
        await publish(cloud.redis, job_id, job.version)
    return won


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


# How often a rerun is polled, and how long it may take before the loop goes on without it (names only).
RERUN_POLL_S = 3
RERUN_DEADLINE_S = 150
# The speaker loop's rerun kept on the transcription job, in one statement beside any voiceprint job written meanwhile.
REMEMBER_RERUN = sqlalchemy.text(
    "UPDATE transcription_jobs SET vendor_refs = vendor_refs || jsonb_build_object('rediarize', CAST(:rerun AS jsonb))"
    " WHERE id = :job"
)


async def speaker_progress(
    cloud: Cloud, recording_id: UUID, meeting_id: UUID | None, stage: str, failed: bool = False
) -> None:
    """The loop's stage stored on the recording (the rest as it is: a user's answer meanwhile stays), `failed` when
    it broke; a meeting's version moves too, so the app's long-poll wakes and shows the stage (MOM-08's live
    story)."""
    async with cloud.sessions() as db:
        await speaker_loop.restaged(db, recording_id, stage=stage, **({"failed": True} if failed else {}))
        row = None if meeting_id is None else await meetings.changed(db, Meeting.id == meeting_id)
        await db.commit()
    if row is not None:
        await publish(cloud.redis, row.id, row.version)


async def voiceprints_of(cloud: Cloud, recording_id: UUID) -> dict[str, tuple[UUID, str]]:
    """Each live speaker's ID and voiceprint, unsealed, by its identify label `p<N>` (only with the device's consent
    are there any)."""
    async with cloud.sessions() as db:
        known = await recordings.speakers_of(db, recording_id)
    prints = {}
    for speaker in known.values():
        n = people.number_of(speaker)
        if speaker.voiceprint is not None and n is not None:
            unsealed = sealing.unseal(cloud.config.voiceprint_keys, speaker.id, speaker.voiceprint)
            prints[f"p{n}"] = (speaker.id, unsealed.decode())
    return prints


async def heard_again(
    ctx: Ctx, recording_id: UUID, run: UUID, prints: dict[str, tuple[UUID, str]], count: int
) -> speaker_loop.Heard | None:
    """pyannote again for the speaker loop (MOM-08): identify against the speakers' voiceprints when two or more have
    one, else a diarization at `count` voices. The day's audio is reserved first and the vendor's job kept on the
    transcription job before it is awaited, so a resumed run (`run`) never pays twice. None when there is no quota
    left, the vendor fails, or it takes past RERUN_DEADLINE_S: the loop then changes names only."""
    cloud, strategy = cloud_of(ctx), strategy_of(ctx)
    kind = "identify" if len(prints) >= 2 else "diarize"
    async with cloud.sessions() as db:
        job = await jobs.live(db, recording_id)
        recording = await db.get_one(Recording, recording_id)
        if job is None or job.upload_id is None:
            return None
        kept = job.vendor_refs.get("rediarize") or {}
        same_run = kept.get("run") == str(run) and kept.get("kind") == kind
        vendor_job = kept.get("jobId") if same_run else None
        if vendor_job is None:
            # The day's audio is reserved first and committed (the day's row is not held during the vendor's call);
            # a vendor that fails gives it back, so nothing is charged for a rerun that never ran.
            seconds, device = -(-recording.duration_ms // 1000), recording.device_id
            if not await usage.reserve(db, device, seconds):
                return None
            await db.commit()
            upload = await db.get_one(Upload, job.upload_id)
            audio = cloud.storage.presign_get(upload.s3_key, AUDIO_URL_S)
            try:
                voiceprints = {label: print_ for label, (_, print_) in prints.items()}
                identify = kind == "identify"
                started = strategy.identify(audio, voiceprints) if identify else strategy.rediarize(audio, count)
                vendor_job = await started
                remembered = {"run": str(run), "kind": kind, "count": count, "jobId": vendor_job}
                await db.execute(REMEMBER_RERUN, {"rerun": json.dumps(remembered), "job": job.id})
                await db.commit()
            except BaseException as error:
                # Nothing kept, nothing charged: a vendor that fails, or a task cancelled mid-way, gives the day back.
                # A rerun the vendor started but we never kept needs no cleanup: pyannoteAI deletes its output itself.
                await db.rollback()
                await usage.release(db, device, seconds)
                await db.commit()
                if isinstance(error, VendorError):
                    return None
                raise
    labels = {label: speaker for label, (speaker, _) in prints.items()}
    deadline = time.monotonic() + RERUN_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            found = await (
                strategy.identification(vendor_job) if kind == "identify" else rediarization(strategy, vendor_job)
            )
        except VendorError:
            return None
        if found is not None:
            same = speaker_rules.voice_groups(found.get("voiceprints"), labels)
            return speaker_loop.Heard(turns_of(found.get("exclusiveDiarization") or []), same)
        await asyncio.sleep(RERUN_POLL_S)
    return None


async def rediarization(strategy: TranscriptionStrategy, vendor_job: str) -> dict[str, Any] | None:
    """A diarization rerun's turns in identify's shape, so both reruns read alike."""
    turns = await strategy.rediarization(vendor_job)
    return None if turns is None else {"exclusiveDiarization": turns}


async def identified(
    ctx: Ctx, recording_id: UUID, meeting_id: UUID | None, run: UUID, draft: MinutesOut | None
) -> speaker_loop.Outcome | None:
    """The speaker loop for the recording (MOM-08), `run` its token: the same token again carries on (services/
    speaker_loop.py). None, asking nothing, when the loop is done and no whole run asked for it again (a whole run
    and Identify speakers set its stage back to `reading`): a sections-only rewrite after an undo never runs it."""
    cloud = cloud_of(ctx)
    async with cloud.sessions() as db:
        recording = await db.get_one(Recording, recording_id)
    stored = None if recording.speaker_pass is None else speaker_loop.pass_of(recording)
    if stored is not None and stored.stage == "done" and stored.run != str(run):
        return None
    # A loop found mid-way (the run died; the sweep handed it a new token) goes on as the same run: it asks nothing
    # it already asked and reuses the rerun it already paid for.
    if stored is not None and stored.stage not in ("reading", "done") and stored.run is not None:
        run = UUID(stored.run)
    prints = await voiceprints_of(cloud, recording_id)
    progress = partial(speaker_progress, cloud, recording_id, meeting_id)
    rerun = speaker_loop.Rerun(voiceprints=len(prints) >= 2, run=partial(heard_again, ctx, recording_id, run, prints))
    outcome = await speaker_loop.run(ctx["tiers"], cloud.sessions, recording_id, run, draft, rerun, progress)
    return outcome


async def loop_or_none(
    ctx: Ctx, recording_id: UUID, meeting_id: UUID | None, run: UUID, draft: MinutesOut | None
) -> speaker_loop.Outcome | None:
    """The speaker loop (after a meeting's draft, or on a transcript alone); a loop that breaks never holds anything
    back: its stage goes done, marked failed, and what it applied before the break still counts (its merges are
    retold). The log names the error's type only, never its message (no words in logs)."""
    started = time.monotonic()
    try:
        outcome = await identified(ctx, recording_id, meeting_id, run, draft)
    except Lost:
        raise
    except Exception as error:
        task_log(
            "speaker_loop", str(meeting_id or recording_id), "-", elapsed(started), f"failed:{type(error).__name__}"
        )
        with contextlib.suppress(Exception):
            await speaker_progress(cloud_of(ctx), recording_id, meeting_id, "done", failed=True)
        # A round may have merged before the break: its tokens are still retold, so no one reads "someone". No section
        # is written again for its moves.
        async with cloud_of(ctx).sessions() as db:
            outcome = await speaker_loop.outcome_of(db, recording_id)
        outcome.moved = set()
    return outcome


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
        # The model that wrote the stored minutes, for a run that writes no section (Identify speakers' run).
        written_by = meeting.minutes_model or ctx["tiers"].polish.engine
        # The version the minutes are built from, read before the model runs, so an edit landing mid-call leaves
        # the minutes stale rather than silently current (D29).
        snapshot = meeting.transcript_version
        # The day the meeting was held, so "Friday" resolves to a real date (UTC: the recording's own clock).
        day = meeting.started_at.date().isoformat() if meeting.started_at else None
        doc = meetings.minutes_of(meeting) or minutes.empty(list(minutes.CALLS))
        # A run of some sections (a retry, Identify speakers, an undo's rewrite) leaves the minutes as stale as they
        # were: what made them stale is still not in them. A whole run's start cleared the version (meetings.begin),
        # so it is current as of its snapshot, however much of it was left when it was picked up.
        if meeting.minutes_transcript_version is not None:
            snapshot = meeting.minutes_transcript_version
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
        engine = written_by
        if doc.pending:
            doc, engine = await minutes.run(ctx["tiers"], title, marked, lines, speakers, doc, saved, day)
        # The draft, then who the speakers are (MOM-08): a merge retells its people tokens, and only the sections that
        # cite a moved line are written again. Names need nothing: tokens show each speaker as they are now.
        outcome = None if minutes.failure(doc) else await loop_or_none(ctx, recording_id, meeting_id, run, doc)
        if outcome is not None:
            doc = people.retold(doc, outcome.merges)
            again = speaker_rules.touched(doc, outcome.moved)
            if again:
                # Stored pending before the stage says so: a run that dies mid-rewrite writes these sections when it
                # picks up (a resumed run past `rewriting` writes nothing again itself).
                doc = doc.model_copy(update={"pending": again})
                await saved(doc)
                await speaker_progress(cloud, recording_id, meeting_id, "rewriting")
                async with cloud.sessions() as db:
                    lines = await recordings.live_lines(db, recording_id)
                    speakers = await recordings.speakers_of(db, recording_id)
                doc, engine = await minutes.run(ctx["tiers"], title, marked, lines, speakers, doc, saved, day)
            await speaker_progress(cloud, recording_id, meeting_id, "done")
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
            speakers = await recordings.speakers_of(db, meeting.recording_id)
            # Embedded as words: each person by the name they have now (D41).
            doc = people.rendered_doc(MinutesOut.model_validate(meeting.minutes), speakers)
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


async def finish_held(ctx: Ctx, job: TranscriptionJob) -> None:
    """A Studio job whose speaker loop stopped (its task timed out or died): the loop ends failed, so the merge, run
    once more under one arq ID per job, asks nothing and makes the stored transcript ready."""
    with contextlib.suppress(Exception):
        await speaker_progress(cloud_of(ctx), job.recording_id, None, "done", failed=True)
    # One arq ID per stall (a resumed loop restarts the clock), so a merge that failed never blocks the next one.
    held = f"merge:{job.id}:held:{int(job.updated_at.timestamp())}"
    await cloud_of(ctx).redis.enqueue_job("merge_and_store", job.id, _job_id=held)


async def sweep_waiting_jobs(ctx: Ctx) -> None:
    """The backstop for every way a task can end without moving its job (arq drops a task on any error but Retry):
    - queued for 24 h with no audio: failed `upload_expired`;
    - queued with its audio for 5 min (start crashed, or its enqueue was lost): started again, and failed
      `internal` once that has gone on for 24 h;
    - processing past its deadline plus a margin (a timeout chain or a merge that ended): failed `vendor_timeout`;
    - held for its speaker loop past the loop's own timeout (that loop is over): made ready, never failed, since its
      transcript is stored;
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
            if job.stage == "identifying":
                if job.updated_at < now - HELD_LOOP_OVER:
                    await finish_held(ctx, job)
                continue
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


async def identify_speakers(ctx: Ctx, recording_id: UUID, run: UUID) -> None:
    """The speaker loop asked for again on a recording that is no meeting (the app's Identify speakers, MOM-08),
    under the route's run token, so a retried job carries on: the app reads the transcript again when its stage is
    done."""
    started = time.monotonic()
    outcome = None
    try:
        async with cloud_of(ctx).sessions() as db:
            job = await jobs.live(db, recording_id)
        if job is not None and job.state == "ready":
            outcome = await loop_or_none(ctx, recording_id, None, run, None)
    finally:
        # Done whatever happened (no transcript any more, a timeout): the route set `reading`, and a stage left there
        # would refuse every later Identify.
        with contextlib.suppress(Exception):
            await speaker_progress(cloud_of(ctx), recording_id, None, "done")
    changed = 0 if outcome is None else len(outcome.changes)
    task_log("identify_speakers", str(recording_id), "-", elapsed(started), f"changes:{changed}")


FUNCTIONS = (
    start_transcription,
    on_vendor_webhook,
    check_vendor_timeout,
    # The speaker loop runs inside it for a recording that is no meeting: two model calls and a rerun (MOM-08).
    func(merge_and_store, name="merge_and_store", timeout=SPEAKER_LOOP_TIMEOUT_S),
    func(identify_speakers, name="identify_speakers", timeout=SPEAKER_LOOP_TIMEOUT_S),
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
