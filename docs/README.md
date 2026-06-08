# Task Logging — Documentation

User-facing documentation lives in the top-level [`README.md`](../README.md) — that's where to look for installation, quick start, deployment with Grafana Alloy, and the public API reference.

This `docs/` directory holds **design notes** that explain *why* the library is shaped the way it is. They are written for two audiences:

- contributors who want to change the library without breaking its assumptions
- users who want a deeper mental model than the README provides

## Design notes

| Note | Question it answers |
|---|---|
| [design/decorators.md](design/decorators.md) | Why is there only one `@log_func_call` decorator instead of separate `FunctionLogger` / `ClassFunctionLogger`? Why doesn't it require classes to have a `_logger` attribute? |
| [design/task-context.md](design/task-context.md) | How does `task_log_context` make user-supplied attrs flow through threads, asyncio tasks, and third-party libraries' logs without modifying them? |
| [design/stdlib-logging-primer.md](design/stdlib-logging-primer.md) | A bottom-up tour of stdlib `logging` — `LogRecord`, the logger tree, handlers, filters, formatters — with the rules that prevent the most common pitfalls. |
| [design/why-json-logs.md](design/why-json-logs.md) | Loki accepts arbitrary text; why does this library emit JSON anyway? What do we gain, and what do we trade away? |
| [design/json-schema.md](design/json-schema.md) | Where do the JSON keys (`ts`, `level`, `logger`, `msg`, `service`, `task_id`, …) come from? What was renamed, what was dropped, and what's the stability promise? |
| [design/migrating-from-v0.0.1.md](design/migrating-from-v0.0.1.md) | The old `TaskLogger` class is gone. Where did each of its capabilities go, and why is the new shape better? Includes a code-level migration walkthrough. |

## Archived

- [archive-v0.0.1-postgres-design.md](archive-v0.0.1-postgres-design.md) — the original design notes from when logs were persisted to PostgreSQL via a `TaskLoggingDatabaseInterface`. Kept for historical reference; the library has since pivoted to a stdlib-logging + JSON-file + Grafana-Alloy + Loki pipeline. Nothing in this file describes the current code. If you're porting v0.0.1 code, see [design/migrating-from-v0.0.1.md](design/migrating-from-v0.0.1.md) for the capability mapping.
