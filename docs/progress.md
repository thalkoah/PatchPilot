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

## Task 08 — Investigator agent with citations to source files

Status: complete (2026-11-03).

A task with a `repo_id` follows `START -> triage -> investigate -> finalize -> END`. A task without one still follows `START -> triage -> finalize -> END` and stores the same triage object in `result_json`. `InvestigatorAgent` may call `list_tree`, `read_text_file`, and `search_text` at most `INVESTIGATOR_MAX_TOOL_CALLS` times (default 6, hard maximum 12). The stored `InvestigationReport` has hypotheses, `relative_path:line` citations, probable files, and unanswered questions. A citation is kept only when `read_text_file` returned that path and those line numbers. An invented path is omitted from the task and from the event payload. File text is not stored. The investigate stage is in `GET /api/v1/tasks/{id}/events` when a report was saved.

`FakeProvider` scripts the demo bug and three bad replies: missing evidence, a citation of `invented/not_read.py`, and a reply that obeys `PATCHPILOT_INJECT` in a source comment.

### Evidence

`uv run pytest` — 172 passed in 11.62s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 82 files already formatted.

The demo investigation cites `src/demo_service/discounts.py` at the `return KNOWN_TIERS[customer_tier]` line and `src/demo_service/app.py` at the `discount_percent` call. Those strings are the file lines, not text copied into `result_json`. The docstring sentence `Known tiers return a percent.` is absent from the task JSON and from the checkpoint rows. A registered-repo worker run stored `rejected_citations` 0 and an investigate `stage_completed` event whose payload lists the citation labels.

The missing-evidence fixture stores no citations after list and search. The bogus-path fixture and the injection comment both leave `rejected_citations` at least 1, and neither `invented/not_read.py` nor `secrets/api_token.txt` appears in the stored report. A `../` read is rejected as `invalid_path` before the tool opens the parent. A tool budget of 2 stops on the second call. A task with no repository makes one model call. A repository id with no opened checkout fails at stage `investigate` and does not call the model again.

The running Compose stack was left as it was. It does not include this graph until the next image rebuild recreates the worker.

### Changed files

- `src/patchpilot/llm/investigation.py` — report, citation, and tool-step schemas
- `src/patchpilot/agents/` — budget and `InvestigatorAgent`
- `src/patchpilot/llm/fake.py` — demo, missing-evidence, bogus-citation, and injection fixtures
- `src/patchpilot/workflow/graph.py` — conditional `investigate` node
- `src/patchpilot/worker.py`, `repositories/jobs.py` — open the registered checkout and record the stage
- `src/patchpilot/config.py` — `INVESTIGATOR_MAX_TOOL_CALLS`
- `tests/test_investigator.py`, plus workflow, config, and Compose contract updates
- `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include this graph yet.
- Host Ollama was not asked to investigate. The tool loop and the citation filter were exercised with `FakeProvider`.
- A search hit is not a citation. Only lines returned by `read_text_file` can be cited.
- The filter drops an unread path. It does not rewrite a hypothesis that also cites a line that was read and happens to repeat comment text.
- Resuming an investigate node that did not finish repeats the read-only tool calls. The reads are not checkpointed.
- There is no separate artifact table. The report lives on `result_json`, and the event stores citation labels only.
- The Task 07 limits still apply: a hard link out of the checkout is not detected, and a file can change between the path check and the read.

### Next task

Task 09 — Structured planning agent.

## Task 09 — Structured planning agent

Status: complete (2026-11-03).

A task with a `repo_id` follows `START -> triage -> investigate -> plan -> finalize -> END`. A task without one still stops after triage. `PlannerAgent` makes one `generate_structured` call and returns a `ChangePlan`: problem statement, assumptions, modifications, test strategy, risks, confidence, and `can_proceed`. A modification is an existing `.py` file in the registered checkout, or a new `tests/test_*.py` path under a `tests` directory that already exists. The planner sets `can_proceed` false when triage needs clarification, when the investigation cited no line, or when every proposed path is unsafe. Unsafe paths are omitted from the stored plan. Nothing in this stage writes the checkout.

A finished plan is a versioned artifact. The file is `{task_id}/plan/v{n}.json` under `ARTIFACTS_ROOT`. The `artifacts` row stores that relative path, the sha256, the size, and the version. The same bytes keep the same version. `GET /api/v1/tasks/{id}/plan` returns the newest revision, and `?version=` returns an older one. The response has no filesystem path. Alembic head is `0004_artifacts`.

### Evidence

`uv run pytest` — 186 passed in 12.63s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 91 files already formatted.

The demo plan names `src/demo_service/discounts.py` and `src/demo_service/app.py` as existing files and `tests/test_unknown_tier_added.py` as a new test. That new file is not created. A snapshot of `examples/demo_service` is unchanged. `GET /api/v1/tasks/{id}/plan` returns version 1, `can_proceed` true, and a checksum that matches the artifact file. `?version=2` is 404. Changing the file bytes makes the same GET return 404. The plan event lists the accepted paths and does not include source text.

An ambiguous report is stored as a plan with `can_proceed` false and no modifications. A draft that also names `../etc/passwd`, `Dockerfile`, and `.github/workflows/ci.yml` keeps only `src/demo_service/discounts.py`. A draft with only those unsafe paths declines, and none of those paths are in the stored plan. A symlink, a parent path, and a checkout with no cited line also decline. Invalid plan JSON fails at stage `plan` with `ChangePlan rejected model output`, and the secret in the prompt is not in `result_json`. Saving the same plan twice stays on version 1. A different plan becomes version 2, and version 1 is still readable.

The running Compose stack was left as it was. It does not include this migration, the plan node, or the `artifacts` volume until the next image rebuild recreates the API and worker.

### Changed files

- `alembic/versions/20261103_0004_artifacts.py` — `artifacts`
- `src/patchpilot/llm/plan.py` — `ChangePlan` and proposed modifications
- `src/patchpilot/agents/planner.py`, `plan_paths.py` — one model call, then the path and evidence check
- `src/patchpilot/artifacts.py`, `repositories/artifacts.py`, `schemas/plans.py` — versioned plan files and `GET /api/v1/tasks/{id}/plan`
- `src/patchpilot/workflow/graph.py` — `plan` node after investigation
- `src/patchpilot/worker.py`, `repositories/jobs.py` — store the artifact and record the plan stage
- `src/patchpilot/llm/fake.py` — valid, ambiguous, unsafe, and schema-failure fixtures
- `tests/test_planner.py`, plus workflow, investigator, checkpoint, config, and Compose contract updates
- `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include this graph or the artifact volume yet.
- Host Ollama was not asked to plan. The path filter and the artifact API were exercised with `FakeProvider`.
- A decline is a completed task with `can_proceed` false. A schema failure is a failed task at stage `plan`, and that failure row stores the error without the investigation.
- Once any line was cited, the planner may name an existing `.py` file that was not itself cited. A new test path does not have to be cited. The path still has to sit inside the checkout and match the allowlist.
- The plan does not write the checkout, and it does not record file bytes. A later edit has to check the file again.
- A hard link that leaves the checkout is still not detected. A symlink is not a permissible path.
- If the database transaction rolls back after the plan file is written, the file can remain without a row. A checksum mismatch is returned as a missing plan.
- `can_proceed` does not authorize a write. Approval is the next task.

