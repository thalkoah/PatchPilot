# ADR 0003: PostgreSQL checkpoints, separate from business rows

Status: accepted

## Context

A paused approval has to survive a worker restart. LangGraph can store that state itself. The task list, the audit trail, and the approval decision also have to be queryable without reading graph internals.

## Decision

The worker compiles the graph with `AsyncPostgresSaver` from `langgraph-checkpoint-postgres`. The thread id is the task UUID. `AsyncPostgresSaver.setup()` creates the checkpoint tables. Alembic does not migrate them. The `tasks`, `events`, `approvals`, and artifact rows remain the business record.

## Consequences

A new process can load a paused or finished thread. Checkpoint blobs can contain model text that the API does not return. Operators initialize checkpoints with `uv run python -m patchpilot.checkpoints`, or by starting the worker, which calls the same setup.
