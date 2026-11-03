# PatchPilot progress

## Task 01 — Bootstrap a runnable skeleton

Status: complete (2026-11-02).

The stack is PostgreSQL, a FastAPI process, and an idle worker. There is no task table, agent, or model client.

### Evidence

`uv run pytest` — 13 passed in 0.16s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 16 files already formatted.

Docker is not installed. Podman 5.8.9 with podman-compose 1.6.0 ran the same `compose.yaml`:

`uvx podman-compose up --build -d`

`uvx podman-compose ps` showed `patchpilot_db_1` healthy, `patchpilot_api_1` published as `127.0.0.1:8000->8000/tcp`, and `patchpilot_worker_1` up.

`curl http://127.0.0.1:8000/health/live` — HTTP 200, `{"status":"live"}`.

`curl http://127.0.0.1:8000/health/ready` — HTTP 200, `{"status":"ok","database":"ready"}`.

After `uvx podman-compose stop db`: live stayed HTTP 200, and ready returned HTTP 503, `{"status":"unavailable","database":"unavailable"}`.

After `uvx podman-compose start db`: ready returned HTTP 200 again, and the database container became healthy.

Worker log: `worker idle; no jobs configured`.

The services were left running.

### Changed files

- `pyproject.toml`, `uv.lock`, `.python-version`
- `src/patchpilot/` — settings, readiness probe, API, idle worker
- `tests/` — config, health, worker, and Compose contract
- `Dockerfile`, `.dockerignore`, `compose.yaml`, `.env.example`, `.gitignore`
- `README.md`, `docs/progress.md`

### Limitations

- `docker compose` itself was not executed. Podman Compose is the verified runner.
- The worker does not connect to PostgreSQL or claim jobs.
- `patchpilot_dev` is a local example password. `.env` is not committed.
- Readiness opens a new connection per request. There is no schema or migration yet.

### Next task

Task 02 — Durable task API.

## Task 02 — Durable task API

Status: complete (2026-11-02).

Bug-report tasks are stored in PostgreSQL. `POST /api/v1/tasks` inserts a `queued` row. `GET /api/v1/tasks/{id}` and `GET /api/v1/tasks` read it back, newest first, with `limit`, `offset`, and an optional `status` filter. The worker is still idle.

### Evidence

`uv run pytest` — 50 passed in 3.31s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 31 files already formatted.

Task tests started a disposable PostgreSQL 16 container with Podman, applied `alembic upgrade head`, and left the Compose volume alone.

`uvx podman-compose up --build -d` rebuilt the image. The API log shows Uvicorn started, which means `alembic upgrade head` exited successfully first. `podman exec patchpilot_db_1 psql -U patchpilot -d patchpilot -c '\dt'` listed `alembic_version` and `tasks`. `alembic_version` is `0001_create_tasks`.

`curl -X POST http://127.0.0.1:8000/api/v1/tasks` — HTTP 201, status `queued`, `repo_id`, `stage`, `result_summary`, and `result_json` null. Id `78bb0ae9-b814-4d8b-ad61-37030e4624ed`.

`uvx podman-compose restart api worker` — ready returned HTTP 200. `GET /api/v1/tasks/78bb0ae9-b814-4d8b-ad61-37030e4624ed` returned the same JSON body. The filtered list returned that one row. The worker logged `worker idle; no jobs configured` again.

The services were left running. That smoke-test row is in the Compose database.

### Changed files

- `src/patchpilot/db/` — async engine and the `tasks` model
- `src/patchpilot/schemas/tasks.py`, `repositories/tasks.py`, `routes/tasks.py`, `dependencies.py`
- `src/patchpilot/api.py` — session lifespan and `/api/v1` router
- `alembic.ini`, `alembic/` — revision `0001_create_tasks`
- `tests/database.py` and task API, repository, schema, and boundary tests
- `pyproject.toml`, `uv.lock` — SQLAlchemy asyncio, Alembic, pytest-asyncio
- `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` itself was not executed. Podman Compose is the verified runner.
- The worker does not claim or update tasks. `stage` and `result_json` stay null until a later task writes them.
- `repo_id` is a nullable column with no repository table yet. Create requests that send it receive HTTP 422.
- The master-plan `idempotency_key` column is not in this revision.
- `updated_at` changes when application code updates the row. There is no database trigger.