### Next task

Task 10 — Human approval with LangGraph interrupt/resume.

## Task 10 — Human approval with LangGraph interrupt/resume

Status: complete (2026-11-04).

A plan that names a code change follows `plan -> request_plan_approval -> finalize`. `request_plan_approval` calls LangGraph `interrupt()` and does not write the database or the checkout before that call, because resume runs the node from the top. The worker then stores the plan artifact, inserts one pending `approvals` row, sets the task to `waiting_approval`, writes `approval_requested`, and ends the lease.

`GET /api/v1/tasks/{id}/approvals` lists the rows. `POST /api/v1/tasks/{id}/decisions` takes `approval_id`, `decision` (`approve` or `reject`), and an optional reason. The same decision again returns the stored row and does not queue another run. A different decision is HTTP 409. A new decision sets the task back to `queued` and writes `approval_decided`. The next claim resumes the same thread id with `Command(resume=...)`.

Approval completes the task and does not apply a patch. Rejection is terminal `rejected`, with the reason as the summary, and writes `task_rejected`. A declined plan never opens an approval. Alembic head is `0005_approvals`.

### Evidence

`uv run pytest` — 190 passed in 18.84s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 95 files already formatted.

The approve test closes the checkpointer after the pause, opens a new one, and sees the task still `waiting_approval` with a checkpoint row for that thread id. A second worker poll claims nothing. Approving twice keeps the first reason. A later reject is HTTP 409. The resumed worker finishes `completed` without another model call. `examples/demo_service` is byte-for-byte unchanged, including the absence of `tests/test_unknown_tier_added.py`. The event list has one `approval_requested`, one `approval_decided`, and one `task_completed`. The `approvals` table has one row.

The reject test records the reason, ignores a second reject, refuses an approve with HTTP 409, and ends `rejected`. There is no `task_completed` event. The checkout snapshot matches the one taken before the run.

An ambiguous report still completes with `can_proceed` false and `GET /approvals` returns `{"items": []}`. An unknown approval id is HTTP 404. `decision: "ship"` is HTTP 422 and leaves the task `queued`.

The running Compose stack was left as it was. It does not include this migration or the approval node until the next image rebuild recreates the API and worker.

### Changed files

