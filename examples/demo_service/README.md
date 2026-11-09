# Demo service

A small FastAPI checkout with one deliberate defect. PatchPilot registers this directory and reads it. Nothing here is real customer code.

`GET /discount?customer_tier=standard` returns `{"customer_tier":"standard","percent":0}`. `silver` is 10 and `gold` is 20. A missing `customer_tier` is HTTP 422.

`GET /discount?customer_tier=platinum` is the defect. `discount_percent` indexes `KNOWN_TIERS` and raises `KeyError`. The app does not handle that, so the response is HTTP 500. The intended response is HTTP 422 with a validation error, and a regression test should cover it.

## Baseline

From the PatchPilot repository root:

```bash
uv run pytest examples/demo_service/tests/test_baseline.py
```

That file passes on this checkout.

## Known failure

```bash
uv run pytest examples/demo_service/tests/test_unknown_tier.py
```

That file expects HTTP 422 and fails while the defect is still present. Do not "fix" it in this checkout. A later PatchPilot task proposes the change in an isolated worktree.

## Git checkout

Registration requires a Git checkout. Create one without changing the parent repository:

```bash
uv run python scripts/init_demo_repo.py
```

The script is idempotent. `examples/demo_service/.git/` is ignored by the parent repository. Do not `git add` this directory while that `.git` directory exists. Git would record a gitlink, and a clone of the parent repository would not contain these source files. From the PatchPilot root, `./scripts/add_all.sh` removes the nested `.git` directories, runs `git add --all`, and recreates the checkouts. It does not commit.
