# ADR 0001: A separate worker claims tasks

Status: accepted

## Context

The API accepts bug reports and serves the dashboard. Triage, investigation, planning, patching, and review are slow and call a model. Doing that work inside the request, or in a FastAPI background task, ties it to one process that can be restarted by the HTTP server.

## Decision

A separate worker process polls PostgreSQL, claims one queued task with `FOR UPDATE SKIP LOCKED`, and runs the workflow. The API only inserts and reads rows. The model is not called from a request handler.

## Consequences

The API stays available while a model call is in progress. A crashed worker leaves a lease that a later poll can recover. The worker is the only process that receives model and GitHub credentials. Concurrency stays at one, which matches the shared runner volume.