- `alembic/versions/20261104_0005_approvals.py` — `approvals`
- `src/patchpilot/db/models.py` — `Approval`
- `src/patchpilot/repositories/approvals.py`, `schemas/approvals.py`, `routes/tasks.py` — list and decide
- `src/patchpilot/workflow/graph.py` — `request_plan_approval` and `Command(resume=...)`
- `src/patchpilot/repositories/jobs.py`, `worker.py` — pause, release the lease, resume
- `src/patchpilot/audit.py` — `approval_requested`, `approval_decided`, `task_rejected`
- `tests/test_approvals.py`, plus planner, investigator, workflow, and checkpoint updates
- `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include this migration or the approval node yet.
- Restart was a new checkpointer connection and a new worker poll against the same database. The API and worker containers were not recreated.
- Host Ollama was not asked to plan. Approve, reject, and the repeated decision were exercised with `FakeProvider`.
- An approved plan is a completed task. This milestone does not create a worktree or apply a patch. The checkout is unchanged after approval and after rejection.
- `GET /approvals` returns pending and decided rows. An unknown task is HTTP 404. A task with no request returns an empty list.
- One task has at most one pending approval. A second pause reuses that row.
- The optional reason is at most 300 characters. A repeated decision keeps the first reason.
- A decision is accepted only while the task is `waiting_approval`. The same decision after the task has moved on returns the stored row and does not queue another run.
- If resume still reports an interrupt, the task is stored as `failed` instead of being parked again.

### Next task

Task 11 — Isolated Git workspace and immutable baseline.

## Task 11 — Isolated Git workspace and immutable baseline

Status: complete (2026-11-04).

An approved plan follows `request_plan_approval -> prepare_workspace -> finalize`. Rejection and a declined plan skip the workspace. `prepare_workspace` makes a bare clone of the registered checkout under `WORKSPACE_ROOT/<task-id>/clone` with `--no-hardlinks`, removes `origin`, and adds a worktree at `WORKSPACE_ROOT/<task-id>/worktree` on `patchpilot/<task-id>` at the source HEAD. `git worktree add` runs against that clone. The registered `.git` is not a worktree parent. There is no fetch and no push. Git hooks are pointed at `/dev/null`, and protocols other than `file` are refused.

The branch and both relative paths are derived from the task id. A checkpoint value that names another path is rejected. `result_json.workspace` stores the base commit and the branch. The `workspaces` row stores those plus the relative paths, with check constraints that tie them to the task id. Alembic head is `0006_workspaces`. Completing the task leaves the worktree in place. `WorkspaceRepository.retire` deletes that task directory and marks the row `removed`. A second retire returns the same row.

### Evidence

`uv run pytest` — 202 passed in 15.25s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 102 files already formatted.

The source snapshot covers the working tree and `.git`, including the index and refs. It matches after the clone, after a second prepare, and after teardown. `.git/worktrees` is absent on the source. The worktree HEAD equals the source HEAD, its common directory is the private clone, and that clone has no remotes and no `http` URL. Editing `app.py` in the worktree leaves the source file unchanged. A read-only source still clones. A symlink where the task directory should be, a symlink `.git`, and a workspace root inside the source are refused, and the outside directory stays empty. A junk directory at the task path is replaced. A stored path of `../elsewhere/clone` is rejected in Python and a mismatched branch is rejected by PostgreSQL.

The approve test copies `examples/demo_service`, commits it, and resumes after approval. The task completes with `patchpilot/<task-id>` and the copy's HEAD. `tests/test_unknown_tier_added.py` is not created in the source or the worktree. The absolute workspace path is absent from the task JSON and the workspace events. One `workspaces` row records `<task-id>/clone` and `<task-id>/worktree` as `active`. Retire removes the directory, leaves the source snapshot unchanged, and a second retire stays `removed`. Rejection and a declined plan leave the workspace directory empty and insert no row. The demo checkout used by those two tests is unchanged.

The running Compose stack was left as it was. It does not include this migration, git, or the `workspaces` volume until the next image rebuild recreates the worker.

### Changed files

- `alembic/versions/20261104_0006_workspaces.py` — `workspaces`
- `src/patchpilot/db/models.py` — `Workspace`
- `src/patchpilot/workspace/` — path layout, clone, and worktree cleanup
- `src/patchpilot/repositories/workspaces.py`, `repositories/jobs.py` — association and `prepare_workspace` events
- `src/patchpilot/workflow/graph.py`, `worker.py` — node after approval
- `src/patchpilot/config.py` — `WORKSPACE_ROOT`
- `tests/test_workspace.py`, `tests/test_approvals.py`, plus workflow, checkpoint, config, and Compose contract updates
- `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include this migration, git, or the workspace volume yet.
- Host Ollama was not asked to plan. The clone, the approval resume, and the rejection were exercised with `FakeProvider`.
- The completed task keeps its worktree. Retirement is explicit. This milestone does not apply the plan.
- The baseline is the source HEAD. Uncommitted files in the registered checkout are not copied.
- A second prepare reuses the worktree when the branch and commit still match, including edits already in that worktree.
- If the workspace row cannot be stored, the worker deletes the task directory and marks the task failed.
- A crash after the clone and before the row commit leaves the directory. The next attempt reuses it when the branch and commit still match.
- `result_json` does not include the relative path. The row does, and the delete path is computed from the task id again.
- Git is installed in the runtime image. The host tests used the system `git`.
- `safe.directory` is set for the source, the clone, and the worktree so a checkout owned by another user can be read. The command list still does not take a path from the model.
- A checkpoint that already moved past approval and is waiting on the old edge to `finalize` fails closed on resume, because that state has no workspace. A task parked on the interrupt resumes into `prepare_workspace`.

### Next task

Task 12 — Developer agent produces a validated patch.

## Task 12 — Developer agent produces a validated patch

Status: complete (2026-11-04).

