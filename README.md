# PatchPilot

Local-first, human-supervised software maintenance. PostgreSQL stores bug-report tasks. FastAPI creates and reads them. A separate worker claims each queued task and runs a LangGraph triage workflow. Model calls go through `generate_structured` and stay out of the API.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker Compose, or Podman with a Compose provider

The API is published only on `127.0.0.1:8000`. Database credentials in `.env.example` and the Compose defaults are development examples. Copy `.env.example` to `.env` to override them. `.env` is gitignored.

## First run

On Fedora or macOS, one script installs what is missing and starts the stack. It is safe to run again. An existing `.env` is kept, the demo checkout is initialized only once, a model that is already present is not pulled again, and Compose leaves a running stack in place.

```bash
./scripts/bootstrap.sh
```

The script reads `LLM_MODEL` and `LLM_STAGE_MODELS` from `.env` (or the defaults in `src/patchpilot/config.py` when `.env` is new) and pulls each distinct Ollama model those settings name. Changing the model list does not require editing the script. `--skip-models` skips that download. `--no-start` prepares the machine and does not run Compose. `--skip-system` does not install operating-system packages.

The dashboard is <http://127.0.0.1:8000/> when the script finishes.

## Run the stack

```bash
docker compose up --build -d
docker compose ps
curl http://localhost:8000/health/ready
```

`GET /health/live` returns `{"status":"live"}` when the API process is up.

`GET /health/ready` opens a PostgreSQL connection and runs `SELECT 1`.

The same process serves the local dashboard at <http://127.0.0.1:8000/>.

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

Stop the stack with `docker compose down`. Named volumes `pgdata`, `artifacts`, and `workspaces` stay, so tasks, plans, and private worktrees are still there after the API and worker restart. `docker compose down -v` deletes those volumes. Compose rotates each service log at 10 MB and keeps three files.

A five-minute walkthrough is in `docs/DEMO.md`. The runtime picture is in `docs/ARCHITECTURE.md`. The decisions behind the worker, the graph, PostgreSQL checkpoints, deterministic routing, and the restricted runner are in `docs/adr/`.

## Task API

`POST /api/v1/tasks` validates a title and description and inserts a `queued` task. The handler does not call a model. Callers cannot set status, stage, or results. `repo_id` is optional. Omit it, or set it to the id of an enabled row in `repositories`. An unknown id, a disabled id, a filesystem path, or a Git URL is HTTP 422 and stores nothing. The worker fills `stage`, `result_summary`, and `result_json`. When `repo_id` is set, it investigates that checkout after triage.

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
curl -s http://127.0.0.1:8000/api/v1/tasks/<id>/events
curl -s 'http://127.0.0.1:8000/api/v1/tasks?status=queued&limit=20&offset=0'
```

`GET /api/v1/tasks/{id}/events` returns the append-only trail, oldest first. A missing task is HTTP 404. A task with no events yet returns `{"items": [], "truncated": false}`. The event names are `stage_started`, `stage_completed`, `model_error`, `task_completed`, `task_rejected`, `approval_requested`, `approval_decided`, `retry`, `recovery`, `repair`, `route`, and `publication_opened`. A `route` event records why the supervisor chose the next stage. Its payload is the route name, the reason, and the stage that had just finished. Payloads are small JSON objects. Fields whose names look like passwords, tokens, or database URLs are stored as `[redacted]`.

## Registered repositories

Checkouts live under `REPOSITORY_ROOT`. A task can name one of them by id. It cannot send a path or a Git URL. Registration is a local admin command, not an HTTP route: the API has no authentication, and a request that reaches the container is not treated as an administrator.

The stored path is relative to `REPOSITORY_ROOT`. The database does not keep the host absolute path, a Git remote, or a token. `registration` records the checked-out commit id. An optional `--github owner/name` records the GitHub repository that a later publication approval may use. That pair is not a URL.

Initialize the sample checkout, then register it. On the host, point `DATABASE_URL` at a database you can reach and set `REPOSITORY_ROOT` to the absolute `examples` directory:

```bash
uv run python scripts/init_demo_repo.py
```

That creates `examples/demo_service/.git/` and `examples/minitask/.git/`. The parent repository ignores both. Do not `git add` either directory while the nested `.git` exists, or Git records a gitlink instead of the source files. From the repository root, `./scripts/add_all.sh` removes those directories, runs `git add --all`, and recreates the checkouts. It does not commit. Then register:

```bash
REPOSITORY_ROOT="$PWD/examples" uv run python -m patchpilot.source register \
  --name "Demo service" \
  --path demo_service
