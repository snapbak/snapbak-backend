# Setup

How to run Snapbak Cloud on a Mac: dictation polish, cloud transcripts and share
links. Dictation alone needs only steps 1, 5 and 6. Transcripts and shares need
every step.

| Piece             | Runs where                                  | Started by            |
| ----------------- | ------------------------------------------- | --------------------- |
| API               | this Mac, `127.0.0.1:8787`                  | `just serve`          |
| Transcript worker | this Mac                                    | `just worker`         |
| Postgres 17       | Docker, `127.0.0.1:15432`                   | `just db-up`          |
| Redis 7           | Docker, `127.0.0.1:16379`                   | `just db-up`          |
| Files             | AWS S3 bucket `snapbak-media`, `ap-south-1` | created once (step 2) |
| Transcription     | fake fixtures, or AssemblyAI and pyannoteAI | `.env` (step 4)       |
| Polish models     | OpenAI (or Groq, Anthropic, Gemini)         | `.env` (step 5)       |

There is no local S3 stand-in. Local dev and the tests use real buckets.

## 1. Tools

```sh
mise install          # Python, uv and just, as pinned in .mise.toml
uv sync
cp .env.example .env  # every step below fills in part of it
```

You also need Docker Desktop (for Postgres and Redis) and the AWS CLI
(`brew install awscli`).

## 2. AWS: the bucket and an IAM user

Use the AWS account Snapbak should bill. S3 is reached through one dedicated,
S3-only IAM user, `snapbak`, on dev and production alike. The server and the
tests sign with its key from `SNAPBAK_AWS_ACCESS_KEY_ID` and
`SNAPBAK_AWS_SECRET_ACCESS_KEY` only. They never read `~/.aws`, the `default`
profile or the shell's `AWS_*` variables, which may belong to another account.

### The bucket

One bucket, `snapbak-media`, in `ap-south-1`, for dev and production alike.
Files live in folders by kind, each with its own subfolders:

| Folder        | Holds                                               | Keys                                          | Kept                       |
| ------------- | --------------------------------------------------- | --------------------------------------------- | -------------------------- |
| `audio/`      | a recording's uploaded audio                        | `audio/<recording>/<upload>.m4a`              | forever                    |
| `clips/`      | a speaker's voiceprint clip                         | `clips/<job>/<speaker>.wav`                   | 1 day                      |
| `raw/`        | pyannoteAI's diarization output                     | `raw/<recording>/<job>/pyannote-diarize.json` | forever                    |
| `shares/`     | a share link's video and poster                     | `shares/<share>/v<n>/video.mp4`               | until replaced, then 1 day |
| `test-cloud/` | `just test-cloud`'s audio, sent to the real vendors | `test-cloud/<job>.m4a`                        | 1 day                      |

Every key holds a random ID, so dev and production never write the same file.
They do see each other's files, though: a dev server can read and delete
production files. The tests never touch `snapbak-media`; each run makes its own
`snapbak-test-<random>` bucket and deletes it afterwards.

### Create the IAM user

In the AWS console: **IAM › Users › Create user**, named `snapbak`, with no
console access. Attach this inline policy, named `snapbak-s3`. It can't delete
the `snapbak-media` bucket itself, only the tests' buckets:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "Bucket",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:GetLifecycleConfiguration",
        "s3:PutLifecycleConfiguration"
      ],
      "Resource": ["arn:aws:s3:::snapbak-media", "arn:aws:s3:::snapbak-test-*"]
    },
    {
      "Sid": "TestBuckets",
      "Effect": "Allow",
      "Action": ["s3:CreateBucket", "s3:DeleteBucket"],
      "Resource": "arn:aws:s3:::snapbak-test-*"
    },
    {
      "Sid": "Objects",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
        "s3:GetObjectTagging",
        "s3:PutObjectTagging"
      ],
      "Resource": [
        "arn:aws:s3:::snapbak-media/*",
        "arn:aws:s3:::snapbak-test-*/*"
      ]
    }
  ]
}
```

Then **Security credentials › Create access key › Application running outside
AWS**. Keep the key and secret for `.env` below. For the manual `aws` commands
in this file, also save them as a CLI profile; only you use it, never the
server:

```sh
aws configure --profile snapbak   # paste the key and secret; region ap-south-1
aws sts get-caller-identity --profile snapbak   # shows user/snapbak
```

### Create the bucket

Once, from an admin login (the IAM user above can't create `snapbak-media`):

```sh
aws s3api create-bucket --region ap-south-1 --bucket snapbak-media \
  --create-bucket-configuration LocationConstraint=ap-south-1
