"""The long-poll wakes on a Redis publish, and answers at `wait` without one. Each poll waits 20 s and the test
fails past 10 s, so a woken poll (milliseconds) and an unwoken one (20 s) never blur under load; the 1 s wake-up
budget is measured, not asserted (HLD)."""

import threading
import time
from typing import Any, cast

import redis
from conftest import RECORDING, REDIS, Sql, create_job, eventually, failure, put_recording, register

WAIT = 20
WOKEN = 10


def waiting_for(api: Any, job: str) -> None:
    """Returns once a long-poll for `job` registered its wake-up (the listener subscribes by pattern, so
    PUBSUB NUMSUB can't see a poll; the poll's own registration can)."""
    assert eventually(lambda: job in api.app.state.events.waiting, bool, within_s=WOKEN)


def test_trx01_a_publish_wakes_a_waiting_long_poll(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    job = create_job(api, auth).json()["jobId"]

    def change() -> None:
        waiting_for(api, job)
        sql("UPDATE transcription_jobs SET state = 'processing', stage = 'transcribing', version = 2")
        redis.Redis.from_url(REDIS).publish(f"job:{job}", 2)

    threading.Thread(target=change).start()
    started = time.monotonic()
    answer = api.get(f"/v1/jobs/{job}?wait={WAIT}&since=1", headers=auth).json()
    elapsed = time.monotonic() - started
    assert (answer["state"], answer["stage"], answer["version"]) == ("processing", "transcribing", 2)
    assert elapsed < WOKEN


def test_trx01_without_a_change_the_long_poll_answers_at_wait(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    job = create_job(api, auth).json()["jobId"]
    started = time.monotonic()
    answer = api.get(f"/v1/jobs/{job}?wait=1&since=1", headers=auth).json()
    elapsed = time.monotonic() - started
    assert answer["version"] == 1
    assert 1.0 <= elapsed < WOKEN


def test_trx01_a_job_already_past_since_answers_at_once(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    job = create_job(api, auth).json()["jobId"]
    started = time.monotonic()
    api.get(f"/v1/jobs/{job}?wait={WAIT}&since=0", headers=auth)
    assert time.monotonic() - started < WOKEN


def test_trx01_waiting_long_polls_hold_no_database_connection_or_transaction(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    job = create_job(api, auth).json()["jobId"]
    polls = [
        threading.Thread(target=api.get, args=(f"/v1/jobs/{job}?wait=3&since=1",), kwargs={"headers": auth})
        for _ in range(5)
    ]
    for poll in polls:
        poll.start()
    waiting_for(api, job)
    # The watcher is registered before the poll's first read, so that read may still be running: wait for it to end.
    # A poll that held its connection through the wait would keep it past the polls' whole 3 s.
    held = eventually(
        lambda: sql(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
            " AND state <> 'idle'"
        ),
        lambda rows: rows == [{"n": 0}],
        within_s=1.5,
    )
    started = time.monotonic()
    other = put_recording(api, auth)
    put_ms = (time.monotonic() - started) * 1000
    for poll in polls:
        poll.join()
    assert held == [{"n": 0}]
    assert (other.status_code, put_ms < 500) == (200, True)


def test_trx01_the_listener_reconnects_after_redis_drops_it(api: Any, sql: Sql) -> None:
    auth = register(api)
    put_recording(api, auth)
    job = create_job(api, auth).json()["jobId"]
    client = redis.Redis.from_url(REDIS)
    # Only this run's listener: other runs and `just serve` use other databases.
    db = client.connection_pool.connection_kwargs["db"]
    subscribers = cast(list[dict[str, str]], client.client_list(_type="pubsub"))
    ours = [entry["id"] for entry in subscribers if int(entry["db"]) == db]
    killed = [client.execute_command("CLIENT", "KILL", "ID", ident) for ident in ours]
    assert sum(killed) >= 1

    def change() -> None:
        waiting_for(api, job)
        sql("UPDATE transcription_jobs SET state = 'processing', stage = 'transcribing', version = 2")
        # The listener subscribes again after its backoff; publish until a subscriber takes it.
        assert eventually(lambda: client.publish(f"job:{job}", 2), bool, within_s=WOKEN)

    threading.Thread(target=change).start()
    started = time.monotonic()
    answer = api.get(f"/v1/jobs/{job}?wait={WAIT}&since=1", headers=auth).json()
    elapsed = time.monotonic() - started
    assert answer["version"] == 2
    assert elapsed < WOKEN


def test_cld04_trashing_the_recording_wakes_a_waiting_long_poll_at_once(api: Any) -> None:
    auth = register(api)
    put_recording(api, auth)
    job = create_job(api, auth).json()["jobId"]
    answers: list[Any] = []
    poll = threading.Thread(target=lambda: answers.append(api.get(f"/v1/jobs/{job}?wait={WAIT}&since=1", headers=auth)))
    poll.start()
    waiting_for(api, job)
    started = time.monotonic()
    assert api.delete(f"/v1/recordings/{RECORDING}", headers=auth).status_code == 204
    poll.join()
    # The trashed job is gone to the app: the poll answers 404 instead of waiting out its 20 s.
    assert failure(answers[0]) == (404, "not_found")
    assert time.monotonic() - started < WOKEN