### Next task

Task 03 — LLM abstraction with a deterministic test model.

## Task 03 — LLM abstraction with a deterministic test model

Status: complete (2026-11-02).

`generate_structured(prompt, schema, options)` is the model call. `FakeProvider` returns deterministic `TriageResult` fixtures and does not open a socket. `OllamaProvider` posts to Ollama `/api/chat` with the JSON schema in `format`, validates the reply, and retries invalid JSON or a timeout at most `LLM_MAX_RETRIES` extra times (default 2, maximum 3). The failure types are `LLMResponseError`, `LLMTimeoutError`, and `LLMTransportError`. Their messages omit model text. The API and the idle worker do not call a provider. `LLM_PROVIDER` defaults to `fake`.

### Evidence

`uv run pytest` — 90 passed in 3.32s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 43 files already formatted.

`uv run python scripts/check_llm.py` — exit 0. Stderr: `provider=fake`. Stdout was a schema-valid high-severity bug triage for the discount-endpoint prompt. The script did not need a database.

`LLM_PROVIDER=ollama LLM_MODEL=gemma4:12b OLLAMA_BASE_URL=http://127.0.0.1:11434 uv run python scripts/check_llm.py` — exit 0 in about 117s. Ollama 0.34.1, model `gemma4:12b`, base URL `http://127.0.0.1:11434`. The parsed object classified the report as a high-severity bug with `needs_clarification` false. An earlier run with the 60 second default timed out on all three attempts, so the default timeout is 180 seconds per attempt.

### Changed files

- `src/patchpilot/llm/` — limits, triage schema, fake provider, Ollama provider, factory
- `src/patchpilot/config.py` — provider, model, base URL, timeout, and retry settings
- `scripts/check_llm.py`
- `tests/test_llm.py`, `tests/test_config.py`, `tests/test_compose_contract.py`, `tests/test_task_boundaries.py`
- `pyproject.toml`, `uv.lock` — `httpx2` is a runtime dependency
- `compose.yaml` — worker `host-gateway` and LLM environment, provider still `fake`
- `.env.example`, `README.md`, `docs/progress.md`
- `src/patchpilot/worker.py` — docstring only; the process stays idle

### Limitations

- `docker compose` itself was not executed. The running Compose containers were not recreated, so they do not yet have the new worker environment.
- The worker does not claim tasks or call the provider. The call is synchronous for that later worker loop.
- Reaching Ollama from the worker container was not smoke-tested. Host Ollama is still bound to loopback, which a container cannot use until `OLLAMA_HOST` changes. Port 11434 is not published.
- A 60 second timeout is too short for `gemma4:12b` on this host. The default is 180 seconds per attempt, and a timed-out call can be retried twice.
- The `ollama` Python package is not a dependency. `httpx2` calls the chat API.
- Token counts, cost, and per-stage model routing are later tasks.

### Next task

Task 04 — First operational LangGraph workflow.

## Task 04 — First operational LangGraph workflow

Status: complete (2026-11-02).

The worker claims the oldest `queued` task, commits, and only then runs a LangGraph graph: `START -> triage -> finalize -> END`. The graph has no checkpointer. One `generate_structured` call produces a `TriageResult`. Success stores status `completed`, stage `finalize`, the summary, and the validated object in `result_json`. Failure stores status `failed`, stage `triage`, and `{"error": "..."}` with no model text. The API still only enqueues and reads. Concurrency is 1. `WORKER_IDLE_SLEEP_SECONDS` now defaults to 2. The worker handles `SIGTERM`, which matters because it is PID 1 in the container.

### Evidence

`uv run pytest` — 110 passed in 4.68s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 47 files already formatted.

`uvx podman-compose up --build -d` rebuilt the image. The worker log: `worker started provider=fake concurrency=1`. It then completed the Task 02 smoke row `78bb0ae9-b814-4d8b-ad61-37030e4624ed`.

`uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000` — exit 0. HTTP 201 was followed by a completed read. Id `4e72a9e9-df33-4e9f-9bdc-379d76361063`, stage `finalize`, summary `An unrecognized customer tier makes the discount endpoint return HTTP 500.` The triage object is the deterministic high-severity bug fixture.