An approved plan follows `prepare_workspace -> develop -> finalize`. Rejection and a declined plan still skip the worktree. `develop` makes one `generate_structured` call for a `PatchProposal`: full-file edits with an expected sha256, or one unified diff. `apply_patch` then checks that proposal against the approved plan and the private worktree. A path is writable only when the plan named it and it is still an existing `.py` file or a new `tests/test_*.py` file. Symlinks, secrets, `.git`, CI workflows, Docker files, dependency manifests, traversal, binary bytes, a stale base, and a diff over 16384 bytes are refused. `git apply --check` runs before `git apply`. The command list is fixed. There is no shell and no test run.

The model proposal and the applied diff are different artifacts, `proposal` and `diff`, with different sha256 values. `GET /api/v1/tasks/{id}/patch` returns both. `result_json.patch` stores the summary, the paths, and the two checksums. It does not store the diff text. A failed patch deletes that task's workspace directory. The registered checkout is not modified. Alembic head is still `0006_workspaces`.

### Evidence

`uv run pytest` — 210 passed in 15.46s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 111 files already formatted.

The approve test copies `examples/demo_service`, commits it, and resumes after approval. The task completes at `finalize`. The worktree `discounts.py` defines `UnknownTierError`, and `tests/test_unknown_tier_added.py` exists there. Neither change is in the source checkout. The source snapshot, including `.git`, matches the snapshot taken before the run. `GET /api/v1/tasks/{id}/patch` returns a unified diff whose checksum is not the proposal checksum. The task JSON and the develop events do not contain `diff --git`. The develop events are one `stage_started` and one `stage_completed`, and they carry both checksums. One extra model call is a `PatchProposal`. Rejection still makes no model call and leaves the workspace directory empty.

The patch tool tests refuse `../outside.txt`, a NUL byte, a diff over the byte cap, `src/other.py` when the plan did not name it, a non-diff, a wrong sha256, and a symlink. The outside file and the source snapshot stay unchanged. The worktree file that would have been edited stays at its baseline bytes.

The running Compose stack was left as it was. It does not include the develop node until the next image rebuild recreates the worker.

### Changed files

