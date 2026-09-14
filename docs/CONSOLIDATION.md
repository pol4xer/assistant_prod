# Consolidation notes

This repository brings two iterations of the scheduling project into one
portfolio example: the newer assistant dashboard/runtime and selected behavior
from the earlier Scheduler experiment.

## Chosen baseline

The newer assistant implementation supplies the active FastAPI application,
SQLite workflow state, English dashboard, scheduling logic, local scenarios, and
Google/OpenAI adapter code. It is the code path used by `make run` and the tests.

The earlier Scheduler explored a different infrastructure direction using
PostgreSQL, Redis, and an outbox model. Its worker wiring was incomplete. Those
components are not included as runtime dependencies, and this repository does
not claim their delivery guarantees or scalability.

## Behavior carried forward

The useful recipient-filtering rule is retained: a new Gmail thread must name
the configured assistant address in To or Cc before it can start an assistant
workflow. Replies on an existing request remain associated with that thread.
This sits alongside duplicate-message detection and scheduling relevance checks
in the current messaging module.

The default execution path is now a repeatable offline demonstration. Fictional
contacts drive the same request lifecycle and scheduler as the application,
while external email delivery, OAuth, calendar calls, and OpenAI requests are
blocked. Local messages are explicitly labeled as unsent.

## Resulting scope

There is one local dashboard and one workspace. SQLite records state and
outbound delivery status; it is not backed by a durable worker/outbox system.
Provider adapters remain inspectable experiments, and the offline tests establish
only the behavior they exercise.

The consolidation keeps implemented scheduling behavior visible without carrying
forward unconnected infrastructure, scale targets, or placeholder provider
features as product claims.
