# PatchPilot

Local-first, human-supervised software maintenance. This repository currently contains the runnable skeleton: PostgreSQL, a FastAPI process, and an idle worker.

No task API, agents, or model calls exist yet.

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

The worker process stays running and does not claim jobs. Follow it with:

```bash
docker compose logs worker
```

Stop the stack with `docker compose down`.

Podman reads the same `compose.yaml`. When the Docker CLI is absent, use `podman-compose up --build -d`. `podman compose` needs a separate Compose plugin. Image names are fully qualified so a non-interactive Podman run does not prompt for a registry.

## Tests and lint

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Unit tests do not need a running database. The readiness failure test connects to a closed local port and checks that the password is not returned or logged.