- `src/patchpilot/llm/patch.py` — `PatchProposal` and file edits
- `src/patchpilot/patching/` — path policy, `git apply --check`, and `apply_patch`
- `src/patchpilot/agents/developer.py` — one model call, then the tool
- `src/patchpilot/llm/fake.py` — demo patch and refusal fixtures
- `src/patchpilot/artifacts.py`, `repositories/artifacts.py`, `schemas/patches.py`, `routes/tasks.py` — proposal and diff artifacts, `GET /api/v1/tasks/{id}/patch`
- `src/patchpilot/workflow/graph.py`, `worker.py`, `repositories/jobs.py` — `develop` after the worktree
- `tests/test_patch.py`, `tests/test_approvals.py`, `tests/test_workflow.py`
- `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include the develop node yet.
- Host Ollama was not asked to write a patch. The apply tool and the approval resume were exercised with `FakeProvider`.
- The new regression file is written in the worktree. Nothing runs it. Test execution is the next task.
- The checkpoint holds the patch text so the worker can store the artifact. `result_json` and the events do not. The API reads the artifact files.
- A develop attempt that applies the patch and then loses the lease can fail the retry: the expected sha256 is the original file, and the worktree already has the edit. The failed task then deletes that workspace directory.
- `git apply --check` failure is reported as a malformed patch. The worktree is left unchanged because the check runs first.
- A symlink anywhere in the worktree makes the snapshot refuse the patch. The demo checkout has none.
- The applied artifact is the diff Git recorded after the edit, not the model's JSON. A unified diff the model sent is checked and applied, then replaced in the artifact by that recording.
- A failed patch deletes the task directory, so the dirty worktree is not kept for inspection. The source checkout is unchanged either way.
- The diff cap is 16384 bytes. The artifact table still allows 32768. A larger valid edit is refused before Git applies it.

### Next task

Task 13 — Restricted test runner and test artifacts.

## Task 13 — Restricted test runner and test artifacts

Status: complete (2026-11-04).

An approved patch follows `develop -> test -> finalize`. The worker copies the private worktree into `RUNNER_ROOT` and writes a request whose only fields are `schema_version` and the task id. A separate process, `python -m patchpilot.runner`, runs one fixed `pytest` command in that job directory. The worker waits for the result file. It does not start pytest. A request that names a command is stored as `invalid_request` and is not executed.

Compose runs that process as uid 1000 with `network_mode: none`, a read-only root, a writable `/runner` volume and `/tmp`, every capability dropped, `no-new-privileges`, and CPU, memory, and PID limits. The runner environment has no database password and no model settings. The runner refuses to start when those variables are present. Pytest's own environment is an allowlist, so a proxy or an extra variable on the runner is not visible to the suite. The shared volume is one tree for every job, which is weaker than a fresh mount per task. Worker concurrency stays 1.

A passing, failing, or timed-out suite is a `test-report` artifact. `GET /api/v1/tasks/{id}/tests` returns the exit code and bounded stdout and stderr. `result_json` and the `test` events keep the status and the checksum, not the output. If no result file appears, the task fails at stage `test`. The registered checkout is not modified. Alembic head is still `0006_workspaces`. Pytest is a runtime dependency so the runner image can execute it.

### Evidence

`uv run pytest` — 224 passed in 20.41s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 124 files already formatted.

The runner tests start `python -m patchpilot.runner` as its own process. A passing suite returns exit code 0 and the printed marker. A failing assertion returns exit code 1. A test that sleeps is killed after one second and stored as `timed_out` with no exit code. A client wait with no runner process raises before any process is started, and the request file has no command. A request that names `curl` and a shell `touch` is `invalid_request`, and the sentinel file is not created. A runner started with `DATABASE_URL` exits 2, and the password is not on stderr. `HTTP_PROXY` and an extra sentinel set on the runner are absent from the pytest output. A symlink in the worktree is refused and the outside file is unchanged. Ten thousand bytes of output are stored truncated at 2000 characters.

The approve test copies `examples/demo_service`, resumes after approval, and the runner executes the patched demo suite. The task completes at `finalize`. `result_json.tests.passed` is true and the exit code is 0. `GET /api/v1/tasks/{id}/tests` returns that report. The stdout does not contain the workspace, checkout, or runner path. The source snapshot, including `.git`, matches the snapshot taken before the run. The worktree has the patch. The source checkout does not. One `test` stage records `stage_started` and `stage_completed` with the checksum and without stdout.

The running Compose stack was left as it was. It does not include the runner service until the next image rebuild recreates the worker.

### Changed files

- `src/patchpilot/runner/` — protocol, staging, client, pytest execution, and the runner process
- `src/patchpilot/workflow/graph.py`, `worker.py`, `repositories/jobs.py` — `test` after `develop`
- `src/patchpilot/artifacts.py`, `repositories/artifacts.py`, `schemas/test_reports.py`, `routes/tasks.py` — `test-report` artifact and `GET /api/v1/tasks/{id}/tests`
- `src/patchpilot/config.py` — `RUNNER_ROOT`, timeout, and wait
- `tests/test_runner.py`, `tests/runner_support.py`, `tests/test_approvals.py` — pass, fail, timeout, unavailable, and the demo suite
- `tests/test_compose_contract.py`, `tests/test_config.py`, `tests/test_workflow.py`
- `compose.yaml`, `Dockerfile`, `pyproject.toml`, `uv.lock`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include the runner service yet. `network_mode: none` is checked in the Compose file. The host tests run the runner as a process, so they do not themselves create a network namespace.
- A failing or timed-out suite is still a completed task. The report says `passed` is false. Blocking success on a failed suite is the next task.
- The shared runner volume holds one job tree at a time. A second task is not isolated by a fresh container mount. Concurrency stays 1.
- The worker deletes the copied project after it reads the result. The result file stays so a resumed node does not ask for a second run. A crash before that read leaves the copy in the volume.
- If the worker stops waiting before the runner writes, the task fails at `test` and a late result is unused. `RUNNER_WAIT_SECONDS` must stay greater than `RUNNER_TIMEOUT_SECONDS`.
- Output is kept up to 2000 characters. A process that writes more than 256 KiB is cut off.
- A symlink anywhere in the copied tree refuses the job. The demo checkout has none.
- Host Ollama was not asked to write a patch. The suite result was produced with `FakeProvider` and the real demo tests.
- Pytest is installed in the API image as well as the runner image, because they share one image. The API does not invoke it.
- A checkpoint that already finished `develop` on the previous graph and is waiting to enter `finalize` does not pass through `test`. A task parked on approval resumes into `prepare_workspace`, then `develop`, then `test`.

### Next task

Task 14 — Independent reviewer and bounded repair cycle.

## Task 14 — Independent reviewer and bounded repair cycle

Status: complete (2026-11-05).

An approved patch follows `develop -> test -> review`. The reviewer sees the approved plan, the applied diff, the investigation citations, and the sandbox report. It returns findings, a severity, evidence, and `accept`, `repair`, or `escalate`. It has no file tool. A path named in the evidence that is not in the diff, the test output, or a citation is omitted. Policy decides. Success requires a passing suite, a diff that stays on the approved paths, no critical finding, and an accept recommendation. A failing suite or a repair request returns to `develop` and `test` at most twice. Each repair writes a `repair` event with the reason. Escalation, a critical finding the reviewer did not ask to repair, a forbidden diff, and a third request end as `failed` at stage `review`, and the summary says a person needs to intervene. The proposal, the applied diff, and the review are different artifacts. `GET /api/v1/tasks/{id}/review` returns the verdict, not the diff. Alembic head is still `0006_workspaces`.

### Evidence

`uv run pytest` — 231 passed in 27.80s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 129 files already formatted.

The approval test still completes the demo bug. `result_json.review` is `accept` / `accept` with `repair_count` 0. The review checksum is neither the diff checksum nor the proposal checksum. `GET /api/v1/tasks/{id}/review` returns that verdict and does not contain `diff --git`. The event list has one review `stage_started` and one `stage_completed`, and no `repair` event. The source checkout is unchanged.

`review repair once` applies a comment that leaves the `KeyError`, the suite fails, and the reviewer asks for a repair. The second patch defines `UnknownTierError`, the suite passes, and policy accepts. One `repair` event starts with `Tests failed.` Patch version 1 does not contain `UnknownTierError`. Version 2 does. Each version's diff checksum differs from its proposal checksum. The source file is unchanged. The worktree has the fix.

`review tests fail` keeps the lookup broken. The reviewer says `accept` on every pass. Policy does not accept the failing suite. The task ends `failed` at `review` after exactly two repairs and three `PatchProposal` calls. The summary says the repair limit is exhausted and a person needs to intervene. `result_json.tests.passed` is false and `result_json.review.action` is still `accept`.

`review reject` keeps a critical finding and asks for a repair on every pass. The task ends `failed` after two repairs. The summary says a critical review finding blocks the patch. `result_json.review.critical` is true.

`review forever` produces a passing suite and a non-critical repair request on every pass. The task ends `failed` after two repairs. The newest patch is version 3, and its checksum is not the proposal checksum or the review checksum. `GET /review` does not contain the diff.

A unit test refuses a diff that names `Dockerfile`. A reviewer reply that cites `invented/not_read.py` does not keep that path. `require_repair_budget(3)` raises. The source snapshot matches after every one of these runs, and a failed review removes that task's workspace directory.

The running Compose stack was left as it was. It does not include the review node until the next image rebuild recreates the worker.

### Changed files

- `src/patchpilot/llm/review.py` — `ReviewResult`, findings, severity, and action
- `src/patchpilot/agents/reviewer.py`, `review_policy.py`, `limits.py` — one model call, grounded evidence, and the repair cap
- `src/patchpilot/llm/fake.py`, `agents/developer.py` — repair fixtures and the reviewer note in the developer prompt
- `src/patchpilot/workflow/graph.py`, `worker.py`, `repositories/jobs.py` — `review` after `test`, at most two repairs, and the events
- `src/patchpilot/runner/client.py` — a new job id for each repair pass
- `src/patchpilot/artifacts.py`, `repositories/artifacts.py`, `schemas/reviews.py`, `routes/tasks.py` — `review` artifact and `GET /api/v1/tasks/{id}/review`
- `src/patchpilot/audit.py` — `repair` events
- `tests/test_review.py`, `tests/test_approvals.py`, `tests/test_workflow.py`
- `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include the review node yet.
- Host Ollama was not asked to review. The verdicts, the repair cap, and the demo suite were exercised with `FakeProvider`.
- The repair cap is two. `require_repair_budget` rejects a larger value. There is no environment setting that raises it.
- A failing suite whose reviewer says `accept` is still repaired, up to twice, because the accept is advisory. A critical finding paired with `accept`, and an `escalate` action, stop without a repair.
- A diff that leaves the approved paths fails at `review` before the reviewer is called. The apply tool already refuses that diff, so this is a second check.
- A failed review stores the proposal, the diff, the test report, and the review, then deletes the task workspace. The registered checkout is unchanged either way.
- The first test pass still uses the task id as the runner job id. Each repair uses a different id so the suite runs again. Resuming the same pass reuses that pass's result file.
- The shared runner volume is still one tree. Concurrency stays 1.
- A checkpoint that already finished `test` on the previous graph and is waiting to enter `finalize` fails closed, because a patch now needs a review.
- The reviewer does not choose the repair count. Policy does, and the events record the reason for each repair that was taken.

