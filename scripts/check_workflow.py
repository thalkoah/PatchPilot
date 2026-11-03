#!/usr/bin/env python3
"""Submit one bug report and require a schema-valid triage result.

`--api-url` posts to a running API and waits for the worker process.
`--once` inserts a row and triages that task in this process. Use `--once`
only when no other worker is polling the same database. The default provider
is fake and stays offline.
"""

import argparse
import asyncio
import json
import sys
import time
from uuid import UUID

import httpx2
from pydantic import ValidationError

from patchpilot.checkpoints import open_checkpointer, setup_checkpoints
from patchpilot.config import Settings, get_settings
from patchpilot.db.models import Task, TaskStatus
from patchpilot.db.session import create_engine, create_session_factory
from patchpilot.llm.factory import create_provider
from patchpilot.llm.triage import TriageResult
from patchpilot.repositories.tasks import TaskRepository
from patchpilot.schemas.tasks import TaskResponse
from patchpilot.worker import process_task
from patchpilot.workflow.graph import compile_triage_graph, read_thread_state

DEMO_TITLE = "Unknown customer tier crashes discount endpoint"
DEMO_DESCRIPTION = (
    "When the demo-service discount endpoint receives a customer tier it does not "
    "recognize, it returns HTTP 500. It should respond with HTTP 422 and a clear "
    "validation error. Find the cause, propose a minimal fix and add a regression test."
)
TERMINAL = {TaskStatus.COMPLETED, TaskStatus.FAILED}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="triage one inserted task in-process")
    parser.add_argument("--api-url", help="post to a running API and wait for its worker")
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="seconds to wait for a terminal status (default: 90)",
    )
    args = parser.parse_args(argv)
    if args.once == (args.api_url is not None):
        parser.error("pass exactly one of --once or --api-url")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")
    if args.once:
        return asyncio.run(run_once(args.timeout))
    return run_api(args.api_url, args.timeout)


async def run_once(timeout: float) -> int:
    try:
        settings = get_settings()
    except ValidationError:
        print("error: invalid settings", file=sys.stderr)
        return 2

    provider = create_provider(settings)
    engine = create_engine(settings)
    try:
        await setup_checkpoints(settings.database_url)
        factory = create_session_factory(engine)
        async with open_checkpointer(settings.database_url) as checkpointer:
            graph = compile_triage_graph(checkpointer)
            async with factory() as session:
                created = await TaskRepository(session).create(
                    title=DEMO_TITLE,
                    description=DEMO_DESCRIPTION,
                )
            task_id = created.id
            claimed = await process_task(
                factory,
                provider,
                task_id,
                graph,
                lease_seconds=settings.worker_lease_seconds,
                database_url=settings.database_url,
            )
            if claimed:
                async with factory() as session:
                    stored = await TaskRepository(session).get(task_id)
            else:
                stored = await _wait_for_terminal(factory, task_id, timeout)
        async with open_checkpointer(settings.database_url) as checkpointer:
            reloaded = await read_thread_state(compile_triage_graph(checkpointer), task_id)
    except Exception as exc:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        provider.close()
        await engine.dispose()

    _print_provider(settings)
    if stored is None or stored.result_summary != reloaded.get("result_summary"):
        print("error: checkpoint did not reload", file=sys.stderr)
        return 1
    if str(reloaded.get("task_id")) != str(task_id):
        print("error: checkpoint thread did not match the task", file=sys.stderr)
        return 1
    print(f"checkpoint thread_id={task_id} reloaded=true", file=sys.stderr)
    return _emit_task(stored)


def run_api(api_url: str, timeout: float) -> int:
    base = api_url.rstrip("/")
    payload = {"title": DEMO_TITLE, "description": DEMO_DESCRIPTION}
    try:
        with httpx2.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
            created = client.post(f"{base}/api/v1/tasks", json=payload)
            if created.status_code != 201:
                print(f"error: create returned HTTP {created.status_code}", file=sys.stderr)
                return 1
            body = created.json()
            task_id = body["id"]
            deadline = time.monotonic() + timeout
            while body.get("status") not in {"completed", "failed"}:
                if time.monotonic() >= deadline:
                    print(f"error: task is still {body.get('status')}", file=sys.stderr)
                    return 1
                time.sleep(0.5)
                fetched = client.get(f"{base}/api/v1/tasks/{task_id}")
                if fetched.status_code != 200:
                    print(f"error: read returned HTTP {fetched.status_code}", file=sys.stderr)
                    return 1
                body = fetched.json()
    except httpx2.HTTPError:
        print("error: API request failed before a response", file=sys.stderr)
        return 1

    if body.get("status") == "completed":
        try:
            TriageResult.model_validate(body.get("result_json"))
        except ValidationError:
            print("error: stored triage result failed validation", file=sys.stderr)
            return 1
        print(json.dumps(body, indent=2))
        return 0
    print("error: triage failed", file=sys.stderr)
    print(json.dumps(body, indent=2))
    return 1


async def _wait_for_terminal(factory, task_id: UUID, timeout: float) -> Task | None:
    deadline = time.monotonic() + timeout
    while True:
        async with factory() as session:
            task = await TaskRepository(session).get(task_id)
        if task is None or task.status in TERMINAL or time.monotonic() >= deadline:
            return task
        await asyncio.sleep(0.25)


def _print_provider(settings: Settings) -> None:
    if settings.llm_provider == "fake":
        print("provider=fake", file=sys.stderr)
        return
    print(
        f"provider=ollama model={settings.llm_model} base_url={settings.ollama_base_url}",
        file=sys.stderr,
    )


def _emit_task(task: Task | None) -> int:
    if task is None:
        print("error: task was not stored", file=sys.stderr)
        return 1
    if task.status == TaskStatus.COMPLETED:
        try:
            TriageResult.model_validate(task.result_json)
        except ValidationError:
            print("error: stored triage result failed validation", file=sys.stderr)
            return 1
        print(TaskResponse.model_validate(task).model_dump_json(indent=2))
        return 0
    if task.status == TaskStatus.FAILED:
        print("error: triage failed", file=sys.stderr)
    else:
        print(f"error: task is still {task.status.value}", file=sys.stderr)
    print(TaskResponse.model_validate(task).model_dump_json(indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