`uvx podman-compose restart api worker`, then `GET /api/v1/tasks/4e72a9e9-df33-4e9f-9bdc-379d76361063` — HTTP 200 with the same JSON body, including the same `updated_at`.

`LLM_PROVIDER=ollama LLM_MODEL=gemma4:12b OLLAMA_BASE_URL=http://127.0.0.1:11434 uv run python scripts/check_workflow.py --once` — exit 0 in 64.86s against a disposable PostgreSQL 16 container, which was removed afterward. Ollama 0.34.1, model `gemma4:12b`. The stored result classified the report as a high-severity bug, `needs_clarification` false, summary `Discount endpoint returns 500 instead of 422 for unknown customer tiers`.

After the image containing the signal handler was running, `uvx podman-compose restart worker` stopped it without the previous SIGKILL warning. The log shows `worker stopped` and then `worker started provider=fake concurrency=1`.

The services were left running.

### Changed files

- `src/patchpilot/workflow/` — typed triage graph, no checkpointer
- `src/patchpilot/worker.py` — claim loop, one task at a time, SIGTERM
- `src/patchpilot/repositories/tasks.py` — `FOR UPDATE SKIP LOCKED` claim and terminal triage write
- `scripts/check_workflow.py`
- `src/patchpilot/config.py` — idle poll default is 2 seconds
- `tests/test_workflow.py`, `tests/test_worker.py`, boundary, config, and Compose contract tests
- `pyproject.toml`, `uv.lock` — `langgraph`
- `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` itself was not executed. Podman Compose is the verified runner.
- The graph is not checkpointed. A crash during the model call leaves the task `running` until a later recovery task.
- The worker container was not pointed at Ollama. Host Ollama is still bound to loopback. The Ollama smoke ran in a host process.
- Concurrency is fixed at 1. There is no lease, event log, or second agent.
- `updated_at` still changes only when application code updates the row.

### Next task

Task 05 — Real LangGraph PostgreSQL checkpointing.

## Task 05 — Real LangGraph PostgreSQL checkpointing

Status: complete (2026-11-03).

The worker compiles the triage graph with `AsyncPostgresSaver`. The LangGraph `thread_id` is the task UUID. Each finished node is stored with `durability="sync"` before the next node starts. The saver's tables are `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, and `checkpoint_writes`, created by `AsyncPostgresSaver.setup()`. Alembic's only revision is still `0001_create_tasks`. The business result stays on `tasks`.

The worker calls setup when it starts, then keeps one autocommit connection until it stops. The same setup is the command `uv run python -m patchpilot.checkpoints`.

### Evidence

`uv run pytest` — 119 passed in 6.34s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 50 files already formatted.

`uvx podman-compose up --build -d` built `localhost/patchpilot:local` with `langgraph-checkpoint-postgres==3.2.0`. That command did not replace the containers that had been up for 20 hours. `uvx podman-compose up -d --force-recreate api worker` did. The database volume stayed. The worker log: `worker started provider=fake concurrency=1 checkpointer=postgres`. `\dt` listed `tasks`, `alembic_version`, and the four checkpoint tables. `alembic_version` was still `0001_create_tasks`.

`uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000` — exit 0. Id `01ae80ba-bb4e-4f42-b503-761137a1c8d7`, status `completed`, stage `finalize`, summary `An unrecognized customer tier makes the discount endpoint return HTTP 500.` `checkpoints` had 4 rows for that thread id and no other thread.

`uvx podman-compose restart api worker` logged `worker stopped` and then `worker started provider=fake concurrency=1 checkpointer=postgres`. `GET /api/v1/tasks/01ae80ba-bb4e-4f42-b503-761137a1c8d7` returned the same JSON body. The checkpoint count stayed 4. A new Python process in the restarted worker loaded that thread and printed the same task id and summary.

`podman exec patchpilot_worker_1 python -m patchpilot.checkpoints` — exit 0. `checkpoint tables ready`.

The services were left running.

### Changed files

