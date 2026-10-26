# Cloud transcripts and share links

Snapbak Cloud turns a recording's audio into a transcript with words, times,
speakers and names (PRD TRX-01, TRX-08, TRX-09, CLD-01 to CLD-04), translates
its captions into other languages (TRX-07), and serves a recording's share link
(SHR-01, D18). The specs are
`../snapbak/docs/engineering/studio/13-transcription-pipeline/`,
`16-caption-translation/` and `12-share-links/` (HLD and LLD). This page is the
backend's reference: routes, tables, the worker, the share page, how to run it
and how to test it. Dictation (`/v1/polish`, `/v1/command`) is unchanged and
still stores nothing.

The cloud starts only when `DATABASE_URL` is set: `main.py` imports
`api/cloud.py` inside its startup, which opens the database, Redis and S3 and
adds the cloud routes after the dictation ones. A dictation-only server loads
none of SQLAlchemy, boto3 or arq, so it starts as fast and as small as before
(145 ms, 55 MB; `tests/unit/test_dictation_only.py` checks the imports).

## Pieces

| Piece    | Where                                     | Job                                                                                                                                                                          |
| -------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API      | `main.py`, `api/v1/*.py`                  | devices, recordings, uploads, jobs, the transcript, edits, webhooks                                                                                                          |
| Worker   | `worker.py` (arq)                         | submits to the vendors, takes webhooks, merges, stores, voiceprints                                                                                                          |
| Postgres | `db/models.py`, `migrations/`             | the source of truth: devices, recordings, jobs, speakers, lines                                                                                                              |
| Redis    | arq's queue, and pub/sub `job:{id}`       | tasks, and the wake-up for long-polls (the row is the truth)                                                                                                                 |
| S3       | `services/storage.py` (boto3 in a thread) | the `snapbak-media` bucket, by folder: `audio/` (the m4a, kept until a trashed meeting is purged), `raw/` (pyannote output), `clips/`, `shares/` (each share's video and poster, one folder a version) |
| Page     | `api/share_page.py`, `templates/`         | `snapbak.site/v/{slug}` and its helper routes                                                                                                                                |
| Vendors  | `transcription/`                          | the `TranscriptionStrategy` facade, AssemblyAI + pyannoteAI, a fake                                                                                                          |

Code, top to bottom: `config/settings.py` (`CloudConfig`), `db/`, `services/`
(`failures`, `cloud`, `storage`, `events`, `jobs`, `usage`, `recordings`,
`shares`, `sealing`, `signatures`, `retention`, `transliteration`, `translate`),
`ai/languages.py` and `ai/prompts/translate.py`, `transcription/` (`languages`,
`merge`, `strategy`, `assemblyai_pyannote`, `fake`, `factory`, `voiceprints`),
`api/v1/`, `api/share_page.py`, `worker.py`.

## Routes

JSON, UTF-8, under `/v1`. Every route but `POST /v1/devices`, the webhooks and
the [share page](#share-links) needs `Authorization: Bearer <device token>`. IDs
are lowercase UUIDs. Times are integer milliseconds of source time. Errors are
`{"error": {"code", "message"}}`; the app shows its own copy per `code`. Each
request logs one line: the route's template (`/v1/shares/{slug}`, never the
path), time, status. Never a body, a title, a slug or a word. The app turns
uvicorn's access log off at startup (`main.py`), however it is run:
`just serve`, ECS or anything else. Its lines would print each path, a share's
slug among them, and the caller's address. The ECS task needs no flag for it;
the E2E test runs the `just serve` command and checks the output.

| Route                                            | Answer                                                                                                                         |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `POST /v1/devices`                               | 201 `{deviceId, token, voiceprintConsent}`; the same `installId` keeps its device and usage                                    |
| `PUT /v1/devices/me/consent`                     | 200 `{voiceprintConsent, consentAt}`                                                                                           |
| `PUT /v1/recordings/{id}`                        | 201 created or 200 updated, `{recordingId, title, durationMs, recordingType}` (`video` or `audio`, MEETINGS.md)                |
| `DELETE /v1/recordings/{id}`                     | 204, soft delete of the recording and every row under it; again is 204                                                         |
| `POST /v1/recordings/{id}/uploads`               | 201 `{uploadId, partSize, expiresAt, urls: [{n, url}]}`, 8 MiB parts, URLs for 1 h; `kind` `audio` or `share` (with `shareId`) |
| `POST /v1/uploads/{uploadId}/parts`              | 200, fresh URLs for the parts asked                                                                                            |
| `POST /v1/uploads/{uploadId}/complete`           | 200 `{uploadId, state, jobId}`, or `{uploadId, state, share}` for a share; a retry is a no-op                                  |
| `POST /v1/recordings/{id}/shares`                | 201 new or 200 existing `{shareId, slug, url, state, version, pageState, expiresAt, posterUploadUrl, type}`                    |
| `GET /v1/recordings/{id}/shares`                 | 200 the share, `posterUploadUrl: null`; 404 when the recording has none                                                        |
| `PATCH /v1/shares/{slug}`                        | 200 the share; `state` `live` or `disabled`, `expiresIn` `never`, `7d` or `30d`                                                |
| `DELETE /v1/shares/{slug}`                       | 204, soft delete; again is 204                                                                                                 |
| `POST /v1/recordings/{id}/transcription`         | 201 `{jobId, state, version, needsUpload}`; 409 carries `job`; 429 carries `resetsAt`                                          |
| `GET /v1/jobs/{jobId}?wait=0..25&since=n`        | 200 `{jobId, recordingId, state, stage, failure, language, version}`, long-poll                                                |
| `GET /v1/recordings/{id}/transcript`             | 200 `{recordingId, language, script, speakers, lines}` once ready                                                              |
| `PATCH /v1/lines/{lineId}`                       | 200 `{rev}`; the whole line, last write wins; clears its Roman words                                                           |
| `PATCH /v1/speakers/{speakerId}`                 | 200 the speaker, `nameSource: "user"`                                                                                          |
| `PUT /v1/recordings/{id}/speakers/{speakerId}`   | 201 or 200 the speaker, made on the Mac with **New speaker**                                                                   |
| `POST /v1/recordings/{id}/transliterate`         | 200 `{lines: [{id, romanText, romanWords}]}`; 502 `provider` if the model can't answer                                         |
| `POST /v1/recordings/{id}/captions/translations` | 200 `{track, failed}`: up to 200 lines into the language's track; lines whose `sourceHash` matches skip the model              |
| `PATCH /v1/caption-tracks/{trackId}/lines/{idx}` | 200 the line, `edited: true`; an older `rev` than the stored edit changes nothing                                              |
| `DELETE /v1/caption-tracks/{trackId}`            | 204, soft delete; again is 204                                                                                                 |
| `POST /v1/webhooks/assemblyai?job=`              | 204; `X-Snapbak-Webhook` must be the job's HMAC, else 401                                                                      |
| `POST /v1/webhooks/pyannote?job=&kind=&speaker=` | 204; `X-Signature` over `v0:{ts}:{body}`, hex or base64, else 401; the timestamp's age is not checked                          |
| `POST /v1/recordings/{id}/restore`               | 200 `{recordingId, title, durationMs, recordingType}`; out of the trash inside 30 days (D28); 404 when purged or unknown |
| `POST /v1/recordings/{id}/meeting`               | 201 `{meetingId, state, version}`; the meeting envelope (MEETINGS.md); starts the transcript's job when there is none          |
| `GET /v1/recordings/{id}/meeting?wait&since`     | 200 the meeting: `{state, minutes, minutesFailure, stale, version, pins, startedAt, endedAt}`; long-poll as the job route; `minutes` partial while `generating`, `pending` the sections to come |
| `PATCH /v1/meetings/{meetingId}`                 | 200 the meeting; `title`                                                                                                       |
| `PUT /v1/meetings/{meetingId}/pins/{pinId}`      | 201 or 200 `{id, atMs, note}`; idempotent by the client's pin ID                                                               |
| `POST /v1/meetings/{meetingId}/minutes/regenerate` | 200 `{meetingId, state, version}`; minutes again from the transcript now (D29); body `{sections}` optional, none meaning all |
| `PUT /v1/meetings/{meetingId}/minutes`           | 200 the meeting; `{baseVersion, minutes}`, the user's whole edited minutes; what they added or changed is `byUser`            |
| `POST /v1/meetings/{meetingId}/ask`              | 200 `{answer, citations: [{lineId, quote}], supported}`; one question, from the meeting's lines and minutes (D18)               |
| `POST /v1/meetings/search`                       | 200 `{hits: [{meetingId, recordingId, title, kind, snippet, owner, score}]}`; over the minutes' sections (D38): `kind` `summary`, `action`, `decision` or `question`; `owner` an action item's, else null |

| HTTP | `code`                 | When                                                                                                                       |
| ---- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 401  | `unauthorized`         | missing, unknown or deleted token; a bad webhook signature                                                                 |
| 404  | `not_found`            | unknown, another device's, or soft-deleted; never 403                                                                      |
| 404  | `recording_not_found`  | the same, for a caption translation's recording                                                                            |
| 404  | `track_not_found`      | the same, for a caption track                                                                                              |
| 404  | `line_not_found`       | the track has no line with that `idx`                                                                                      |
| 409  | `transcript_not_ready` | a caption translation, or a meeting question, before the recording's transcript is `ready`                                 |
| 413  | `too_many_lines`       | a translation page over 200 lines                                                                                          |
| 422  | `language_unsupported` | a target outside `ai/languages.py`, or the transcript's own language                                                       |
| 429  | `translation_limit`    | the device would pass 20,000 translated lines today (UTC)                                                                  |
| 409  | `job_exists`           | a job is `queued`, `processing` or `ready` (no regenerate, Q20)                                                            |
| 409  | `upload_state`         | parts or complete of an aborted upload                                                                                     |
| 413  | `too_long`             | `durationMs` over 2 h                                                                                                      |
| 413  | `upload_too_large`     | audio over 200 MB; a share video over 1 GiB (create or upload)                                                             |
| 422  | `unsupported_language` | not `auto`, `hinglish` or one of the 18 Universal-3.5 Pro codes                                                            |
| 422  | `invalid`              | the body fails validation; a complete whose ETags S3 refuses (the upload stays open); turning on a share with no video yet |
| 422  | `upload_size_mismatch` | the completed object's size differs from `bytes`; the upload is aborted                                                    |
| 429  | `daily_limit`          | the device would pass 7,200 audio seconds today (UTC), or 50 meeting questions or searches (D33)                           |
| 409  | `meeting_exists`       | the recording is already a meeting (one live meeting per recording)                                                        |
| 409  | `minutes_not_ready`    | an edit of the minutes while they aren't `ready`                                                                           |
| 409  | `version_conflict`     | an edit of the minutes whose `baseVersion` is not the meeting's version                                                    |
| 413  | `minutes_too_large`    | edited minutes over 2 MB (bytes) as JSON (their only bound)                                                                |
| 409  | `minutes_running`      | a regenerate while a run is writing the minutes                                                                            |
| 429  | `rate_limited`         | an address registered 10 devices this hour                                                                                 |
| 429  | `share_limit`          | the device has 100 shares that are not deleted (a turned-off one counts)                                                   |
| 502  | `provider`             | the model behind transliteration, every batch of a translation page, a meeting question, or a meeting search (its embedding or re-rank), didn't answer; a question or search is given back |
| 500  | `internal`             | anything else                                                                                                              |

The shapes are pinned by `tests/contract/*.json` (see the LLD's contract table).

### Marketing site

The website at `snapbak.site` (its own repo, `../snapbak-website`) calls these
with no token. CORS allows `https://snapbak.site` and `http://localhost:3000`.
Fields are camelCase like the rest of the cloud API.

| Route                     | Answer                                                                                         |
| ------------------------- | ---------------------------------------------------------------------------------------------- |
| `GET /v1/waitlist/status` | 200 `{spotsTotal, spotsClaimed, waitlistOpen}`; the first read makes the 100-spot row          |
| `POST /v1/waitlist`       | 201 `{message}` for `{email, useCase?, source?}`; the same email again keeps the first row     |
| `POST /v1/waitlist/claim` | 200 the status, one more spot claimed, never past the total; `Bearer SNAPBAK_WAITLIST_TOKEN`   |
| `POST /v1/contact`        | 201 `{message}` for `{name, email, message, source?}`; message up to 5,000 characters, stored |

A bad email or an empty name or message is 422 `invalid`. Emails are stored
lowercased. Contact messages land in `contact_messages` and nothing alerts
anyone yet; read them with
`SELECT created_at, name, email, message FROM contact_messages ORDER BY 1 DESC`.
Like every cloud route, these log the route, time and status, never an email,
a name or a message.

## Job states

`queued` (with no upload, then with one) → `processing` (`transcribing`, then
`diarizing` or `merging`) → `ready` or `failed`. `ready` and `failed` are final.
Every change bumps `version` with a conditional update on the version read, so
of two writers only one wins, and publishes on `job:{id}`. A failed job refunds
its seconds to the day it was made. Retry makes a new row; the unique partial
index ignores failed rows.

Failure reasons: `unsupported_language`, `too_long`, `no_speech`,
`upload_expired`, `upload_invalid`, `vendor_error`, `vendor_timeout`,
`internal`.

The long-poll registers its wake-up before it reads the row, answers at once
when `version > since` or the job is final, else waits on Redis up to `wait`
seconds; a wake-up without a change waits on for the time left. The token check
ends its transaction at once, and each read takes its own short session, so a
waiting long-poll holds no database connection or transaction
(`tests/integration/test_longpoll_redis.py` checks `pg_stat_activity`). The
Redis listener subscribes again after a dropped connection (0.1 s backoff,
doubling to 5 s) and then wakes every waiting request to read its row again.

`POST /v1/devices` mints a token from an install ID alone, so each address may
register 10 devices an hour, counted in Redis. Behind the load balancer, run
uvicorn with `--proxy-headers` and `FORWARDED_ALLOW_IPS` set to the VPC range,
so the address is the caller's, not the balancer's.

## Meetings

A meeting is a thin envelope over a recording (MEETINGS.md): `meetings` holds
one live row per recording (partial unique index, as shares), the minutes
document, and its own state machine `awaiting_transcript → generating → ready |
failed`, version-bumped and published on `job:{meetingId}` like a job. The chain
runs unattended: `merge_and_store` starts the minutes when a live meeting's
transcript lands, `generate_minutes` writes the minutes one polish-model call
per section, each claim checked against the transcript lines
(`services/minutes.py`, D23), and `embed_minutes_task` writes the minutes'
sections as `meeting_chunks` vectors (pgvector; `fake:hashed` in tests): the
summary, and each action item, decision (with its rationale) and open question
on its own (D38, decisions added on 26 Oct 2026; only the dev database held
meetings from before, embedded again once by hand). A meeting's `startedAt` and
`endedAt` are its recording's creation and creation plus its length.

**Runs.** Each minutes run carries a token (`meetings.minutes_run`) that only
two places hand out: `meetings.begin` (from a starter state: the merge, a
promote on a ready transcript, a regenerate) and the sweep's `rerun`. The task
gets its token as an argument (arq job ID `minutes:{meetingId}:{token}`), claims
the meeting with it in one guarded update before it calls the model, marks it
alive every 60 s while it works, and writes each section and its end (ready or
failed, committed and published) only while it still holds the token; a run that
finds its token gone at a save stops there (verdict `lost`). Retries have one
owner each (grading 4): a section's call is asked once more 2 s after it fails,
then the section is marked; a run never retries itself (no arq retry); the sweep
alone runs the minutes again. A run has 30 minutes in all (arq's timeout for the
task); past it, the sweep's new run resumes at the first section still pending.
The sweep gives a run idle for 5 minutes a new token and its task again, so a
stalled run that wakes up can neither claim nor write.
`meetings.minutes_attempts` counts the runs the minutes got with no click (a
click counts afresh): after 3, a stalled run fails `provider` instead of
starting again (a run that always dies would otherwise be restarted for ever),
and minutes a provider outage left short 15 minutes ago with runs left (failed
`provider`, or ready with a section marked `provider`) get those sections
written again with no click, so an outage ends in minutes unattended (D39) and
costs at most 3 runs of calls, however long it lasts. The sweep also starts a
meeting still `awaiting_transcript`, or `failed` before a newer transcript
landed, whose job has been ready for 5 minutes (a worker that died between the
merge and the start). A transcription job that fails fails its waiting meeting
with the job's code (`too_long`, `vendor_error`, ...), published and logged as
that meeting's `generate_minutes` verdict, so no meeting waits forever and the
unattended count sees it. Regenerate is the app's Try again: on a ready
transcript it starts a run (a 409 `minutes_running` while one is writing, so the
app can say why; a run that has not marked itself alive for 10 minutes is taken
over with a new token); on a meeting whose transcript failed it makes a new job,
with the language and script the last one asked for, and the meeting waits on it
again. A second promote is a 409 `meeting_exists` before any job is made or
minute reserved.

**Sections** (minutes v2, 26 Oct 2026). The minutes are eleven sections:
`title`, `summary`, `mom` (markdown, one `### Topic` per topic), `actionItems`
(owner, due), `decisions` (rationale, alternatives), `openQuestions`, `risks`
(kind risk, blocker or dependency; owner), `followUps`, `nextAgenda`, `agenda`
(status covered, partial or unresolved) and `contradictions` (two or more
derivations from different lines). Ten calls write them, one after another in
that order, title and summary first; each sends the whole transcript and may
answer up to 64,000 tokens. The model puts something in only when the lines
support it, on any hint, and leaves a section empty (or `title` null) when
nothing does: never padding. Each section is stored and published the moment it
lands, the rest listed in `pending`, so the app shows the summary while the
other sections are written; the meeting stays `generating` until the last one,
then `ready`. A run starts from the stored document, so a crashed or restarted
run resumes at its first pending section. A section that fails (the model down
for its two tries, 2 s apart, or no answer that parses even after its repair)
is listed in `incomplete` with its code (`provider`, `bad_answer`) and
the rest go on; the minutes are `ready` with it marked, and a regenerate with
`{"sections": [...]}` writes only those again (any of the ten calls; the title
comes with the summary). Only when neither the summary nor the MoM could be
written does the run fail, with the summary's code. The minutes are always
English by rule too, not by the prompt alone: a summary or MoM with Devanagari
in it is refused and repaired like a bad answer, and an item whose text is
not English is dropped; only quotes keep the script they were spoken in.

**The detail guard.** After the sections, rules list the transcript's details
(`services/details.py`): every number, time or date word (English and the
Hinglish kal, parso, aaj, subah, shaam, raat), name (a capitalized word inside a
sentence, and each speaker's chosen name) and commitment ("I'll", "we will",
"need to", karunga, karenge, dunga, ...), one per line that holds one, none
when negated ("don't need to"), conditional ("would have to", "we'd have to")
or rhetorical ("let's see", "let's say"). A detail is kept when its words are
somewhere in the minutes; a commitment when an action item or follow-up cites
its line, or the completion judged it none. When fewer than 95 % are kept, any
commitment is uncovered, or an agenda topic has no `### ` section of its own,
one completion call gets the summary, the MoM, the missing details and the
lines. It works them in, gives each missing topic a heading of its own, and
answers for every missing commitment: an action item (owner and due when said,
checked as any), or, only when it is no commitment (a proposal nobody took up,
a remark), an entry in `notCommitments` (`{lineId, reason}`, server-owned like
the pins, kept through an edit). A judgment on a line the guard did not ask
about is dropped. Any commitment the first answer left unanswered gets one
more call for those alone. The answer is taken only when it makes neither
measure worse (details kept, agenda topics without a section) and one better,
and new text that would lose a topic's heading is left out while the answer's
items still count, so the guard never loses anything. The agenda call gets the
MoM's headings, written before it, and names each topic by one of them. Only
counts reach a log.

**Item ids** are the server's, never the model's (whose `act_1` repeats across
runs): each section call numbers its items `<prefix>_<tag>_<n>`, the tag new
each call, so an edit resent over a newer run never takes another item's
citations. An id that repeats within a section anyway (in an edit) becomes
`<id>_<position>`, its index there from 0, the rule Studio applies to what it
reads.

**Edits.** `PUT /v1/meetings/{id}/minutes` takes the user's whole document and
the version they edited (`baseVersion`, else 409 `version_conflict`; 409
`minutes_not_ready` while a run writes). An item that is new or changed is
marked `byUser` and needs no derivation: a new one keeps none, a changed AI
item keeps the derivations the server checked (the client cannot hand in new
ones); a changed `title`, `summary` or `mom` joins `userEdited`. Pins,
`pending` and `incomplete` stay the server's. The edit bumps the version,
publishes and embeds the minutes again. A regenerate replaces only the model's
items and texts: `byUser` items and `userEdited` texts stay. There is no length
limit on any section; an edit over 2 MB (bytes, as JSON) is a 413
`minutes_too_large`, refused on its Content-Length before the body is read.
Registering a recording again never changes its type (it is set at insert), a
speaker PUT that changes nothing marks no minutes stale, and a meeting the trash
left waiting on a job it failed comes back from a restore failed with that job's
code, so the app offers Try again.

**Due dates.** An action item's or follow-up's `due` is a calendar date as
`YYYY-MM-DD` when the meeting states or clearly implies one (each call gets the
meeting's day, the recording's UTC date, so "Friday" resolves against it), any
other deadline exactly as said ("the next eight weeks"), or null. A due that
looks like a date but is no real day ("2026-02-30") is refused and repaired;
the eval counts any served as `invalid due dates`, gated at 0.

**The meeting's title.** Each saved section copies `minutes.title` into the
meeting's own title unless the user named the meeting
(`meetings.title_set_by_user`): a rename, a promote whose body says
`titleByUser` (the app knows whether its sheet's title was the user's), or an
edit of the minutes' title.

A minutes call is hedged only when stuck: unattended, one answering in its usual
time is never doubled, but one not answered in 40 s (the slowest section on a
30 min meeting took 24 s) gets the fallback beside it and the first answer wins,
so a stuck call no longer holds the run for its 180 s deadline (one did, and a
run took 297 s from Stop, past MOM-04's 3 min). The fallback still answers when
the model fails, and the stored `minutes_model` is the engine that answered
last.
Model calls of the meetings pass no reasoning effort. A repair call that fails
leaves the first answer's checked items standing, as a repair that doesn't
parse does.

The minutes are checked by rules, never by a model (MEETINGS.md 7.2). A
derivation stands when its line exists, its time is inside the line, its speaker
is the line's, and its quote is whole words of the line once both are folded the
same way (`folding.skeleton`): NFKC, casefolded, punctuation a space, a
Devanagari nukta dropped and a chandrabindu read as an anusvara (spellings of
one word), every other script's combining mark dropped. Devanagari vowel signs
and the virama are kept, so काम (work) never passes for कम (less). Ask ranks
lines with the same fold. A quote with no words never stands. An item with too
few derivations left (one; two lines for a contradiction), or one the wire
can't take, is dropped; nothing is clipped. Anything dropped sends the model
one repair turn for that section with its own first answer and the problems; if
the repair doesn't parse, the first answer's checked items stand. The pins go
into the stored minutes as `pinned`, copied, never written by the model. A 2 h
meeting (about 2,500 lines) is about 69,000 tokens in each section's call.

A question (`ask`) is one model call (MOM-06, 5 s), joined by the fallback when
the model has not answered in 2.5 s and given up at 8 s (past the 5 s P95 on
purpose: at 5 s a slow call under load was an error, not a slow answer; the
search's re-rank
too). It needs the meeting's transcript, not its minutes: it answers while a run
writes them, and is a 409 `transcript_not_ready` before the transcript lands.
Its candidate lines come in the founder's order (26 Oct 2026). First by meaning:
every item of the minutes and the summary is a labelled section ("Open question:
...", "Agenda (partly covered): ...", "Action item: ... (owner ...)"), and one
embedding call for the question and the sections ranks them; the 8 nearest lead,
each with the lines it cites. Then by words: the lines holding the most of the
question's content words (any script, 3 characters or more, no stopword) as
whole words. Thirty in all; with neither, the meeting's first lines. The model
answers from them with the sections, nearest first, as notes, and a question
about the meeting as a whole (what is undecided, who owns what, the risks, a
summary) is answered from the minutes' items, cited by their lines. When no
citation of that answer stands, it is asked once more over the whole transcript
(a 2 h meeting fits), never a third time. A citation stands by the same quote
rule; when none stands, whatever the model said, the answer is the server's own
"Not in this meeting." with `supported: false`, so an uncited answer is never
shown. An embedding that fails leaves the candidates by words (the retry still
stands). A search embeds the query, scans
the device's own live sections for the nearest `3 x limit` (an exact scan in a
materialized CTE: an HNSW walk would take every device's nearest and filter
after, so a device could find nothing at scale; so no vector index is kept), and
the model re-ranks them with each meeting's title beside its section. A hit on
an action item carries its `owner`, read at query time from the stored minutes
by the section's index (null for a summary, decision or question, or an action
with no owner). Both routes read what they need and end their transaction before
any model or embedding call, and the embed task embeds between two short
transactions, so no database connection waits on a model. A failed embedding or
model is a 502 `provider`; whatever ends a question or a search before its
answer, the day's use is given back.

Transcript edits (line, speaker) bump `meetings.transcript_version`; the
minutes keep the version read before their model call, so `GET` marks them
`stale` (D29), an edit landing during the call included.

**Trash, restore and purge** (D28). Trashing a recording soft-deletes it and
every row under it, the meeting, its pins and its vectors included, at one time.
`POST /v1/recordings/{id}/restore` inside the window clears `deleted_at` on
exactly the rows the trash marked (a line or pin deleted on its own before stays
deleted); the vectors come back with the meeting, the share stays turned off,
and a job the trash failed stays failed. Past the window it is 404.
`purge_expired` (a daily cron, 03:00 UTC) takes only recordings that are
meetings, oldest first, in batches until none is left. Each goes in one
transaction under its recording's lock: still trashed past the window (a restore
that won the lock keeps it), its S3 audio, raw, clip and share objects
superseded, then every row hard-deleted. One that fails is logged (`task=purge
job=<recordingId> verdict=failed`) and left for the next pass, never blocking
the rest. A key with no object, such as an upload aborted before it completed,
is skipped. Superseded objects are deleted by the bucket's own rule a day later,
so where `just bucket-rules` has run, no object is left 31 days after the trash.
A recording that was never a meeting stays soft-deleted for good (Q101).

**Unattended success** (D39). Each minutes run that reaches `ready` logs
`task=generate_minutes job=<meetingId> verdict=ready:unattended`, or
`ready:clicked` when a regenerate started it (`meetings.minutes_clicked`); a run
ready with sections marked adds their count (`ready:unattended:incomplete:1`),
which counts as a miss: those sections needed the sweep or a click. Count
`ready:unattended` per device and UTC day (join the meeting ID to its
recording's device) against meetings promoted that day; `failed:<reason>` lines
are the misses. `meetings_lost` (a recording orphaned on the Mac) is counted by
the app, not here.

A complete whose answer was lost is asked again by the app. S3 keeps a completed
upload's ID for a while (25 Oct 2026): asked again with the same ETags it
answers OK, with other ETags `InvalidPart`, and once the ID is gone
`NoSuchUpload`. So when S3 refuses the parts, complete looks for the object: one
of the upload's size means the earlier complete went through, and it answers as
that one would; no object, or the wrong size, stays a 422 with the upload open.

## Locks

Every write under a recording first locks the recording's row
(`recordings.lock`, `SELECT ... FOR NO KEY UPDATE`), then reads what it needs
again and answers 404 if the recording was trashed meanwhile, so writers of one
recording take turns. `FOR NO KEY UPDATE` because no write changes a key: it
still makes writers wait for each other, but not the foreign-key checks of rows
that point at the recording. The paths that take it:

- share create, which then locks its device's row too (also `FOR NO KEY UPDATE`,
  after the recording's, so a create waiting for a busy recording holds nothing
  of the device's), keeping the 100-share count exact across the device's
  recordings; share `PATCH` and `DELETE`
- upload start and complete, audio and share; both call S3 under the lock (start
  creates the multipart upload, complete completes it, reads the object's size
  and the poster's, and lists and tags old objects after its commit), so a 1 GiB
  complete holds that recording's writers for seconds, and only them
- generate (`POST .../transcription`)
- trashing a recording (`DELETE /v1/recordings/{id}`)
- edits: `PATCH /v1/lines/{id}`, `PATCH /v1/speakers/{id}`,
  `PUT .../speakers/{id}`, and each batch of Roman words a transliteration keeps
- caption translation: a page's first step (the track made, the lines picked,
  the day's lines reserved, each picked line marked in flight in the track) and
  its last (the answers written, which clears the marks), never while the model
  thinks; a line changed between them keeps its change. A page that finds a line
  in flight for the same `sourceHash` neither asks for it nor counts it: after
  its own batches it polls the track, with no lock, until the mark goes or runs
  out (65 s, a page's two waves of 30 s batches and 5 s more, so a crashed
  request's mark is ignored after that), and a line still not translated then is
  in its `failed` list. A page where no batch answered takes its marks back.
  Marks and the placeholder lines they make are never sent. A translated line's
  `PATCH` (which drops a mark) and a track's `DELETE`
- the worker: `merge_and_store`, and every job it fails (with the refund); a
  fail that lost to another writer commits at once, so the lock goes

`PUT /v1/recordings/{id}` is an upsert of the recording's own row in one
statement: it takes that row's lock for the statement and nothing else, so it
needs no other. Besides these, transactions take only the key-share locks
Postgres takes for foreign keys (an insert of a recording, an upload or the
day's usage reads its parent's row); neither lock blocks them. A write of one
row that commits at once (the worker moving a job's stage, sealing a voiceprint,
recording a voiceprint job, the device's `last_seen_at`) holds one lock and
can't wait while holding it, so it takes none.

Each race that bit has a test: a share video's start or complete, an audio
complete, a generate (both orders, two jobs) and a merge, each racing the trash
of their recording; each edit (a line, a speaker's name, a new speaker, a
share's `PATCH` and `DELETE`, kept Roman words) racing it; two share creates on
two recordings at 99 shares; a share create beside the day's first generate; a
share create queued behind a complete, while the device's other requests go
through; turning a share on while its first video completes; a generate while
the worker fails the recording's job (`test_shares_s3.py`,
`test_recordings_api.py`, `test_trash_races.py`, `test_share_limits.py`,
`test_worker_tasks.py`).

## Worker

`just worker` runs `arq snapbak_backend.worker.WorkerSettings`. Tasks take IDs
only and return early when the row is past their step; arq job IDs dedupe them.

| Task                   | arq job ID                                                     | Does                                                                                                                                                                                                                                      |
| ---------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `start_transcription`  | `start:{job}`                                                  | presigns the m4a for 2 h, submits to both vendors, checks again in 120 s                                                                                                                                                                  |
| `on_vendor_webhook`    | `hook:{vendor}:{job}`                                          | asks the vendor, moves the stage, enqueues the merge when both are done                                                                                                                                                                   |
| `check_vendor_timeout` | `timeout:{job}:{n}`                                            | the fallback for a lost webhook, every 60 s, fails at max(15 min, 2 × length)                                                                                                                                                             |
| `merge_and_store`      | `merge:{job}`                                                  | merges, refuses a language outside the 18, over 2 h, or no words; stores                                                                                                                                                                  |
| `create_voiceprints`   | `voiceprints:{job}`                                            | with consent only: a clip per speaker to pyannoteAI; 3 tries, then gives up                                                                                                                                                               |
| `on_voiceprint`        | `vp:{speaker}:{vendor job}`, `vp-check:{speaker}` (60 s later) | once the vendor job is the one recorded for the speaker, seals the voiceprint (AES-256-GCM, AD = speaker ID) onto it                                                                                                                      |
| `transliterate`        | `translit:{recording}`                                         | Roman words for a `script: "roman"` transcript                                                                                                                                                                                            |
| `sweep_waiting_jobs`   | cron, every 5 min                                              | the backstop: fails jobs waiting 24 h for audio (`upload_expired`), starts again jobs queued with audio for 5 min and fails them (`internal`) after 24 h, fails jobs processing 5 min past their deadline in any stage (`vendor_timeout`) |

A vendor's 429, 5xx or a network failure retries the task; a 4xx fails the job
with `vendor_error`, and running out of tries fails it with `internal`. When one
vendor accepts the submit and the other fails, the accepted one is kept in
`vendor_refs`, so the retry doesn't submit (and pay for) it again. Every failed
job deletes the vendors' copies (`strategy.cleanup`). The timeout check never
retries itself: a vendor outage or a lost race waits for the next check, so a
job can't stay `processing` forever. A voiceprint webhook counts only when its
`jobId` is the one recorded for that speaker in `vendor_refs.voiceprints`; one
that arrives before the record is written waits for it, and a check a minute
after the request fetches the voiceprint if no webhook came. A retry of
`create_voiceprints` skips speakers already sent. arq ends a task on any error
but `Retry`, so `sweep_waiting_jobs` is the backstop that no job outlives. A job
whose recording was trashed before its start is failed (`internal`) and
refunded; a start that lost a race deletes the vendor jobs it made, and so does
the next try of a submit whose job failed (was trashed) while it waited.
Transliteration reads the lines, ends its transaction while the model thinks,
and writes each batch as it arrives, each line only if its `rev` hasn't moved
and it isn't deleted, so an edit made meanwhile wins and a failure later doesn't
pay for earlier batches again. The merge, the language routing and the vendors'
requests are in the LLD; `transcription/merge.py` is pure and takes 14 ms for a
2 h transcript (budget 1 s, `tests/unit/transcription/test_merge_bench.py`).

Voiceprints follow the founder's "Soft forever" (Q101): `services/retention.py`
keeps them on delete and on withdrawn consent. Withdrawn consent stops new ones
at once. HLD open question 2 would change one statement there.

## Share links

A recording's link `snapbak.site/v/<slug>` (SHR-01, D18). The app re-encodes the
export to 1080p H.264, then, with its device token:

1. `POST /v1/recordings/{id}/shares` with the title and byte count. A new share
   is `uploading` with a 16-character slug (96 random bits; a taken one is drawn
   again, 3 times). An existing share keeps its slug and expiry and takes the
   new title. The answer carries a presigned PUT for the poster of the next
   version, `image/jpeg`, for an hour.
2. PUTs the poster, then uploads the video with `kind: "share"`, `shareId` and
   its `durationMs`, `width` and `height` (kept on the upload row), to
   `shares/{shareId}/v{version + 1}/video.mp4` in the bucket, with `video/mp4`
   and `public, max-age=31536000, immutable`. Starting it aborts the share's
   upload still open, so the later re-export wins. Starts and completes take the
   recording's lock ([Locks](#locks)), so two starts never pick one key, and a
   complete reads its upload again under the lock and answers 409 `upload_state`
   if a newer start aborted it meanwhile. Complete holds that lock, and its
   database connection, through S3's multipart complete, which takes seconds for
   1 GiB; only writes to that recording wait.
3. Complete points the share at that version: the video, the poster if it was
   PUT (else the older one stays), that upload's length and size (so a newer
   create can't relabel it), and `live` if it was `uploading` (turned off stays
   off). An upload older than the share's version changes nothing. After the
   commit, every object of an older version but the poster still shown is tagged
   `superseded=true`. A retried complete tags again, so a tagging that failed
   the first time is finished; a newer version's poster, PUT before its video
   completes, is never tagged.

A share's `type` is worked out from its recording on every create (D20):
`meeting` when it has a live meeting, else `audio` or `video` by its
`recordingType`. An audio recording has nothing to upload: its share plays the
recording's own `audio/` upload (D12, D16), so it is `live` at once, as version
1, and its page shows the not-ready page until that upload completes. A video
recording, meeting or not, goes through the steps above.

Creates of one device take turns on its `devices` row, so the 100-share cap and
one share per recording hold under concurrent creates. `expiresIn` is counted
from the server's clock. `expired` and `gone` are never stored: `page_state`
works them out from `expires_at` and `deleted_at`. Deleting a share, or its
recording, soft-deletes it; its S3 objects stay (Q101).

### The page

No sign-in. Every route reads the share by slug (anything but 16 to 64 URL-safe
characters is 404 with no read) and answers by its state:

| Route                      | Live                                                          | Otherwise                                                                |
| -------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------ |
| `GET /v/{slug}`            | 200, the page with the video signed for an hour               | the error page: `uploading` and `gone` 404, `disabled` and `expired` 410 |
| `GET /v/{slug}/video.mp4`  | 302 to a signed GET, `no-store`                               | the same error page and status                                           |
| `GET /v/{slug}/poster.jpg` | 302 to a signed GET, `public, max-age=300`; 404 without       | the same                                                                 |
| `GET /v/{slug}/download`   | 302 to a signed GET with `attachment; filename="<title>.mp4"` (`.m4a` for audio) | the same                                            |
| `GET /v/{slug}/src`        | `{video, expiresAt}`, `no-store`, for the player's retry (`video` names the audio too) | the same                                         |
| `GET /og.jpg`              | the branded 1200 x 630 card, `public, max-age=86400`          |                                                                          |
| `GET /robots.txt`          | `User-agent: *`, `Disallow: /v/`                              |                                                                          |
| `GET /`                    | 302 to `SNAPBAK_HOME_URL`                                     |                                                                          |

The `/v/` routes answer `HEAD` as `GET`, for link preview bots. Any other path
on the share host gets the branded "isn't available" page, 404, instead of
FastAPI's JSON.

The page is `templates/share.html`: the title (clamped to 2 lines, in full on
hover), the UTC date the share was made (the same text in the unfurl and on the
page), the length, the video, a **Download** button and "Made with Snapbak · Get
Snapbak". The player takes the video's aspect ratio and is never taller than the
window leaves room for, so the video has no bars and **Download** stays above
the fold on a 1100 x 720 window; under 560 px the button is full width under the
title. The look is the app's Soft UI (`docs/design/SOFT-UI.md` in the app, 25
Oct 2026): an opaque ground, the player and cards as plates lit from the top
left, the audio player in a well, **Download** the one lit purple primary,
speaker chips in the transcript's colours and cite chips that seek the player;
light or dark by the viewer's system, from the app's tokens. Open Graph and
Twitter tags point at the stable `/v/{slug}/video.mp4`
and `poster.jpg`, which link previews may cache for days; without a poster
`og:image` is `/og.jpg` (`templates/og.jpg`, 27 KB), with its size and alt text.
Each error state has its own icon (clock, link off, hourglass, camera off), and
the not-ready page reloads every 30 s. Pages send `private, no-store`, `noindex`
(header and meta), `no-referrer`, `nosniff`, `Strict-Transport-Security` and a
CSP that allows only the bucket's origin for media and the one inline script by
its hash. On a video error (an expired signature), the script fetches `/src` at
most once a minute and resumes where the viewer was. No font, framework, cookie
or analytics: the HTML is 14 KB, and the page answers in about 7 ms on a laptop
(budget 30 ms, cap 200 ms).

The same template serves all three types. An `audio` page has an audio player in
place of the video, no `og:video` tags (`og:type` `website`) and **Download
audio**. A `meeting` page (D16, D19) keeps the player on top (audio for an audio
recording, the video for a promoted one, with **Download audio** or **Download
video**), then two columns: the minutes card, in the app's order and with its
headings (Summary, Minutes, Action items with owner and due, Decisions with why
and what was not chosen, Open questions, Risks and blockers with their kind and
owner, Follow-ups, Next meeting, Agenda coverage with each topic's status,
Contradictions, Pins; a section with nothing in it is left out, and a pin
without a note reads "Pinned moment") and the full transcript beside it, sticky,
one column
under 860 px. Both come from the same JSON the app reads. The MoM's markdown is
rendered by a renderer kept small on purpose (headings, `-` lists, `**bold**`,
paragraphs) that escapes every character before it makes any tag. Each time
(`#t=<seconds>`) seeks the player. A meeting's audio player draws a waveform
in its well: the server holds no audio peaks, so each of 96 bars is how much of
its slice someone was speaking (from the transcript's lines), drawn as an
inline SVG twice, muted and lit inside the played level. An audio share with no
transcript keeps a bare level. Dates and times read as the app writes them:
the day as "24 Oct 2026", times in the meeting as "00:21", a due date as "Due
Fri 31 Oct", and the subtitle names the speakers in the order they first spoke.
The 100 KB budget holds for the page around the transcript; the transcript grows
with the meeting, a 2 h one a few hundred KB.

The templates are Python's own `string.Template`, not Jinja2, so no dependency
was added: `api/share_page.py` HTML-escapes every value. `templates/tokens.css`
is a copy of the app's `web/tokens/generated/tokens.css`;
`tests/unit/test_share_template.py` fails, naming the copy to make, when they
differ.

## Settings

In `.env` (see `.env.example`, and [SETUP.md](../SETUP.md) for each value).
Without `DATABASE_URL` the server runs dictation only and has no cloud routes.

| Variable                                                            | Example                                                                                                   |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `DATABASE_URL`                                                      | `postgresql+asyncpg://snapbak:dev@127.0.0.1:15432/snapbak`                                                |
| `REDIS_URL`                                                         | `redis://127.0.0.1:16379/0`                                                                               |
| `S3_REGION`                                                         | `ap-south-1`                                                                                              |
| `S3_BUCKET`                                                         | `snapbak-media`, dev and production alike (SETUP.md)                                                      |
| `SNAPBAK_AWS_ACCESS_KEY_ID`, `SNAPBAK_AWS_SECRET_ACCESS_KEY`        | the `snapbak` IAM user's key, dev and production; never `~/.aws` or `AWS_*` (SETUP.md)                    |
| `SNAPBAK_PUBLIC_URL`                                                | `https://api.snapbak.site`, the base of webhook URLs                                                      |
| `SNAPBAK_SHARE_BASE_URL`                                            | `https://snapbak.site`, the base of share links                                                           |
| `SNAPBAK_HOME_URL`                                                  | the home page **Get Snapbak** and `/` open (open question); never the share host's root, refused at start |
| `SNAPBAK_TRANSCRIPTION`                                             | `fake:two-speakers-en` or `assemblyai+pyannote`                                                           |
| `ASSEMBLYAI_API_KEY`, `PYANNOTE_API_KEY`, `PYANNOTE_WEBHOOK_SECRET` | needed by `assemblyai+pyannote`                                                                           |
| `SNAPBAK_WEBHOOK_SECRET`                                            | signs the AssemblyAI webhook header per job                                                               |
| `VOICEPRINT_KEYS`                                                   | `{"1": "<base64 of 32 bytes>"}`; the highest ID seals                                                     |
| `SNAPBAK_WAITLIST_TOKEN`                                            | the bearer secret for `POST /v1/waitlist/claim`; empty refuses every claim                                |

The limits (2 h a job, 7,200 s a device a UTC day, 20,000 translated caption
lines a device a UTC day, 1 GiB a share video, 100 shares a device) are
constants in `config/settings.py`, not settings.

## Run it locally

[SETUP.md](../SETUP.md) has every step, from the AWS bucket to the first run.

```sh
just db-up         # Postgres 17, Redis 7; migrates
just bucket-rules  # once
just serve         # the API on :8787
just worker        # the arq worker
```

Compose publishes Postgres on 15432 and Redis on 16379, off the defaults, so a
Postgres or Redis already on the Mac doesn't collide. Files go to the one real
S3 bucket, `snapbak-media`, local dev included: there is no local S3 stand-in.
`scripts/bucket_rules.py` sets the lifecycle rules: clips expire after a day,
share objects tagged `superseded=true` expire after a day, and open multipart
uploads are aborted after two days.

The fake strategy (`fake:<fixture>`) replays `tests/fixtures/transcription/`: no
vendor is called, no webhook has to reach the laptop. The fixtures are
synthetic, written in the vendors' documented shapes; `just test-cloud --record`
replaces them with recorded answers.

## Tests

`just test` runs every level. Tests that need the compose stack or S3
(integration, functional, end to end) skip with the reason when it is down or no
AWS credentials are set; `just db-up` starts the stack, and `.env` names the
credentials (SETUP.md). Each run makes its own database, Redis database and
`snapbak-test-<random>` bucket on S3, never `snapbak-media`, so runs at the same
time don't touch each other, and tests wait on the worker's arq results, never
on a sleep.

- unit: merge and its 2 h bench, languages, sealing, webhook signatures, the
  vendor clients against their documented shapes (respx), voiceprint spans,
  settings, transliteration batches, translation batches, answer checks and the
  target languages; share slugs, `page_state`, the rendered share page (tags,
  escaping, CSP hash, weight, tokens identical to the app's). The transcript
  eval's metrics and its live tier's guard rails, with fakes.
- integration: migrations up, down, up and `alembic check`; the device,
  recording, upload (S3) and job routes; the unique job and the daily limit
  under concurrent creates; the long-poll waking on Redis; the contract
  fixtures; the share routes, caps and concurrent creates; share uploads,
  versions and superseded tags on S3; the bucket's rules; caption translation
  (`test_translations_api.py`). The transcript eval end to end on a tiny
  AMI-shaped corpus, with no download.
- functional: the API and an in-process worker on the fake strategy, to the
  exact `two-speakers-en` lines; each failure fixture; voiceprints with and
  without consent; Roman words; a delete while processing; webhooks; the timeout
  check and the sweep; edits; logs hold no text; the share page in every state,
  its headers and helper routes, its time; share logs hold no slug or title.
- e2e: the real `just serve` and `just worker` processes over HTTP: a
  transcript, and a share watched then turned off.

`just test-cloud` is the on-demand run on real vendors. It costs money: never in
`just test`, CI or hooks. Put `.m4a` files, and `.txt` reference transcripts, in
`bench/transcription/`, set the vendor keys and `S3_BUCKET`, and run it. It
polls the vendors, so no tunnel is needed. It prints each vendor's time, the
merged time, the WER, the speakers, the names found and the fillers found, which
settles RESEARCH.md's open facts (speaker identification without a list,
`disfluencies` on U-3.5 Pro, the error strings, the delete endpoint). `--runs 5`
prints p50 and p95 against the 60 s goal; `--record` writes the vendor JSON as
fixtures. Each file it uploads to the bucket is tagged `superseded=true`
afterwards, so the bucket's day-later rule deletes it. `scripts/vendor_run.py`
holds that run, and the transcript eval's live tier uses it too.

It first runs the tests marked `cloud` (`uv run pytest -m cloud`), which
`uv run pytest` leaves out: `tests/e2e/test_translate_real.py` translates a
150-line talk into Spanish and Hindi on the engine in `SNAPBAK_TRANSLATE` (else
`SNAPBAK_BIG`) and checks every line comes back, in order. It is charged to the
eval budget.

## Transcript evals

`just eval-transcript` scores our transcript code (the merge, lines, fillers,
and the pause and caption rules the app applies to them) on three 10-minute AMI
meeting excerpts (CC BY 4.0, `bench/transcription/ami.json`). It is free:
vendor-shaped answers are made from the true words with seeded errors, and
recorded real answers in `bench/transcription/recorded/` are scored too. A run
worse than `bench/transcription/baseline.json` on any metric fails;
`--save-baseline` moves the baseline forward. Its copies of the app's pause and
caption rules are checked against vectors the app's own code printed
(`tests/contract/transcription-rules.json`, byte-identical in the app repo,
which runs the same vectors against its own code). `just eval-transcript-live`
runs the same excerpts through the real vendors and needs both keys, a real
bucket and `--budget-usd`. The metrics, the baseline and how to run both are in
the app repo's 13-transcription-pipeline LLD, "Evals".

`just eval-minutes` scores the minutes and Ask on the AMI test meetings and
QMSum's Product questions (free): the validator's truth table on real lines, the
fake provider through `minutes.run`, and recorded model answers replayed through
the real `minutes.run`, `rerank` and `answer_from`, against a baseline and gates
(served fabrications must be 0). `just eval-minutes-live --budget-usd USD` asks
the polish model and records its answers. The data, metrics, gates and results
are in `bench.md`, "Minutes and Q&A evals".