uv run python -m patchpilot.source list
```

Compose mounts `${REPOSITORY_ROOT_HOST:-./examples}` on `/repositories` in the worker only, with `:ro,Z`. The `Z` option applies a private SELinux label when the worker container is created, so an enforcing Fedora worker can read the checkout. A checkout added after that stays `user_home_t`, and registration fails with `PermissionError` on `/repositories/<name>/.git/HEAD`. `./scripts/bootstrap.sh` copies the mount label onto those files before it registers them. The worker process sees `REPOSITORY_ROOT=/repositories`. The API container does not receive that mount. After the worker image contains this command, the same registration can run inside it:

```bash
docker compose exec worker python -m patchpilot.source register \
  --name "Demo service" \
  --path demo_service
```

`examples/demo_service` is a small FastAPI project. Known customer tiers return a discount. An unknown tier currently produces HTTP 500; the intended response is HTTP 422. See `examples/demo_service/README.md` for the baseline pytest command and the regression that fails on purpose.

`examples/minitask` is a small task-board FastAPI project. The same script creates its checkout. Register it with `--name "MiniTask"` and `--path minitask`. See `examples/minitask/README.md`.

Read-only tools are `list_tree`, `read_text_file`, and `search_text`. Each takes a relative path under that one checkout. They refuse `..`, absolute paths, symlink escapes, `.git`, credential names, binaries, and files past the byte limit. Listings omit denied names. Search uses a literal substring and skips symlinks. The investigator is the only caller. The limits are 200 tree entries, depth 6, 32768 bytes, 400 lines, 100 searched files, and 40 matches. One investigation may make at most `INVESTIGATOR_MAX_TOOL_CALLS` tool calls (default 6, hard maximum 12).

## Dashboard

Open <http://127.0.0.1:8000/> after the stack is up. The page is server-rendered HTML. Reading it does not need JavaScript. Creating a task and recording a decision do: those buttons post JSON to the task API. The page does not call a model.

The default `fake` provider can run this walkthrough without Ollama or a cloud key. Register the demo checkout first, and leave the worker running. The dashboard only displays what the worker stores.

1. Initialize and register `examples/demo_service` as above. `python -m patchpilot.source list` prints the repository id.
2. Open the dashboard. Use the sample issue below and select that repository. The create button posts JSON to `POST /api/v1/tasks` and then opens the task page.
3. The task page polls `GET /tasks/<id>/progress` every 2 seconds. Each event id is added once. When the status or stage changes, the page reloads from the database.
4. At **Waiting for approval**, approve or reject the plan. The buttons post JSON to `POST /api/v1/tasks/<id>/decisions`. Rejection stops without a patch. Approval continues in the private worktree.
5. The finished page shows the evidence citations, the plan, the applied diff, the pytest report, the review, and model usage. Links at the bottom are the same JSON records.

Sample issue:

```text
Title: Unknown customer tier crashes discount endpoint

