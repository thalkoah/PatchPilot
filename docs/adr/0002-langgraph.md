# ADR 0002: LangGraph holds the workflow

Status: accepted

## Context

A task moves through triage, investigation, planning, approval, a private worktree, a patch, tests, and review. Some of those steps pause for a person. A single script would hide the pause and the resume.

## Decision

The workflow is a typed LangGraph `StateGraph`. Each specialist node returns to a supervisor node. Approval uses `interrupt()` so the process can exit and later resume the same thread with `Command(resume=...)`. Nodes that pause do not perform a side effect before the interrupt, because resume runs the node again.

## Consequences

The graph is the place that sequences agents. Business status still lives on the `tasks` row, which the API and the dashboard read. Adding a stage means adding a node and a supervisor rule, not a new service.
