# PatchPilot

Local-first, human-supervised software maintenance. PostgreSQL stores bug-report tasks. FastAPI creates and reads them. A separate worker claims each queued task and runs a LangGraph triage workflow. Model calls go through `generate_structured` and stay out of the API.

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

The worker polls for `queued` tasks, claims one at a time, and triages it. It does not serve HTTP. Follow it with:

```bash
docker compose logs worker
```

`WORKER_IDLE_SLEEP_SECONDS` is the pause when the queue is empty. The default is 2 seconds.

Podman reads the same `compose.yaml`. When the Docker CLI is absent, use `podman-compose up --build -d`. `podman compose` needs a separate Compose plugin. Image names are fully qualified so a non-interactive Podman run does not prompt for a registry.

Stop the stack with `docker compose down`. Data stays in the `pgdata` volume, so a task is still there after the API and worker restart.

## Task API

`POST /api/v1/tasks` validates a title and description and inserts a `queued` task. The handler does not call a model. Callers cannot set status, stage, `repo_id`, or results. `repo_id` stays null. The worker fills `stage`, `result_summary`, and `result_json`.

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

## Triage workflow

The worker runs a LangGraph `StateGraph` with the path `START -> triage -> finalize -> END`. This version compiles the graph with no checkpointer. The task row in PostgreSQL is the result that is still there after a restart. Durable graph checkpoints are a later task.

The worker claims the oldest queued row, sets `running` and stage `triage`, and commits. That commit releases the row lock before the model call. `triage` calls `generate_structured` once. `finalize` checks the object again. A success stores status `completed`, stage `finalize`, the summary, and the validated triage object in `result_json`. A provider failure or any other error stores status `failed`, stage `triage`, a short summary, and `{"error": "..."}`. Those strings omit model text. Concurrency is 1. A crash during the model call leaves the task `running` until a later recovery task.

Smoke test against the running stack. The worker container claims the task:

```bash
uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000
```

One in-process iteration, for a migrated database that no other worker is polling:

```bash
uv run python scripts/check_workflow.py --once
```

Manual Ollama, still in-process. Point `DATABASE_URL` at a database the host can reach. The Compose database port is not published; use `--api-url` for the stack.

```bash
LLM_PROVIDER=ollama LLM_MODEL=gemma4:12b \
  OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  DATABASE_URL=postgresql://patchpilot:patchpilot_dev@127.0.0.1:5432/patchpilot \
  uv run python scripts/check_workflow.py --once
```

When the Compose worker itself uses Ollama, set `LLM_PROVIDER=ollama` and read the binding warning below. Wait longer for that poll:

```bash
uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000 --timeout 600
```

## Language model

`generate_structured(prompt, schema, options)` is the model call. `TriageResult` carries classification, severity, a short summary, acceptance criteria, confidence, and `needs_clarification`. `FakeProvider` returns deterministic fixtures. `OllamaProvider` posts to Ollama `/api/chat` with `stream` false and the JSON schema in `format`. Pydantic validates every response. Invalid JSON and timeouts retry at most `LLM_MAX_RETRIES` extra times (0–3, default 2). The next failure raises `LLMResponseError` or `LLMTimeoutError`. Those messages omit the model text.

`LLM_PROVIDER` defaults to `fake`. Leave that value for pytest and CI.

Offline check:

```bash
uv run python scripts/check_llm.py
```

`LLM_TIMEOUT_SECONDS` defaults to 180 seconds per attempt. A cold local model can use most of that. Raise it when a larger model still times out. The ceiling is 600 seconds, and `LLM_MAX_RETRIES` stays between 0 and 3.

Manual Ollama on the host, using a model you already have:

```bash
LLM_PROVIDER=ollama LLM_MODEL=gemma4:12b \
  OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  uv run python scripts/check_llm.py
```

The script prints one schema-valid `TriageResult` as JSON.

### Ollama from Compose on Fedora

The worker service adds the host gateway and leaves the provider on `fake` until you opt in:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

`host-gateway` is the host address on the Compose network. Podman Compose and Docker Compose both accept that value. When `OLLAMA_BASE_URL` is unset, the worker uses `http://host.docker.internal:11434`. A process on the host uses `http://127.0.0.1:11434`.

Ollama listens on `127.0.0.1:11434` unless `OLLAMA_HOST` says otherwise. Packets that arrive on the bridge address do not reach a loopback socket. Serving the worker means Ollama has to listen on the bridge address, or on every interface.

Keep port 11434 off the LAN and off the public internet. `OLLAMA_HOST=0.0.0.0:11434` accepts connections on every interface. On Fedora, leave 11434 closed in firewalld, or bind Ollama to the Compose bridge address only. PatchPilot sends no credentials to Ollama. Anyone who can open a TCP connection can use the model. Compose does not publish port 11434.

## Tests and lint

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_llm.py
```

`scripts/check_workflow.py --once` also needs `DATABASE_URL` and a migrated database. The pytest suite covers that path with `FakeProvider`.

Health and settings tests do not need a running database. The readiness failure test connects to a closed local port and checks that the password is not returned or logged.

Task tests start a disposable PostgreSQL 16 container with `podman` or `docker`, apply `alembic upgrade head`, and talk to that database. The Compose volume is left alone. The example password in that container is `patchpilot_dev`.