When the demo-service discount endpoint receives a customer tier it does not recognize, it returns HTTP 500. It should respond with HTTP 422 and a clear validation error. Find the cause, propose a minimal fix and add a regression test.
```

The layout is one column on a narrow screen. Database URLs, bearer tokens, `sk-` keys, and configured secrets are not shown. The page does not show artifact filesystem paths.

## Triage workflow

The worker runs a LangGraph `StateGraph`. Every specialist node returns to `supervisor`, which chooses the next node. The choice is a deterministic rule. Set `SUPERVISOR_MODEL=true` to also ask the configured model for a recommendation. The model result is schema-validated, and a route the rules did not allow is ignored. The model cannot approve a plan, write a file, or raise a limit. A task with no `repo_id` is finalized after triage. A task with a `repo_id` is investigated, then planned. An investigation that cited no line may run once more. A plan whose paths are all forbidden may be replanned once. A plan that declines continues to `finalize`. A plan that names a code change continues to `request_plan_approval`, which calls `interrupt()` and does not write the checkout. The graph stops after 24 supervisor decisions, after 2700 seconds, or at LangGraph's recursion limit of 40. The graph is compiled with the async Postgres checkpointer from `langgraph-checkpoint-postgres`. The LangGraph `thread_id` is the task UUID, with no extra prefix. Each finished node is stored before the next node starts (`durability="sync"`).

Checkpoint tables live in the same PostgreSQL database and stay out of Alembic. `AsyncPostgresSaver.setup()` creates `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs`, and `checkpoint_writes`. The business result stays on `tasks`: status, stage, `result_summary`, and `result_json`.

Initialize those tables with:

```bash
uv run python -m patchpilot.checkpoints
```

The command is idempotent. Point `DATABASE_URL` at a database the host can reach. The worker runs the same setup when it starts, then holds one autocommit connection until the process stops. Compose does not need a separate checkpoint service. The API does not open the checkpointer.

The worker claims the oldest queued row with `FOR UPDATE SKIP LOCKED`, opens a `task_runs` row, and commits. That commit releases the row lock before the model call. The run's `lease_until` is the database clock plus `WORKER_LEASE_SECONDS` (default 30). A background thread renews that lease only while this worker still holds it and the run is open, so a blocking model call does not freeze the lease.

A second worker will not take a task whose lease is still in the future, so two workers do not run the same task at the same time.

If the worker process dies, the lease expires. The next poll closes that run with `lease_expired`, writes a `recovery` event, and starts the next attempt when the attempt count is below `WORKER_MAX_ATTEMPTS` (default 3). The new attempt resumes the LangGraph thread when a checkpoint exists, and otherwise replays triage from the start. Event writes use an idempotency key, so replaying a stage does not insert a second copy. When the expired attempt is already the last one, the task becomes `failed` with `recovery_exhausted` and stays there.

`triage` calls `generate_structured` once. `investigate` runs only when triage succeeded and the task names a repository. It calls the three read-only tools under a fixed budget and returns an `InvestigationReport`: hypotheses, `relative_path:line` citations, probable files, and unanswered questions. A citation is stored only when `read_text_file` returned that file and those line numbers. A path the model invented is omitted, and the omitted path is not written into the task or the event payload. File text stays out of `result_json` and the audit events.

`plan` runs after a stored investigation. It asks for one `ChangePlan`: a problem statement, assumptions, proposed modifications, a test strategy, risks, confidence, and `can_proceed`. A modification is either an existing `.py` file in that checkout or a new `tests/test_*.py` path under a `tests` directory that already exists. The planner declines when triage needs clarification, when no line was cited, or when every proposed path is unsafe. Unsafe paths are omitted. The node does not write the checkout.

A plan that finishes, including one that is waiting for approval, is stored as a versioned artifact. The file lives under `ARTIFACTS_ROOT`, and the `artifacts` table stores the relative path, sha256, size, and version. The same bytes keep the same version. `GET /api/v1/tasks/{id}/plan` returns the newest revision, and `?version=` returns an older one. The response does not include a filesystem path. Compose mounts one `artifacts` volume on the API and the worker at `/artifacts`.

`request_plan_approval` pauses the thread. The worker writes one `approvals` row, sets the task to `waiting_approval`, records `approval_requested`, and ends the lease so the task is not claimed again. `GET /api/v1/tasks/{id}/approvals` lists those rows. `POST /api/v1/tasks/{id}/decisions` accepts `approval_id`, `decision` (`approve` or `reject`), and an optional `reason`. The same decision again returns the stored row and does not queue another run. A different decision is HTTP 409. A new decision sets the task back to `queued` and writes `approval_decided`. The next worker claim resumes the same `thread_id` with `Command(resume=...)`.

Nothing in that pause writes the registered checkout. After an approval, `prepare_workspace` makes a private bare clone under `WORKSPACE_ROOT` and adds a worktree on `patchpilot/<task-id>` at the source HEAD. Git writes the worktree metadata into that clone, not into the registered `.git`. Creating the workspace does not fetch or push. The `workspaces` row stores the base commit, the branch, and the relative clone and worktree paths. Those paths are derived from the task id. `result_json` stores the base commit and the branch, not a host path. Compose mounts a `workspaces` volume on the worker at `/workspaces`. The API does not receive it. Rejection and a declined plan do not create a workspace. Rejection becomes terminal `rejected` with the reason, or `The plan was rejected.` when no reason was sent, and writes `task_rejected`. A declined plan never opens an approval. It completes at `finalize`.

`develop` runs only after that worktree exists. It asks for a few exact text replacements, renders those as one unified diff, stores that as a `PatchProposal`, then calls `apply_patch`. Each replacement has to fit the response cap, so the model is not asked for a full file or a git diff. The tool accepts a path only when it is in the approved plan and is still an existing `.py` file or a new `tests/test_*.py` file. Symlinks, secrets, `.git`, CI workflows, Docker files, dependency manifests, traversal, binary bytes, a stale base hash, and a diff larger than 16384 bytes are refused. `git apply --check` runs before `git apply`. There is no shell. The model proposal and the applied diff are stored as separate artifacts, `proposal` and `diff`, with different sha256 values. `GET /api/v1/tasks/{id}/patch` returns both. `result_json` keeps the checksums and the paths, not the diff text. The registered checkout is not modified.

`test` runs only after that patch. The worker copies the private worktree into `RUNNER_ROOT` and writes a request that contains a job id and nothing else. A separate `runner` Compose service reads that file and runs one fixed `pytest` command in that job directory. The model cannot choose the executable or the arguments. The worker waits for the result file and does not start pytest itself. Compose runs the runner as uid 1000 with `network_mode: none`, a read-only root, a writable `/runner` volume and `/tmp`, dropped capabilities, `no-new-privileges`, and CPU, memory, PID, and time limits. The runner process is not given the database password or model settings. The shared volume is one tree for every job, which is weaker isolation than a fresh mount per task, so worker concurrency stays 1. A passing, failing, or timed-out pytest is stored as a `test-report` artifact. `GET /api/v1/tasks/{id}/tests` returns the exit code and the bounded output. `result_json` keeps the status and the checksum, not the output. If the runner never writes a result, the task fails at stage `test` and the registered checkout is still unchanged. A later repair publishes a new job id, so the suite runs again. Resuming the same pass reuses the result file that is already there.

`review` runs after a stored test report. The reviewer sees the approved plan, the applied diff, the investigation citations, and the test report. It returns findings, a severity, evidence, and one action: `accept`, `repair`, or `escalate`. It has no file tool. Evidence that names a path absent from the diff, the test output, and the citations is omitted. Policy then decides. A passing suite, an allowed diff, no critical finding, and an accept recommendation finish the task. A failing suite, a repair request, or a critical repair request goes back through `develop` and `test` at most twice. Each repair writes a `repair` event with the reason. Escalation, a diff outside the approved paths, a critical finding the reviewer did not ask to repair, and a third request all end as status `failed` at stage `review`, with a summary that a person needs to intervene. The model proposal, the applied diff, and the review are separate artifacts. `GET /api/v1/tasks/{id}/review` returns the verdict and the policy decision, not the diff. `result_json.review` keeps the action, the policy, the repair count, and the checksum.

Publication is off unless `GITHUB_ENABLED=true`. The worker is the only process that receives `GITHUB_TOKEN`. The API and the runner do not. A token is used only for an `owner/name` that is both stored on the registered checkout (`python -m patchpilot.source register --github owner/name`) and listed in `GITHUB_ALLOWED_REPOS`. After policy accepts a patch, the graph pauses a second time. Nothing is pushed while that approval is pending. Approving it pushes only `patchpilot/<task-id>`, never the default branch, and opens a draft pull request. The body contains the issue, the plan's reasoning, a compare link, the test result, and a machine-generated disclosure. Rejecting it finishes the local task and does not call GitHub. A retry reuses the existing draft instead of opening another. There is no merge and no deploy. The main demo leaves these settings unset.

`GET /api/v1/tasks/{id}/publication` returns that draft. It does not include a token.

`finalize` checks the objects again. A task with no repository stores status `completed`, stage `finalize`, the triage summary, and the validated triage object as `result_json`. A planned task uses the plan's problem statement as the summary and keeps the triage, the investigation, and a short plan record in `result_json`. An approved task that policy accepts also stores the decision, the workspace baseline, the patch checksums, and the review checksum, and uses the patch summary as `result_summary`. The plan body, the patch bytes, and the review are artifacts. The investigate events list the citation labels. The plan events list `can_proceed`, the version, and the accepted paths. The workspace events list the base commit and the branch. The develop events list the paths and the two checksums. The review events list the action, the policy, and the repair count. A provider, patch, runner, or review failure stores status `failed`, stage `triage`, `investigate`, `plan`, `prepare_workspace`, `develop`, `test`, or `review`, a short summary, and an error string. A pytest failure is not a completed task: policy repairs it at most twice and then fails the task. Those strings omit model text, diff text, and host paths. Concurrency is 1. A new process can load a paused or finished thread by the same task UUID. Resuming after a completed node does not call the model again. Answering an interrupt does not call it again either, until `develop` makes its call.

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
LLM_PROVIDER=ollama LLM_MODEL=qwen2.5-coder:7b \
  LLM_STAGE_MODELS='triage=ollama:qwen3.5:4b,investigate=ollama:qwen2.5-coder:7b,plan=ollama:qwen3.5:4b,develop=ollama:qwen2.5-coder:7b,review=ollama:qwen2.5-coder:7b,supervisor=ollama:qwen3.5:4b' \
  OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  DATABASE_URL=postgresql://patchpilot:patchpilot_dev@127.0.0.1:5432/patchpilot \
  uv run python scripts/check_workflow.py --once
```

