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