- `src/patchpilot/checkpoints.py` — async saver, setup command, named connection
- `src/patchpilot/workflow/graph.py` — thread id, sync durability, resume and reload helpers
- `src/patchpilot/worker.py` — setup, one checkpointer connection, close on stop
- `scripts/check_workflow.py` — `--once` reloads the thread through a second saver
- `tests/test_checkpoints.py`, `tests/workflow_support.py`
- `tests/test_workflow.py`, `tests/test_worker.py`, `tests/test_task_boundaries.py`, `tests/test_compose_contract.py`
- `pyproject.toml`, `uv.lock` — `langgraph-checkpoint-postgres`
- `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` itself was not executed. Podman Compose is the verified runner. `up --build -d` did not recreate containers that were already running.
- A crash during the model call still leaves the business row `running`. This worker does not claim that row again or resume the thread. The checkpoint is there for the next task.
- Tasks completed by the previous worker have no checkpoint rows.
- The saver is one connection, which matches concurrency 1. There is no pool.
- `compile_triage_graph()` without a saver is still the path for unit tests that do not need PostgreSQL. LangGraph 1.2.19 raises `AttributeError` if `durability` is passed with no checkpointer, so durability is set only when a saver is attached.
- Host Ollama was not part of this run. The checkpoint path was exercised with `FakeProvider`.

### Next task

Task 06 — Event audit trail, recoverable jobs and worker leases.

## Task 06 — Event audit trail, recoverable jobs and worker leases

Status: complete (2026-11-03).

A claim still uses `FOR UPDATE SKIP LOCKED`, and it now opens a `task_runs` row before the commit that releases the task lock. `lease_until` is the database clock plus `WORKER_LEASE_SECONDS` (default 30). A background thread renews that lease only while the same worker still holds an open run, including while a synchronous model call blocks the event loop. A second worker does not take a task whose lease is in the future.

When a lease expires, the next poll locks the task, closes the run with `lease_expired`, and starts the next attempt if the attempt count is below `WORKER_MAX_ATTEMPTS` (default 3). That attempt resumes the LangGraph thread when a checkpoint exists, and otherwise replays triage. At the last attempt the task is stored as `failed` with `recovery_exhausted`. Events are append-only. A repeated idempotency key does not change the stored payload. `GET /api/v1/tasks/{id}/events` returns the trail, oldest first. Alembic head is `0002_task_runs_and_events`. Checkpoint tables are still created by `AsyncPostgresSaver.setup()`.

### Evidence

`uv run pytest` — 134 passed in 11.16s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 58 files already formatted, after one format pass on the new event writer.

The suite's crash test starts a worker subprocess, waits until a fake model call is inside `time.sleep`, sends `SIGKILL`, expires the lease, and runs `process_one` again. The task ends `completed`. The first run is `lease_expired`, the second run has no error, and the event rows include `stage_started`, `recovery`, `retry`, and `task_completed`. The restarted model call happens once. Two competing workers produce one model call. An expired lease at `max_attempts=1` becomes `failed` and is not claimed again. A blocking one-second model call still moves `lease_until` forward. `UPDATE` and `DELETE` on `events` raise `events are append-only`.

The running Compose stack was not recreated. It is still the Task 05 image, so it does not have `task_runs` or `events` until the next `podman-compose up --build` recreates the API and worker. `alembic upgrade head` in that new API process applies `0002_task_runs_and_events`.

### Changed files

- `alembic/versions/20261103_0002_task_runs_and_events.py` — `task_runs`, append-only `events`
- `src/patchpilot/db/models.py` — `TaskRun`, `TaskEvent`
- `src/patchpilot/audit.py`, `repositories/events.py`, `repositories/jobs.py`, `leases.py`
- `src/patchpilot/worker.py` — claim, renewal, recovery, resume
- `src/patchpilot/workflow/graph.py` — resume helper, lease-lost abort
- `src/patchpilot/routes/tasks.py`, `schemas/events.py`, `dependencies.py` — event API
- `src/patchpilot/config.py` — lease seconds and max attempts
- `tests/test_events.py`, `tests/test_recovery.py`, plus worker, workflow, checkpoint, and contract updates
- `compose.yaml`, `.env.example`, `README.md`, `scripts/check_workflow.py`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers from Task 05 were left running and do not include this schema yet.
- A model error is a terminal `failed` task. Recovery retries expired leases, not a triage result the graph already stored as failed.
- If a worker is still inside a model call after its lease has expired, the recovering worker may start. The first worker discards its result when the lease check fails, but the two calls can overlap until that return. Healthy workers renew the lease and do not overlap.
- Concurrency stays 1. The renewal thread is one short-lived connection per active run.
- Event payloads omit the idempotency key. Rows written in one transaction are ordered by adding microseconds to `clock_timestamp()`.
- Host Ollama was not part of this run. Recovery was exercised with `FakeProvider`.