The Compose worker uses those models unless you override them. Read the binding warning below. Wait longer for that poll:

```bash
uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000 --timeout 600
```

## Language model

`generate_structured(prompt, schema, options)` is the model call. `TriageResult` carries classification, severity, a short summary, acceptance criteria, confidence, and `needs_clarification`. `FakeProvider` returns deterministic fixtures. `OllamaProvider` posts to Ollama `/api/chat` with `stream` false and the JSON schema in `format`. Pydantic validates every response. Invalid JSON and timeouts retry at most `LLM_MAX_RETRIES` extra times (0–3, default 2). The next failure raises `LLMResponseError` or `LLMTimeoutError`. Those messages omit the model text.

The worker defaults to local Ollama. `triage`, `plan`, and `supervisor` use `qwen3.5:4b`. `investigate`, `develop`, and `review` use `qwen2.5-coder:7b`. pytest and CI set `LLM_PROVIDER=fake` and leave `LLM_STAGE_MODELS` empty, which keeps those runs offline.

Each stage can name its own provider and model with `LLM_STAGE_MODELS` (`triage=ollama:qwen3.5:4b`). An empty value uses `LLM_PROVIDER` and `LLM_MODEL` for triage, investigate, plan, develop, review, and supervisor. A stage runs only when that model advertises the capability the stage needs. Current stages need `structured_output`. Native tool calling is separate and stays off for Ollama unless `OLLAMA_TOOL_CALLING=true`.

