# Five-minute demo

This walkthrough uses the deterministic `fake` provider. It does not call Ollama or a cloud API. The sample project is `examples/demo_service`. An unknown customer tier still returns HTTP 500. The intended response is HTTP 422.

The Compose worker otherwise uses local Ollama. For this walkthrough set `LLM_PROVIDER=fake`, `LLM_STAGE_MODELS` empty, `LLM_CLOUD_ENABLED=false`, and `GITHUB_ENABLED=false`.

## 0:00 — Start the stack

```bash
docker compose up --build -d
docker compose ps
curl -s http://127.0.0.1:8000/health/ready
```

Readiness is `{"status":"ok","database":"ready"}` when PostgreSQL accepts `SELECT 1`. The dashboard is <http://127.0.0.1:8000/>.

On a machine without the Docker CLI, `podman-compose up --build -d` reads the same `compose.yaml`.

## 1:00 — Register the demo checkout

```bash
uv run python scripts/init_demo_repo.py
docker compose exec worker python -m patchpilot.source register \
  --name "Demo service" \
  --path demo_service
docker compose exec worker python -m patchpilot.source list
```

`init_demo_repo.py` creates `examples/demo_service/.git/`. Do not `git add` that directory into the parent repository while the nested `.git` exists. The worker sees the checkout at `/repositories/demo_service`, read-only.

## 2:00 — Submit the sample issue

Open the dashboard. Choose **Demo service** and submit:

Title: `Unknown customer tier crashes discount endpoint`

Description:

```text
When the demo-service discount endpoint receives a customer tier it does not recognize, it returns HTTP 500. It should respond with HTTP 422 and a clear validation error. Find the cause, propose a minimal fix and add a regression test.
```

The page polls until the status is **Waiting for approval**. The timeline shows triage, investigation, the plan, and a route event for each supervisor choice. Nothing has been written to `examples/demo_service`.

The same create is:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"title":"Unknown customer tier crashes discount endpoint","description":"When the demo-service discount endpoint receives a customer tier it does not recognize, it returns HTTP 500. It should respond with HTTP 422 and a clear validation error. Find the cause, propose a minimal fix and add a regression test.","repo_id":"<id from source list>"}'
```

## 3:30 — Approve the plan

On the task page, approve the plan. Rejecting it ends the task as `rejected` and does not create a worktree.

After approval the worker clones the checkout into the `workspaces` volume, applies a patch on `patchpilot/<task-id>`, and waits for the runner. The runner executes the fixed pytest command with no network.

## 4:30 — Read the result

The task page shows the citations, the plan, the applied diff, the pytest report, the review, and model usage. With the fake provider the diff adds an unknown-tier error and a regression test in the private worktree. `examples/demo_service` is unchanged. Usage cost is `unknown` unless you configured a verified local price file.

Confirm the source checkout did not move:

```bash
git -C examples/demo_service status --short
git -C examples/demo_service rev-parse HEAD
```

Both should match the commit printed when you initialized the demo repository.

## If you only have a minute

Skip the dashboard and run the offline smoke test. It does not need a registered repository:

```bash
uv run python scripts/check_workflow.py --api-url http://127.0.0.1:8000
```

That submits a triage-only task. The worker stores a completed `TriageResult`. It does not investigate, plan, or patch.

## Screenshots

Capture these during a live run and commit them beside the names in the README:

- `docs/screenshots/01-task-list.png`
- `docs/screenshots/02-waiting-approval.png`
- `docs/screenshots/03-diff-and-tests.png`
