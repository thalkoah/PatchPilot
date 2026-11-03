"""Worker loop, atomic claims, and terminal triage writes."""

import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime

import pytest
from tests.database import fetch_status, open_session_factory, stamp_task

from patchpilot.config import Settings, get_settings
from patchpilot.db.models import TaskStatus
from patchpilot.llm.fake import FakeProvider
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.worker import main, process_one, serve
from patchpilot.workflow.graph import STAGE_TRIAGE


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
        llm_provider="fake",
        worker_idle_sleep_seconds=2,
        _env_file=None,
    )


async def _create(factory, *, title: str, description: str = "A reproducible failure."):
    async with factory() as session:
        return await TaskRepository(session).create(title=title, description=description)


async def test_claim_commits_before_the_session_closes(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        created = await _create(factory, title="only one")

        async def claim():
            async with factory() as session:
                return await TaskRepository(session).claim_next_queued(stage=STAGE_TRIAGE)

        first = await claim()
        second = await claim()
    finally:
        await engine.dispose()

    assert first is not None
    assert first.id == created.id
    assert first.title == "only one"
    assert second is None
    assert fetch_status(postgres_database_url, created.id) == "running"


async def test_oldest_queued_task_is_claimed_first(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        older = await _create(factory, title="older")
        newer = await _create(factory, title="newer")
        stamp_task(
            postgres_database_url,
            older.id,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        stamp_task(
            postgres_database_url,
            newer.id,
            created_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        async def claim():
            async with factory() as session:
                return await TaskRepository(session).claim_next_queued(stage=STAGE_TRIAGE)

        first = await claim()
        second = await claim()
    finally:
        await engine.dispose()

    assert first is not None and second is not None
    assert first.id == older.id
    assert second.id == newer.id


async def test_overlapping_claims_do_not_share_a_task(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        left = await _create(factory, title="left")
        right = await _create(factory, title="right")

        async def claim():
            async with factory() as session:
                return await TaskRepository(session).claim_next_queued(stage=STAGE_TRIAGE)

        claimed = await asyncio.gather(claim(), claim())
    finally:
        await engine.dispose()

    assert {item.id for item in claimed} == {left.id, right.id}
    assert fetch_status(postgres_database_url, left.id) == "running"
    assert fetch_status(postgres_database_url, right.id) == "running"


async def test_claim_by_id_does_not_take_a_different_task(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        wanted = await _create(factory, title="wanted")
        other = await _create(factory, title="other")
        async with factory() as session:
            claimed = await TaskRepository(session).claim_queued(wanted.id, stage=STAGE_TRIAGE)
        async with factory() as session:
            again = await TaskRepository(session).claim_queued(wanted.id, stage=STAGE_TRIAGE)
    finally:
        await engine.dispose()

    assert claimed is not None
    assert claimed.id == wanted.id
    assert again is None
    assert fetch_status(postgres_database_url, wanted.id) == "running"
    assert fetch_status(postgres_database_url, other.id) == "queued"


async def test_save_refuses_a_task_that_is_not_running(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    try:
        created = await _create(factory, title="still queued")
        with pytest.raises(RuntimeError, match="not running"):
            async with factory() as session:
                await TaskRepository(session).save_triage_result(
                    created.id,
                    status=TaskStatus.COMPLETED,
                    stage="finalize",
                    result_summary="should not stick",
                    result_json={"injected": True},
                )
    finally:
        await engine.dispose()

    assert fetch_status(postgres_database_url, created.id) == "queued"


async def test_empty_queue_does_not_call_the_provider(
    postgres_database_url: str,
    clean_tasks: None,
):
    engine, factory = open_session_factory(postgres_database_url)
    provider = FakeProvider()
    try:
        assert await process_one(factory, provider) is False
    finally:
        await engine.dispose()

    assert provider.calls == []


async def test_idle_loop_sleeps_without_opening_an_engine(monkeypatch, caplog):
    sleeps: list[float] = []

    async def no_work(session_factory, provider, graph=None):
        return False

    async def stop(seconds):
        sleeps.append(seconds)
        raise KeyboardInterrupt

    def fail_engine(settings):
        raise AssertionError("injected factory must not open an engine")

    monkeypatch.setattr("patchpilot.worker.process_one", no_work)
    monkeypatch.setattr("patchpilot.worker.asyncio.sleep", stop)
    monkeypatch.setattr("patchpilot.worker.create_engine", fail_engine)

    with caplog.at_level(logging.INFO), pytest.raises(KeyboardInterrupt):
        await serve(_settings(), provider=FakeProvider(), session_factory=object())

    assert sleeps == [2.0]
    assert "worker started provider=fake concurrency=1" in caplog.text


def test_main_logs_a_clean_stop(monkeypatch, caplog):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://patchpilot:patchpilot_dev@localhost/patchpilot",
    )
    get_settings.cache_clear()

    def interrupt(coro):
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr("patchpilot.worker.asyncio.run", interrupt)

    with caplog.at_level(logging.INFO):
        main()

    assert "worker stopped" in caplog.text


def test_sigterm_stops_the_worker_process(postgres_database_url: str, clean_tasks: None):
    env = os.environ.copy()
    env["DATABASE_URL"] = postgres_database_url
    env["LLM_PROVIDER"] = "fake"
    env["WORKER_IDLE_SLEEP_SECONDS"] = "30"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-m", "patchpilot.worker"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output = ""
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            output += line
            if "worker started" in output:
                break
        else:
            raise AssertionError(output)
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=5)
        output += proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert code == 0
    assert "worker stopped" in output