An OpenAI-compatible cloud API is off unless `LLM_CLOUD_ENABLED=true` and a base URL, API key, and capability list are set. The worker is the only service that receives the key. Building the router does not open a cloud client for a stage that still uses `fake` or Ollama. Invalid JSON, timeouts, and HTTP 408, 429, 502, 503, and 504 wait `LLM_RETRY_BACKOFF_SECONDS` between attempts. HTTP 500 and connection failures are not retried. Two invalid outputs for the same stage and model open a circuit, and the next call does not reach the provider.

A task stops when it exceeds its model-call, tool-call, reported-token, runtime, parallelism, or repair budget. Parallelism is one and cannot be raised. Local Ollama calls also take one shared slot, in this process and with a Postgres advisory lock across workers. `GET /api/v1/tasks/{id}/usage` lists each call's stage, provider, model, latency, reported tokens, and outcome. Cost is `unknown` unless `LLM_PRICING_PATH` points at a local JSON file that marks that exact model `verified`. A missing token count stays null. The file is not fetched from the network, and a price that is not in it is not invented.

Offline check:

```bash
uv run python scripts/check_llm.py --provider fake
```

`LLM_TIMEOUT_SECONDS` defaults to 1800 seconds per attempt. A cold local model can use most of that. The ceiling is 1800 seconds, and `LLM_MAX_RETRIES` stays between 0 and 3.

