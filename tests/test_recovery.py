"""Leases, competing workers, and crash recovery."""

import asyncio
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

import psycopg
from tests.database import expire_open_leases, fetch_status, open_session_factory
from tests.workflow_support import compiled_worker_graph

from patchpilot.llm.fake import DISCOUNT_TIER_TRIAGE, FakeProvider
from patchpilot.repositories.jobs import JobRepository
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.worker import process_one
from patchpilot.workflow.graph import STAGE_TRIAGE

DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = "An unrecognized customer tier returns HTTP 500."

SLOW_WORKER = textwrap.dedent(
    """
    import asyncio
    import os
    import time

    class Slow:
        def generate_structured(self, prompt, schema, options=None):
            with open(os.environ["STARTED_FILE"], "w", encoding="utf-8") as handle:
                handle.write("started")
                handle.flush()
                os.fsync(handle.fileno())
            time.sleep(30)
            raise AssertionError("the model call should have been killed")

        def close(self):
            return None

    async def main() -> None:
        from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
        from patchpilot.config import Settings
        from patchpilot.db.session import create_engine, create_session_factory
        from patchpilot.worker import process_one
        from patchpilot.workflow.graph import compile_triage_graph

        database_url = os.environ["DATABASE_URL"]
        settings = Settings(database_url=database_url, llm_provider="fake", _env_file=None)
        await setup_checkpoints(database_url)
        engine = create_engine(settings)
        try:
            factory = create_session_factory(engine)
            async with open_checkpointer(database_url) as checkpointer:
                await process_one(
                    factory,
                    Slow(),
                    compile_triage_graph(checkpointer),
                    worker_id="killed-worker",
                    lease_seconds=30,
                    database_url=database_url,
                )
        finally:
            await engine.dispose()

    asyncio.run(main())
    """
)


class HoldProvider:
    """Block inside the model call so a second worker can try to claim."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def generate_structured(self, prompt: str, schema: type, options: object = None):
        with self._lock:
            self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return DISCOUNT_TIER_TRIAGE

    def close(self) -> None:
        return None


class SleepProvider(FakeProvider):
    def generate_structured(self, prompt: str, schema: type, options: object = None):
        time.sleep(1.2)
        return super().generate_structured(prompt, schema, options)


async def _create(factory, *, title: str = DEMO_TITLE, description: str = DEMO_DESCRIPTION):
    async with factory() as session:
        return await TaskRepository(session).create(title=title, description=description)


def _run_process_one(database_url: str, provider: object, worker_id: str) -> bool:
    async def inner() -> bool:
        engine, factory = open_session_factory(database_url)
        try:
            return await process_one(
                factory,
                provider,
                worker_id=worker_id,
                lease_seconds=30,
            )
        finally:
            await engine.dispose()

    return asyncio.run(inner())


async def test_a_live_lease_is_not_taken_by_another_worker(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    provider = FakeProvider()
    try:
        created = await _create(factory, title="held")
        async with factory() as session:
            claimed = await JobRepository(session).claim_next_queued(
                stage=STAGE_TRIAGE,
                worker_id="owner",
                lease_seconds=30,
            )
        assert claimed is not None
        assert await process_one(factory, provider, worker_id="other", lease_seconds=30) is False
    finally:
        await engine.dispose()

    assert provider.calls == []
    assert fetch_status(postgres_database_url, created.id) == "running"


async def test_renewal_refuses_an_expired_lease(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        created = await _create(factory, title="expiring")
        async with factory() as session:
            claimed = await JobRepository(session).claim_next_queued(
                stage=STAGE_TRIAGE,
                worker_id="owner",
                lease_seconds=30,
            )
        assert claimed is not None
        expire_open_leases(postgres_database_url)
        async with factory() as session:
            renewed = await JobRepository(session).renew_lease(claimed, lease_seconds=30)
    finally:
        await engine.dispose()

    assert renewed is False
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT lease_until < now()
            FROM task_runs
            WHERE task_id = %s
            """,
            (created.id,),
        ).fetchone()
    assert row is not None
    assert row[0] is True


