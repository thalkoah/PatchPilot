# PatchPilot

Local-first, human-supervised software maintenance. PostgreSQL stores bug-report tasks. FastAPI creates and reads them. The worker process is still idle: it does not claim jobs or call a model.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker Compose, or Podman with a Compose provider

The API is published only on `127.0.0.1:8000`. Database credentials in `.env.example` and the Compose defaults are development examples. Copy `.env.example` to `.env` to override them. `.env` is gitignored.

## Run the stack

```bash
docker compose up --build -d
docker compose ps
curl http://localhost:8000/health/ready
```

`GET /health/live` returns `{"status":"live"}` when the API process is up.

`GET /health/ready` opens a PostgreSQL connection and runs `SELECT 1`.

- Database accepts the probe: HTTP 200, `{"status":"ok","database":"ready"}`
- Database is unreachable: HTTP 503, `{"status":"unavailable","database":"unavailable"}`

The API container runs migrations before it serves traffic:

```bash
alembic upgrade head
```

That command is the image command and the Compose `api` command. It reads `DATABASE_URL` and refuses to start without it. Run the same command on the host with `uv run alembic upgrade head` when the API is not running in Compose. Point `DATABASE_URL` at `localhost` for a host process; Compose uses the `db` hostname.

The worker process stays running and does not claim jobs. Follow it with:

```bash
docker compose logs worker
```

Podman reads the same `compose.yaml`. When the Docker CLI is absent, use `podman-compose up --build -d`. `podman compose` needs a separate Compose plugin. Image names are fully qualified so a non-interactive Podman run does not prompt for a registry.

Stop the stack with `docker compose down`. Data stays in the `pgdata` volume, so a task is still there after the API and worker restart.

## Task API

`POST /api/v1/tasks` validates a title and description and inserts a `queued` task. Callers cannot set status, stage, `repo_id`, or results. Those columns exist for later milestones; `repo_id` stays null, and `stage` stays null until a worker runs.

- `201` — task created
- `404` — unknown task id
- `422` — invalid body or query

Title is 1–200 characters. Description is 1–20000 characters. Surrounding whitespace is removed. The status values are `queued`, `running`, `waiting_approval`, `completed`, `rejected`, `failed`, and `cancelled`. New tasks are always `queued`.

`GET /api/v1/tasks` returns newest `created_at` first, breaking ties by id. `total` counts the filtered set.

- `limit` — default 20, maximum 100
- `offset` — default 0, maximum 10000
- `status` — optional exact status

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"title":"Unknown customer tier crashes discount endpoint","description":"When the demo-service discount endpoint receives a customer tier it does not recognize, it returns HTTP 500. It should respond with HTTP 422 and a clear validation error."}'

curl -s http://127.0.0.1:8000/api/v1/tasks
curl -s http://127.0.0.1:8000/api/v1/tasks/<id>
curl -s 'http://127.0.0.1:8000/api/v1/tasks?status=queued&limit=20&offset=0'
```

## Tests and lint

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Health and settings tests do not need a running database. The readiness failure test connects to a closed local port and checks that the password is not returned or logged.

Task tests start a disposable PostgreSQL 16 container with `podman` or `docker`, apply `alembic upgrade head`, and talk to that database. The Compose volume is left alone. The example password in that container is `patchpilot_dev`.