Manual Ollama on the host, using a model you already have:

```bash
LLM_PROVIDER=ollama LLM_MODEL=qwen3.5:4b \
  OLLAMA_BASE_URL=http://127.0.0.1:11434 \
  uv run python scripts/check_llm.py
```

The script prints one schema-valid `TriageResult` as JSON.

### Ollama from Compose on Fedora

The worker service adds the host gateway and calls the per-stage Ollama models above. Set `LLM_PROVIDER=fake` and `LLM_STAGE_MODELS` empty to run the stack offline:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

`host-gateway` is the host address on the Compose network. Podman Compose and Docker Compose both accept that value. When `OLLAMA_BASE_URL` is unset, the worker uses `http://host.docker.internal:11434`. A process on the host uses `http://127.0.0.1:11434`.

Ollama listens on `127.0.0.1:11434` unless `OLLAMA_HOST` says otherwise. Packets that arrive on the bridge address do not reach a loopback socket. Serving the worker means Ollama has to listen on the bridge address, or on every interface.

Keep port 11434 off the LAN and off the public internet. `OLLAMA_HOST=0.0.0.0:11434` accepts connections on every interface. On Fedora, leave 11434 closed in firewalld, or bind Ollama to the Compose bridge address only. PatchPilot sends no credentials to Ollama. Anyone who can open a TCP connection can use the model. Compose does not publish port 11434.

`scripts/bootstrap.sh` sets that `OLLAMA_HOST` for the Ollama service when it starts the stack on Linux or Podman, and it does not open 11434 in firewalld. Docker Desktop can already reach a loopback Ollama through `host.docker.internal`, so the script leaves that bind alone.

## Benchmark

The offline harness runs 15 curated issues through the worker and writes `report.json`, `report.md`, and `report.html`. `--provider fake` uses fixtures only. It does not call Ollama or a cloud API. `DATABASE_URL` must point at a migrated application database. The command creates the LangGraph checkpoint tables itself.

```bash
uv run python -m patchpilot.benchmark --provider fake --database-url "$DATABASE_URL"
```

Reports land in `benchmark-results/`. The scores are regression measurements for this repository. They do not represent industry benchmark quality.

An explicit local comparison uses the same issue texts and skips faults that only the fixtures can force:

```bash
uv run python -m patchpilot.benchmark --provider ollama --database-url "$DATABASE_URL"
```

That command needs Ollama on the configured base URL. It is not part of `uv run pytest`.

## Screenshots

These files are not in the repository yet. Capture them from a live demo and keep the names:

- `docs/screenshots/01-task-list.png` — task list and the create form
- `docs/screenshots/02-waiting-approval.png` — plan waiting for a decision
- `docs/screenshots/03-diff-and-tests.png` — applied diff, pytest report, and review

## Cost limits

A task stops when it exceeds one of these ceilings. The defaults are also the values in `.env.example`.

