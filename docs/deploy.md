# Deploy: one Ubuntu EC2 box

Production runs on one Ubuntu box: the API and the worker as two systemd
services, Redis beside them. Postgres is RDS and files are the S3 bucket
(SETUP.md).

| Piece    | Runs as                           | Started by                    |
| -------- | --------------------------------- | ----------------------------- |
| API      | `snapbak-server`, `0.0.0.0:80`    | alembic, then uvicorn         |
| Worker   | `snapbak-worker`                  | arq                           |
| Redis 7  | apt's `redis-server`, `127.0.0.1` | systemd                       |
| Postgres | RDS 17 with pgvector              | AWS                           |
| Files    | S3 `snapbak-media`                | created once (SETUP.md)       |

## The box

- Ubuntu 24.04, x86 or ARM.
- The security group opens port 80 to the world: the app and the vendors'
  webhooks reach it there. Port 6379 stays closed; Redis listens on 127.0.0.1
  only.
- RDS lets the box in on 5432. The migration role needs `CREATE EXTENSION` for
  `vector` (SETUP.md, step 3).

## Set it up

As the `ubuntu` user (or any user with sudo; the services run as that user):

```sh
git clone <repo> ~/snapbak-backend && cd ~/snapbak-backend
deploy/setup-ubuntu.sh   # first run: copies .env.example to .env and stops
vi .env                  # fill it in: the values below and SETUP.md
deploy/setup-ubuntu.sh   # installs, migrates and starts both services
```

The script installs `redis-server`, `ffmpeg` (the worker cuts voiceprint
clips with it) and `nodejs` (for pyright) from apt, then mise. Then it installs
the pinned Python, uv and just, and runs `uv sync`. It writes
`deploy/snapbak-server.service` and `deploy/snapbak-worker.service` into
`/etc/systemd/system` with this user and this path, and restarts both. Run it
again after every pull: it is safe to repeat.

`.env` on the box differs from a Mac's in these:

```sh
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<rds-host>:5432/snapbak
REDIS_URL=redis://127.0.0.1:6379/0
SNAPBAK_PUBLIC_URL=http://<the box's public name>
```

The server unit passes `--host 0.0.0.0 --port 80` itself; `PORT` in `.env` is
for `just serve` only. The server binds port 80 as a plain user
through `CAP_NET_BIND_SERVICE`, never as root. Port 80 is plain HTTP: bearer
tokens and webhook URLs cross the network unencrypted. TLS needs a load
balancer or a proxy in front (then see `--proxy-headers` in docs/cloud.md).

## How the units run

Both units run the venv's Python by absolute path, with no just, mise or uv in
between: `python -m alembic upgrade head` and then `python -m uvicorn` for the
server, and `python -m arq snapbak_backend.worker.WorkerSettings` for the
worker. systemd reads `.env` itself (`EnvironmentFile=`), so a value with spaces
or quotes needs single quotes, as `.env.example` does. The setup refuses to
install a unit whose command isn't there. The worker starts after the server.
Both restart 2 s after any exit. On stop, arq cancels the jobs it holds and
requeues them, and the next start picks them up.

## Day to day

```sh
sudo systemctl status snapbak-server snapbak-worker
journalctl -u snapbak-server -f                       # the API's log
journalctl -u snapbak-worker -o cat | just unattended # D39's success rate
git pull && deploy/setup-ubuntu.sh                    # ship a change
```

Every `just` recipe runs on the box as on a Mac: the setup adds mise to
`~/.bashrc`, so a login shell has the pinned tools. There are two
exceptions. `just db-up` and `just db-down` need Docker, which the box doesn't
have; it uses RDS and apt's Redis instead. `just test` needs a compose stack,
so run it on a Mac or in CI. `just stop-to-minutes` encodes its meeting with
ffmpeg's AAC where afconvert is missing (docs/bench.md).