### Next task

Task 15 — Supervisor routing and real multi-agent orchestration.

## Task 15 — Supervisor routing and real multi-agent orchestration

Status: complete (2026-11-05).

Every specialist node returns to `supervisor`. Deterministic rules choose the next node and record why. The routes are `investigate`, `investigate_more`, `plan`, `replan`, `proceed_to_approval`, `prepare_workspace`, `develop`, `test`, `review`, `repair`, `finalize`, and `escalate`. A task with no repository finalizes after triage. An investigation that cited no line runs once more, then plans. A plan whose paths were all forbidden is replanned once, and the model may instead choose `finalize` for that case. A plan that names a change still waits for approval. Policy still owns repair: a failing suite or a repair request goes back to `develop` at most twice, and the model may only choose `repair` or `escalate` there. Escalation, rejection, and a decline end at `finalize`. The model is off unless `SUPERVISOR_MODEL=true`. A recommendation is schema-validated, and a route the rules did not allow is ignored. The model cannot approve a plan, write a file, or raise a limit. The graph stops after 24 supervisor decisions, after 900 seconds, or at LangGraph's recursion limit of 40. Each decision is a `route` event. Alembic head is still `0006_workspaces`.

### Evidence

`uv run pytest` — 242 passed in 29.61s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 132 files already formatted.

