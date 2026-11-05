# ADR 0005: A restricted runner executes tests

Status: accepted

## Context

A patch has to be executed to know whether the tests pass. Running pytest inside the API or the worker would give generated code the database password, the model settings, and the worker's network.

## Decision

The worker copies the private worktree into a job directory and writes a request that contains only a job id. A separate `runner` service runs one fixed pytest command. Compose starts it as uid 1000 with `network_mode: none`, a read-only root, dropped capabilities, `no-new-privileges`, and CPU, memory, PID, and time limits. The runner refuses to start when a secret is in its environment. The model cannot choose the executable or the arguments.

## Consequences

A passing, failing, or timed-out suite is a test-report artifact. The shared runner volume is one tree for every job, which is weaker than a fresh mount per task, so worker concurrency stays 1. The runner does not receive the database URL, a model key, or the GitHub token.