### Next task

Task 07 — Repository registry and safe read-only tools.

## Task 07 — Repository registry and safe read-only tools

Status: complete (2026-11-03).

A task may name an enabled row in `repositories`. It cannot send a filesystem path or a Git URL. Registration is the local command `python -m patchpilot.source`. There is no HTTP registration route. The stored path is relative to `REPOSITORY_ROOT`. `registration` keeps the checked-out commit id and does not keep a host path or a Git remote.

`list_tree`, `read_text_file`, and `search_text` read one checkout. They reject `..`, absolute paths, symlink escapes, `.git`, credential names, binaries, and files over 32768 bytes. Listings omit denied names. Search is a literal substring and does not follow symlinks. The triage worker does not call these tools. Alembic head is `0003_repositories`.

`examples/demo_service` is a small FastAPI checkout. Known tiers return a discount. An unknown tier still raises `KeyError`, so the endpoint returns HTTP 500. The regression test expects HTTP 422 and fails on purpose.

### Evidence

`uv run pytest` — 160 passed in 11.37s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 77 files already formatted.

`uv run pytest examples/demo_service/tests/test_baseline.py` — 2 passed in 0.11s.

`uv run pytest examples/demo_service/tests/test_unknown_tier.py` — 1 failed, as intended. `GET /discount?customer_tier=platinum` returned HTTP 500. The assertion expected HTTP 422. Exit code 1.

`uv run python scripts/init_demo_repo.py` — exit 0. It created commit `74b808a` with the message `Baseline demo service with the unknown-tier defect.` A second run printed `demo repository already initialized`. The generated `examples/demo_service/.git/` was then removed so the parent repository tracks the source files instead of a gitlink. Run the script again before registering the checkout.

The Compose containers `patchpilot_db_1`, `patchpilot_api_1`, and `patchpilot_worker_1` were already up and were left running. They do not have this migration or the read-only source mount until the next image rebuild recreates the API and worker.

### Changed files

- `alembic/versions/20261103_0003_repositories.py` — `repositories` and `fk_tasks_repo_id`
- `src/patchpilot/db/models.py`, `repositories/repos.py`, `repositories/tasks.py` — registry rows and task `repo_id`
- `src/patchpilot/schemas/tasks.py`, `routes/tasks.py` — optional registered `repo_id`
- `src/patchpilot/source/` — path checks, read-only tools, registration, CLI
- `src/patchpilot/config.py` — absolute `REPOSITORY_ROOT`
- `examples/demo_service/` — buggy discount service and its pytest suite
- `scripts/init_demo_repo.py`
- `tests/test_source_tools.py`, `tests/test_repo_registry.py`, plus API, config, checkpoint, and Compose contract updates
- `compose.yaml`, `.env.example`, `.gitignore`, `README.md`, `docs/progress.md`

### Limitations

- The running Compose stack was not recreated. `docker compose` itself was not executed. Podman is the runtime on this machine.
- The worker still only triages. A `repo_id` is stored and survives a claim, and no agent reads the checkout yet.
- Registration refuses a `.git` symlink or a checkout symlink that leaves `REPOSITORY_ROOT`. A hard link to a file outside the checkout is not detected. A file can also be replaced between the path check and the read.
- Files over the byte limit are refused. A file over the line limit returns the first 400 lines with `truncated` true.
- Search skips every symlink, including one whose target stays inside the checkout. `read_text_file` follows that internal symlink and reports the target path.
- After `scripts/init_demo_repo.py`, `git add examples/demo_service` records a gitlink. The parent `.gitignore` ignores the nested `.git` directory, not the source files.
- The Compose database port is still unpublished. Host registration needs a `DATABASE_URL` the host can reach, or `docker compose exec` on a rebuilt worker.

### Next task

Task 08 — Investigator agent with citations to source files.