A task with no repository stores one `finalize` route whose reason says investigation is skipped, and the only model call is `TriageResult`. A missing-evidence report is investigated a second time, then planned, and the stored plan has `can_proceed` false. One `ChangePlan` call is made. An all-forbidden plan is replanned once and then declines. Two `ChangePlan` calls are made. With `SUPERVISOR_MODEL=true`, a recommendation of `proceed_to_approval` on a task with no repository stays `finalize`, and the stored reason names the rejected route. The model's own reason text is not stored. The same switch lets the model choose `finalize` instead of `replan` for an all-forbidden plan, and that run makes one `ChangePlan` call. A reviewer `escalate`, a repair request, a step cap of 24, and a passed deadline each select the rule's route in the routing tests. Resuming a thread that stopped after triage enters `supervisor` and does not call the model again. The stored summary stays `pass-1`.

The running Compose stack was left as it was. It does not include the supervisor node until the next image rebuild recreates the worker.

### Changed files

- `src/patchpilot/agents/supervisor.py`, `llm/supervisor.py` — `SupervisorDecision`, the rules, and the optional recommendation
- `src/patchpilot/agents/limits.py` — investigation, plan, step, deadline, and recursion caps
- `src/patchpilot/workflow/graph.py` — supervisor node and conditional edges
- `src/patchpilot/llm/fake.py` — deterministic supervisor recommendations
- `src/patchpilot/audit.py`, `repositories/events.py`, `repositories/jobs.py` — `route` events
- `src/patchpilot/worker.py`, `config.py` — `SUPERVISOR_MODEL`, off by default
- `tests/test_supervisor.py`, plus workflow, event, investigator, checkpoint, config, and Compose updates
- `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include the supervisor node yet.
- Host Ollama was not asked to choose a route. The rules, the override, and the accepted recommendation were exercised with `FakeProvider`.
- `SUPERVISOR_MODEL` defaults to false. The rules are the route. Most decisions allow one route. The model can choose between `replan` and `finalize` when every proposed path was forbidden, and between `repair` and `escalate` when policy requested a repair.
- A second investigation starts over and spends another tool budget. It does not continue the first investigation's notes.
- A plan the model returns with invalid JSON still fails at `plan`. Replan is for a valid plan whose paths were all rejected. A deliberate decline with no rejected path is not replanned.
- The step cap is 24, the deadline is 900 seconds from the first supervisor decision, and the LangGraph recursion limit is 40. They are constants. An environment setting cannot raise them.
- Route events are written when the run is stored. On the first transaction they follow triage completion. On a resumed run, decisions already stored are not inserted again. The payload index is the decision order.
- The event ordinal cap is 40 so one transaction can hold the route rows and the stage rows. It was 20.
- A checkpoint that already chose its next node still enters that node. A task paused on approval resumes through the supervisor. A checkpoint that finished triage and was waiting for `finalize` now enters `supervisor`, then `finalize`.
- The supervisor does not read the checkout, apply a patch, or approve a plan. Those controls stay in the existing nodes.

### Next task

Task 16 — Model routing, usage, limits and operational controls.

## Task 16 — Model routing, usage, limits and operational controls

Status: complete (2026-11-05).

Each stage can name `fake`, Ollama, or an explicitly enabled OpenAI-compatible model. An empty `LLM_STAGE_MODELS` keeps `LLM_PROVIDER` and `LLM_MODEL` for triage, investigate, plan, develop, review, and supervisor. Current stages require `structured_output`. Fake always has it. Ollama has it, and native tool calling stays off unless `OLLAMA_TOOL_CALLING=true`. A cloud stage runs only when `LLM_CLOUD_ENABLED=true`, a base URL and API key are set, and `LLM_CLOUD_CAPABILITIES` includes `structured_output`. The cloud client is built only for a stage that selects it. The key is a bearer header on the worker. The API and the runner do not receive it.

A task stops at its model-call, tool-call, reported-token, runtime, parallelism, or repair ceiling. Defaults are 24 model calls, 12 tool calls, 100000 tokens, 900 seconds, parallelism 1, and 2 repairs. Hard caps are 48, 24, 500000, 3600, 1, and 2. Tokens are counted only when the provider reports them. Missing counts stay null. Two invalid outputs for the same stage, provider, and model open a circuit, and the next call does not reach the provider. Invalid JSON, timeouts, and HTTP 408, 429, 502, 503, and 504 wait between attempts. HTTP 500 and connection failures are not retried. Local Ollama calls take one in-process slot and one Postgres advisory lock. Fake and cloud do not take that slot.

Each metered call is a `model_calls` row: stage, provider, model, latency, reported tokens, outcome, and cost. Cost is estimated only from a local JSON file at `LLM_PRICING_PATH` whose entry for that exact model has `verified: true`. Otherwise cost is unknown. Alembic head is `0007_model_calls`. `GET /api/v1/tasks/{id}/usage` reads those rows and does not call a model.

### Evidence

`uv run pytest` — 260 passed in 29.57s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 145 files already formatted.

The usage tests exhaust every budget with `FakeProvider`: model calls, reported tokens, runtime, parallelism, the local slot, investigator tool calls, and repairs. A third invalid output does not call the provider. A pricing file with `verified: true` stores the calculated cost. A missing file, an unverified entry, and a call with no token counts stay unknown. An Ollama 429 waits and then succeeds. An Ollama 500 is one request. `TASK_MAX_PARALLELISM=2` is a validation error. A second advisory lock on disposable PostgreSQL does not acquire. `GET /api/v1/tasks/{id}/usage` after one fake triage lists that call with cost unknown, and a missing task is HTTP 404.

`llm_provider=openai` is still rejected until the cloud flag, URL, and key are set. A stage that needs `structured_output` fails closed when the cloud capability list omits it. Building a fake router does not construct an HTTP client. A disabled cloud provider does not open one. An enabled provider with a mock transport posts once to `{base}/chat/completions`, sends the key as a bearer header, and does not put the key in the URL.

The running Compose stack was left as it was. It does not include `model_calls` or the usage route until the next image rebuild recreates the API and the worker.

### Changed files

- `src/patchpilot/llm/routing.py`, `meter.py`, `budget.py`, `capabilities.py`, `pricing.py`, `backoff.py`, `local_gate.py`, `usage_report.py`, `openai_compatible.py` — stage routing, ceilings, metering, and the cloud client
- `src/patchpilot/llm/errors.py`, `factory.py`, `fake.py`, `ollama.py` — error classes, reported usage, and transient backoff
- `src/patchpilot/config.py`, `worker.py`, `workflow/graph.py`, `agents/investigator.py` — settings, the worker router, and per-stage calls
- `alembic/versions/20261105_0007_model_calls.py`, `db/models.py`, `repositories/model_calls.py`, `schemas/usage.py`, `routes/tasks.py`, `dependencies.py` — `model_calls` and `GET /api/v1/tasks/{id}/usage`
- `tests/test_usage.py`, plus checkpoint head and boundary updates
- `compose.yaml`, `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include this revision yet.
- Host Ollama and a live cloud API were not called. Backoff and the cloud request were exercised with HTTP mocks. Budgets, the circuit, and the usage route were exercised with `FakeProvider`.
- Tests that inject a provider do not meter. The worker builds the router, and therefore the records, only when it creates the provider itself.
- Tool-call counts live in the running task. A resumed investigation can spend another tool budget. Model-call counts, reported tokens, and the invalid-output streak are seeded from `model_calls`.
- A call that reports no tokens does not spend the token budget. A later call that does report tokens is checked against the sum of reported tokens.
- Cost stays unknown when any call in the task lacks a verified price or a token count. The usage payload does not add a partial total.
- Parallelism cannot be raised above one. The advisory lock is held for one Ollama call. Process exit releases it. A lock error does not include the database URL.
- Prompts and model text are not stored on `model_calls`.

