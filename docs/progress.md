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