async def test_two_workers_do_not_run_the_same_task(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        created = await _create(factory, title="contended")
    finally:
        await engine.dispose()

    provider = HoldProvider()
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def run(worker_id: str) -> bool:
        async def inner() -> bool:
            local_engine, local_factory = open_session_factory(postgres_database_url)
            try:
                barrier.wait(timeout=5)
                return await process_one(
                    local_factory,
                    provider,
                    worker_id=worker_id,
                    lease_seconds=30,
                )
            finally:
                await local_engine.dispose()

        return asyncio.run(inner())

    left = threading.Thread(target=lambda: results.append(run("worker-left")))
    right = threading.Thread(target=lambda: results.append(run("worker-right")))
    left.start()
    right.start()
    assert provider.started.wait(timeout=10)
    assert provider.calls == 1
    provider.release.set()
    left.join(timeout=10)
    right.join(timeout=10)

    assert not left.is_alive()
    assert not right.is_alive()
    assert sorted(results) == [False, True]
    assert provider.calls == 1
    assert fetch_status(postgres_database_url, created.id) == "completed"


async def test_two_workers_cannot_recover_one_expired_lease(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        created = await _create(factory, title="expired")
        async with factory() as session:
            claimed = await JobRepository(session).claim_next_queued(
                stage=STAGE_TRIAGE,
                worker_id="crashed",
                lease_seconds=30,
            )
    finally:
        await engine.dispose()

    assert claimed is not None
    expire_open_leases(postgres_database_url)
    provider = HoldProvider()
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def run(worker_id: str) -> bool:
        async def inner() -> bool:
            local_engine, local_factory = open_session_factory(postgres_database_url)
            try:
                barrier.wait(timeout=5)
                return await process_one(
                    local_factory,
                    provider,
                    worker_id=worker_id,
                    lease_seconds=30,
                    max_attempts=3,
                )
            finally:
                await local_engine.dispose()

        return asyncio.run(inner())

    left = threading.Thread(target=lambda: results.append(run("worker-left")))
    right = threading.Thread(target=lambda: results.append(run("worker-right")))
    left.start()
    right.start()
    assert provider.started.wait(timeout=10)
    assert provider.calls == 1
    provider.release.set()
    left.join(timeout=10)
    right.join(timeout=10)

    assert sorted(results) == [False, True]
    assert provider.calls == 1
    assert fetch_status(postgres_database_url, created.id) == "completed"
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        actions = connection.execute(
            """
            SELECT payload->>'action'
            FROM events
            WHERE task_id = %s AND event_type = 'recovery'
            """,
            (created.id,),
        ).fetchall()
    assert actions == [("resume",)]


async def test_recovery_exhaustion_fails_the_task(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    provider = FakeProvider()
    try:
        created = await _create(factory, title="give up")
        async with factory() as session:
            claimed = await JobRepository(session).claim_next_queued(
                stage=STAGE_TRIAGE,
                worker_id="crashed",
                lease_seconds=30,
            )
        assert claimed is not None
        expire_open_leases(postgres_database_url)
        assert (
            await process_one(
                factory,
                provider,
                worker_id="closer",
                lease_seconds=30,
                max_attempts=1,
            )
            is True
        )
        assert await process_one(factory, provider, worker_id="closer") is False
        async with factory() as session:
            stored = await TaskRepository(session).get(created.id)
    finally:
        await engine.dispose()

    assert provider.calls == []
    assert stored is not None
    assert stored.status.value == "failed"
    assert stored.result_json == {
        "error": "Recovery stopped after attempt 1.",
        "error_code": "recovery_exhausted",
    }
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        row = connection.execute(
            """
            SELECT error_code, payload->>'action'
            FROM task_runs
            JOIN events ON events.run_id = task_runs.id
            WHERE task_runs.task_id = %s AND events.event_type = 'recovery'
            """,
            (created.id,),
        ).fetchone()
    assert row == ("recovery_exhausted", "exhausted")


async def test_blocking_model_call_still_renews_the_lease(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        created = await _create(factory, title="slow triage")
        async with compiled_worker_graph(postgres_database_url) as graph:
            assert (
                await process_one(
                    factory,
                    SleepProvider(),
                    graph,
                    worker_id="owner",
                    lease_seconds=1,
                    database_url=postgres_database_url,
                )
                is True
            )
    finally:
        await engine.dispose()

    assert fetch_status(postgres_database_url, created.id) == "completed"
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        span = connection.execute(
            """
            SELECT EXTRACT(EPOCH FROM (lease_until - started_at))
            FROM task_runs
            WHERE task_id = %s
            """,
            (created.id,),
        ).fetchone()
    assert span is not None
    assert float(span[0]) > 1.25


def test_killed_worker_is_recovered_on_restart(
    postgres_database_url: str,
    clean_tasks: None,
):
    created_id = asyncio.run(_insert(postgres_database_url))
    with tempfile.TemporaryDirectory() as directory:
        started = Path(directory) / "started"
        env = os.environ.copy()
        env["DATABASE_URL"] = postgres_database_url
        env["STARTED_FILE"] = str(started)
        env["LLM_PROVIDER"] = "fake"
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-c", SLOW_WORKER],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        output: list[str] = []

        def _drain() -> None:
            assert proc.stdout is not None
            output.append(proc.stdout.read())

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if started.exists():
                    break
                if proc.poll() is not None:
                    reader.join(timeout=2)
                    raise AssertionError("".join(output))
                time.sleep(0.05)
            else:
                proc.kill()
                reader.join(timeout=2)
                raise AssertionError("".join(output) or "worker did not reach the model call")
            proc.kill()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            reader.join(timeout=2)

    assert fetch_status(postgres_database_url, created_id) == "running"
    expire_open_leases(postgres_database_url)
    provider = FakeProvider()
    worked = asyncio.run(_recover(postgres_database_url, provider))

    assert worked is True
    assert len(provider.calls) == 1
    assert fetch_status(postgres_database_url, created_id) == "completed"
    with psycopg.connect(postgres_database_url, autocommit=True) as connection:
        runs = connection.execute(
            """
            SELECT attempt, error_code
            FROM task_runs
            WHERE task_id = %s
            ORDER BY attempt
            """,
            (created_id,),
        ).fetchall()
        events = connection.execute(
            """
            SELECT event_type
            FROM events
            WHERE task_id = %s
            ORDER BY timestamp, id
            """,
            (created_id,),
        ).fetchall()
        summary = connection.execute(
            "SELECT result_summary FROM tasks WHERE id = %s",
            (created_id,),
        ).fetchone()
    assert runs == [(1, "lease_expired"), (2, None)]
    names = [row[0] for row in events]
    assert "stage_started" in names
    assert "recovery" in names
    assert "retry" in names
    assert "task_completed" in names
    assert summary is not None
    assert summary[0] == DISCOUNT_TIER_TRIAGE.summary


async def _insert(database_url: str):
    engine, factory = open_session_factory(database_url)
    try:
        created = await _create(factory)
        return created.id
    finally:
        await engine.dispose()


async def _recover(database_url: str, provider: FakeProvider) -> bool:
    engine, factory = open_session_factory(database_url)
    try:
        async with compiled_worker_graph(database_url) as graph:
            return await process_one(
                factory,
                provider,
                graph,
                worker_id="restarted-worker",
                lease_seconds=30,
                max_attempts=3,
                database_url=database_url,
            )
    finally:
        await engine.dispose()