```

Leave **Block all public access** on, which is the default. The app and browsers
only reach files through signed URLs. CORS isn't needed: the app uploads from
native code, and the share page plays the video in a plain `<video>` tag.

Fill in `.env`:

```sh
S3_REGION=ap-south-1
S3_BUCKET=snapbak-media
SNAPBAK_AWS_ACCESS_KEY_ID=AKIA...
SNAPBAK_AWS_SECRET_ACCESS_KEY=...
```

The same key goes in production's environment. Without it the server refuses to
start and names the missing setting, and the cloud tests fail under `just test`.

Then set the lifecycle rules, once:

```sh
just bucket-rules
```

The rules: files in `clips/` expire after a day, share files a re-export
replaced (tagged `superseded=true`) expire after a day, and uploads nobody
finished are aborted after two days. `audio/` is kept.

## 3. Postgres and Redis

Start Docker Desktop, then:

```sh
just db-up
```

This starts both containers and migrates the schema. Their data stays in Docker
volumes across `just db-down`. The defaults in `.env.example` already point at
them:

```sh
DATABASE_URL=postgresql+asyncpg://snapbak:dev@127.0.0.1:15432/snapbak
REDIS_URL=redis://127.0.0.1:16379/0
```

Leave `DATABASE_URL` empty to run dictation only. The server then has no cloud
routes, and the app's **Generate transcript** fails with "Something went wrong
on Snapbak Cloud".

Postgres runs the `pgvector/pgvector` image, which ships the `vector`
extension the meetings' embedding column needs (MEETINGS.md). The migration
creates the extension itself; on a managed database the migration role needs
`CREATE EXTENSION` rights (RDS 17 supports pgvector).

## 4. Meeting minutes, embeddings, search and Q&A

```sh
SNAPBAK_EMBEDDINGS=openai:text-embedding-3-small
```

Minutes come from the polish model (`SNAPBAK_POLISH`). Meetings, Q&A and
search draw on the same daily caps as everything else: 50 questions and 50
searches per device a UTC day (`docs/cloud.md`). `fake:hashed` replaces the
embeddings with deterministic vectors for tests.

## 5. Transcription

### Fake (default, free)

```sh
SNAPBAK_TRANSCRIPTION=fake:two-speakers-en
```

It replays `tests/fixtures/transcription/two-speakers-en` for every recording.
No vendor is called, and no webhook has to reach your Mac.

### Real vendors (costs money)

```sh
SNAPBAK_TRANSCRIPTION=assemblyai+pyannote
ASSEMBLYAI_API_KEY=...        # assemblyai.com › API keys
PYANNOTE_API_KEY=...          # dashboard.pyannote.ai › API keys
PYANNOTE_WEBHOOK_SECRET=...   # dashboard.pyannote.ai, shown with the webhook
SNAPBAK_PUBLIC_URL=https://<your tunnel>
```

The vendors call webhooks on `SNAPBAK_PUBLIC_URL`, so it has to reach your Mac
from the internet. Use a tunnel to port 8787, for example
`cloudflared tunnel --url http://127.0.0.1:8787`. They fetch the audio from S3
through signed URLs, which works now that the buckets are real.

## 6. Secrets and URLs

These are required once `DATABASE_URL` is set. The server refuses to start and
names the missing one.

| Variable                 | Value                                                       |
| ------------------------ | ----------------------------------------------------------- |
| `SNAPBAK_TOKEN`          | `dev`, the token debug builds of the app send               |
| `SNAPBAK_WEBHOOK_SECRET` | `openssl rand -hex 32`                                      |
| `VOICEPRINT_KEYS`        | `printf '{"1": "%s"}\n' "$(openssl rand -base64 32)"`       |
| `SNAPBAK_PUBLIC_URL`     | `http://127.0.0.1:8787`, or the tunnel from step 4          |
| `SNAPBAK_SHARE_BASE_URL` | `http://localhost:8787`                                     |
| `SNAPBAK_HOME_URL`       | `https://snapbak.app`; never the same as the share base URL |

Polish needs a model and its key: `SNAPBAK_POLISH`, `SNAPBAK_BIG` and
`OPENAI_API_KEY` (or the key for the provider you name). Without keys, use
`fake:edit` for both models.

## 7. Run and check

In two terminals:

```sh
just serve    # the API on 127.0.0.1:8787
just worker   # the transcript worker; without it jobs stay "Waiting in line"
```

Check that the cloud routes are there:

```sh
curl -s localhost:8787/openapi.json | grep -o '"/v1/[a-z]*' | sort -u
```

The list must include `/v1/devices`, `/v1/recordings` and `/v1/jobs`. If it only
shows `/v1/polish` and `/v1/command`, `DATABASE_URL` didn't reach the server.

The app's debug build (`just dev` in `../snapbak`) talks to
`http://127.0.0.1:8787` with no setup. A release build needs
`SNAPBAK_BACKEND_URL`.

## 8. Tests

```sh
just test
```

`just test` loads `.env`, so the tests use the same AWS credentials. Each run
makes its own `snapbak-test-<random>` bucket in `ap-south-1` and deletes it at
the end; the shared `snapbak-media` bucket is never touched. When Docker is
down or `SNAPBAK_AWS_ACCESS_KEY_ID` isn't set, the cloud tests fail under
`just test` (it sets `SNAPBAK_REQUIRE_CLOUD`, so skipped coverage never reads as
green) and skip, saying why, under a plain `uv run pytest`.

A killed run can leave its buckets behind. To list and remove them:

```sh
aws s3 ls --profile snapbak | grep snapbak-test-
aws s3 rb --profile snapbak --force s3://snapbak-test-<random>
```