### Next task

Task 17 — Local web dashboard and progress stream.

## Task 17 — Local web dashboard and progress stream

Status: complete (2026-11-05).

The API serves a server-rendered dashboard at `/`. The task list and the create form are HTML. Creating a task posts JSON to `POST /api/v1/tasks`. A task page shows status, stage, the report, triage, evidence citations, the plan, approval, the applied diff, the pytest report, the review, and model usage. Approve and reject post JSON to `POST /api/v1/tasks/{id}/decisions`. The page itself has no write route.

The task page polls `GET /tasks/{id}/progress` every 2 seconds. The response contains only events after the last id already on the page, and the script skips an id that is already present. A status or stage change reloads the page from the database. Reading the page does not need JavaScript. Connection strings, bearer tokens, `sk-` keys, secret-like JSON fields, and configured secrets of at least eight characters are replaced before render. The page does not show artifact filesystem paths. Cost stays `unknown` unless a stored call was estimated. The layout is one column below 40rem. The wheel includes the templates and the static files.

### Evidence

`uv run pytest` — 271 passed in 29.16s.

`uv run ruff check .` — all checks passed.

`uv run ruff format --check .` — 152 files already formatted.

`uv build --wheel` produced a wheel containing `patchpilot/web/templates/` and `patchpilot/web/static/`.

A local API on disposable PostgreSQL was opened in the browser. The create form posted the sample issue and opened the task page at status Queued. Two inserted events appeared in the timeline, two distinct ids, with the first still present after the second poll. The list then showed that task as Queued. At a 390px width the page did not scroll sideways, and the database password was not in the page text.

The running Compose stack was left as it was. It does not include this dashboard until the next image rebuild recreates the API.

### Changed files

- `src/patchpilot/web/` — pages, progress cursor, redaction, templates, and static files
- `src/patchpilot/api.py`, `dependencies.py` — dashboard routes, static files, repository lookup
- `tests/test_dashboard.py`, `tests/test_dashboard_view.py`
- `pyproject.toml`, `uv.lock` — Jinja2
- `.env.example`, `README.md`, `docs/progress.md`

### Limitations

- `docker compose` was not executed. The Compose containers were left running and do not include this dashboard yet.
- Creating a task and recording a decision need JavaScript. The HTML does not submit a second form endpoint.
- The progress poll reads status, stage, summary, and new events. Plan, diff, tests, review, and usage appear when the page reloads after the status or stage changes.
- A configured secret shorter than eight characters is not stripped from free text. Connection strings, bearer tokens, and `sk-` keys are still removed.
- Host Ollama was not asked to run a task. The browser check used the empty queue and inserted events. The worker was not started.
- The dashboard does not add a repository picker from the filesystem. The select lists enabled registry rows by display name.

### Next task

Task 18 — Evaluation harness and benchmark report.
