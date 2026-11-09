# MiniTask

A small, runnable task-board app made as a **test repository for PatchPilot**.
It has a three-column web UI, a REST API, SQLite persistence, and automated tests.
No JavaScript bundler or external frontend service is required.

![Tech](https://img.shields.io/badge/stack-FastAPI%20%7C%20SQLite%20%7C%20Vanilla%20JS-7563e6)

## Quick start (Docker)

From the `minitask/` directory:

```bash
docker compose up --build
```

Open **http://localhost:8000**. The API documentation is at **http://localhost:8000/docs**.
SQLite data lives in a persistent Docker volume. To stop: `docker compose down`.
To also **erase task data**, run `docker compose down -v`.

Only `127.0.0.1:8000` is published on the host; this is a local development app, not a public production service.

## Quick start (Python, without Docker)

On Fedora, from this directory:

```bash
./start.sh
```

The script is safe to repeat. It installs `python3` when that command is missing, creates `.venv` when it is missing, and installs dependencies when `requirements.txt` has changed. Then it serves the app on **http://127.0.0.1:8000**.

To set the environment up by hand (Python 3.11+):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

Open **http://localhost:8000**. The default SQLite file is `data/minitask.db`.
Change its location by setting `MINITASK_DB_PATH=/some/path/tasks.db`.

## Run tests

```bash
pytest -q
```

All tests use temporary, isolated SQLite databases; they will not touch real tasks.

## Baseline features

- Create a task with title, optional description, and initial status.
- See tasks in three columns: **To do**, **In progress**, **Done**.
- Move a task between columns with its status dropdown.
- Delete a task (with confirmation).
- Persist tasks across restarts in SQLite.
- Backend validation and 404 handling; no login and no external network dependency.
- Responsive desktop/mobile layout.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/tasks` | List tasks (newest first) |
| `POST` | `/api/tasks` | Create a task |
| `PATCH` | `/api/tasks/{id}` | Update title, description, or status |
| `DELETE` | `/api/tasks/{id}` | Delete a task |

Status values are `todo`, `in_progress`, and `done`. Example:

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"title":"Try PatchPilot","description":"Make a simple code change","status":"todo"}'
```

API requests are validated with Pydantic. Built-in OpenAPI documentation is available at `/docs`.

## Repository layout

```text
minitask/
├── app/
│   ├── main.py              # Routes and application startup
│   ├── models.py            # Pydantic schemas
│   └── database.py          # SQLite persistence
├── static/
│   ├── index.html           # Page markup
│   ├── style.css            # Styling
│   └── app.js               # API calls + task board interactions
├── tests/
│   ├── conftest.py          # Isolated SQLite client
│   └── test_api.py          # Baseline regression tests
├── data/                    # Local SQLite file, ignored by Git
├── Dockerfile
├── compose.yaml
└── requirements*.txt
```

## Git checkout

Registration requires a Git checkout. From the PatchPilot repository root:

```bash
uv run python scripts/init_demo_repo.py
```

The script is idempotent. It creates `examples/minitask/.git/` with one baseline commit. The parent repository ignores that directory. Do not `git add examples/minitask` while `.git` exists. Git would record a gitlink, and a clone of the parent repository would not contain these source files. From the PatchPilot root, `./scripts/add_all.sh` removes the nested `.git` directories, runs `git add --all`, and recreates the checkouts. It does not commit.

Register the checkout. On the host, point `DATABASE_URL` at a database you can reach and set `REPOSITORY_ROOT` to the absolute `examples` directory:

```bash
REPOSITORY_ROOT="$PWD/examples" uv run python -m patchpilot.source register \
  --name "MiniTask" \
  --path minitask
```

With Compose, the worker already sees this directory at `/repositories/minitask`, read-only:

```bash
docker compose exec worker python -m patchpilot.source register \
  --name "MiniTask" \
  --path minitask
```

The registered checkout is not modified. After a plan is approved, PatchPilot clones it into a private worktree and applies the patch there.

## Explicit boundaries

MiniTask is intentionally small: no accounts, no auth, no frontend framework,
no full-text search, no priorities, no due dates, and no drag-and-drop in v1.
Those are useful future PatchPilot assignments. Avoid putting real or sensitive
data into this test application.
