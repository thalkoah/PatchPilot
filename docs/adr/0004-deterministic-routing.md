# ADR 0004: Deterministic routing

Status: accepted

## Context

The supervisor chooses the next stage. A model can recommend a route, and a bad recommendation could skip approval, raise a budget, or write a file.

## Decision

Rules in `Supervisor` are the route. `SUPERVISOR_MODEL` defaults to false. When it is on, the recommendation is schema-validated and ignored unless the rules already allowed that route. The model cannot approve a plan, choose a shell command, or change a limit. The graph stops after 24 supervisor decisions, after 900 seconds, or at LangGraph's recursion limit of 40.

## Consequences

The same observations produce the same route under `FakeProvider`. A route event records why the rule chose the next stage. Turning the model on does not move a safety decision into the model.