| Limit | Default | Hard maximum |
| --- | --- | --- |
| Model calls | 24 | 48 |
| Tool calls | 12 | 24 |
| Reported tokens | 100000 | 500000 |
| Runtime | 900 seconds | 3600 seconds |
| Parallelism | 1 | 1 |
| Repairs | 2 | 2 |

Tokens count only when the provider reports them. Cost is `unknown` unless `LLM_PRICING_PATH` points at a local JSON file that marks that exact model `verified`. A price is never invented. Cloud calls stay off unless `LLM_CLOUD_ENABLED=true` and a base URL, API key, and capability list are set. `GET /api/v1/tasks/{id}/usage` reads the stored rows and does not call a model.

## Limitations

- The API listens on localhost and has no authentication. Do not publish port 8000.
- `FakeProvider` makes the automated demo and the benchmark deterministic. Those scores measure this repository's fixtures. They are not an industry benchmark and they are not SWE-bench.
- The runner volume is shared by every job. That is weaker isolation than a fresh mount per task, so worker concurrency stays 1.
- Publication is off unless you set `GITHUB_ENABLED`, a token, an allowlist entry, and `registration.github` for a repository you own. There is no merge and no deploy.
- Checkpoint tables come from `AsyncPostgresSaver.setup()`, not from Alembic.
- A hard link inside a checkout is refused. The operator who registers a repository is still trusted to point `REPOSITORY_ROOT` at the intended tree.
- Host Ollama is optional. Compose leaves the provider on `fake`. Binding Ollama beyond loopback is a separate choice and is described above.

## Threat model

The person running Compose is the administrator. The model is untrusted. Text in a registered repository is untrusted, including comments that tell the model to ignore instructions. A task description is untrusted.

What the system is built to prevent:

- The API and the runner do not receive the model key or the GitHub token. The runner exits if its environment contains a secret, and pytest inherits an allowlist.
- A task cannot name an arbitrary path or Git URL. Read tools reject `..`, absolute paths, symlink escapes, `.git`, credential names, binaries, oversized files, and hard links.
- A patch is applied only after plan approval, only in the private worktree, and only on paths the approved plan still allows. Docker files, CI workflows, dependency manifests, symlinks, hard links, and mode bits other than a normal file are refused. `git apply --check` runs before `git apply`. There is no shell.
- A second approval is required before a task branch is pushed. The client has no merge method. The push refuses `main` and `master`. A repeated decision does not open a second pull request.
- A crashed worker loses its lease. Recovery resumes the checkpoint until `WORKER_MAX_ATTEMPTS`, then the task stays `failed`. Event inserts with the same idempotency key do not replace the first payload.
- Supervisor rules choose the route. A model recommendation that the rules did not allow is ignored.

What this does not claim:

- The localhost API is not an authenticated multi-user service. Anyone who can open `127.0.0.1:8000` can create tasks and record decisions.
- The shared runner volume does not isolate two jobs from each other. Concurrency is 1 so that a second job is not staged at the same time.
- A registered checkout is trusted as the tree you named. Registration is a local command because the API has no authentication.
- Generated Python that the policy accepts still runs under pytest inside the runner. The runner's network, capability, and resource limits are the boundary around that process.
- Checkpoint blobs can hold model text. The API does not serve those tables.

## Tests and lint

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python scripts/check_llm.py
```

GitHub Actions runs Ruff, mypy, and pytest on every push and pull request. The workflow sets `LLM_PROVIDER=fake`, leaves cloud and GitHub disabled, and blanks `GITHUB_TOKEN`. It does not use a cloud API key.

`scripts/check_workflow.py --once` needs `DATABASE_URL` and a migrated database. It triages one task, closes the checkpointer, opens a second one, and reloads that thread. Stderr includes `checkpoint thread_id=<uuid> reloaded=true`. The pytest suite covers that path with `FakeProvider`.

Health and settings tests do not need a running database. The readiness failure test connects to a closed local port and checks that the password is not returned or logged.

Task tests start a disposable PostgreSQL 16 container with `podman` or `docker`, apply `alembic upgrade head`, and talk to that database. The Compose volume is left alone. The example password in that container is `patchpilot_dev`.
