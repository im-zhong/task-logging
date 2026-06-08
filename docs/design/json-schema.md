# Where the JSON keys come from

> The `JsonFormatter` emits keys like `created`, `levelname`, `name`,
> `message`, `pathname`, `funcName`, `lineno`, `process`, `thread`,
> `threadName`, `module`, `exc_info`, plus our own `service`, `env`,
> `hostname`, `task_id`, and any extras. Where do those names come from?
> Is there a standard?

Short answer: **the keys mirror stdlib `LogRecord` attribute names
exactly**, plus a small handful of fields we invented that don't exist
on a `LogRecord` (`service`, `env`, `hostname`, `task_id`, plus your
`task_context` extras). The stdlib reference is *also* the JSON schema
reference:

- https://docs.python.org/3/library/logging.html#logrecord-attributes

This note records why we made that choice, the per-key origin tables,
and the stability promise.

## The pipeline

There are three "sources" feeding the JSON payload:

```
LogRecord (built by stdlib logging, ~25 attrs on record.__dict__)
    │
    ▼
TaskContextFilter  →  stamps onto record.__dict__:
    │                   service, env, hostname, task_id,
    │                   + every key from task_context(**extra)
    ▼
JsonFormatter      →  emits record.__dict__ verbatim,
    │                 minus a small drop-list (_DROPPED_LOGRECORD_ATTRS),
    │                 plus a computed `message` and rendered `exc_info`
    ▼
JSON payload {created, levelname, name, message, ...}
```

The formatter is a **negative filter, not a positive enumeration**:
everything on `record.__dict__` is emitted unless explicitly dropped.
That's the answer to "do we have to list every attribute we want?" — no,
we list the few we *don't* want. Adding a new field anywhere upstream
(stdlib gaining a new attribute, a filter stamping a new key, a user
binding a new one via `task_context`) shows up in the JSON
automatically.

Every key in the JSON falls into one of four groups below.

## Group 1: stdlib `LogRecord` attributes, kept verbatim

| JSON key | stdlib `LogRecord` attribute |
|---|---|
| `created` | `record.created` (Unix timestamp as float) |
| `levelname` | `record.levelname` (`"INFO"`, `"ERROR"`, …) |
| `name` | `record.name` (the logger name) |
| `message` | `record.getMessage()` (the formatted message) |
| `process` | `record.process` (PID) |
| `thread` | `record.thread` (thread id) |
| `threadName` | `record.threadName` |
| `module` | `record.module` |
| `funcName` | `record.funcName` |
| `pathname` | `record.pathname` (full file path) |
| `lineno` | `record.lineno` |
| `exc_info` | rendered from `record.exc_info` (see Group 3) |

No renames. If you've ever read [the LogRecord
docs](https://docs.python.org/3/library/logging.html#logrecord-attributes),
you already know the schema. The JSON spelling for `funcName` is
`funcName` (camelCase), not `func_name` (snake_case), because that's
what stdlib calls it — picking a different spelling would just create a
parallel naming convention readers have to memorise.

## Group 2: added by `TaskContextFilter`

These don't exist on a stock `LogRecord`. The filter writes them onto
the record, and the formatter reads them back off:

| JSON key | Source | Why this name |
|---|---|---|
| `service` | the `service=` arg to `setup_logging` | Standard term in microservice telemetry (Datadog, OTel, Grafana docs all use it). |
| `env` | the `env=` arg | Standard in deployment contexts ("prod", "staging", "dev"). |
| `hostname` | `socket.gethostname()` | Self-explanatory. There's no `record.hostname` in stdlib — it's our concept. |
| `task_id` | `contextvars` lookup via `get_task_id()` | The library's own concept. Kept "task_id" because that's what the rest of the API (`task_context(task_id=...)`, `get_task_id()`) calls it. |

Note we deliberately do NOT add a `pid` field. Stdlib already populates
`record.process`, which becomes the JSON `process` key — duplicating it
under a second name would contradict "JSON keys mirror LogRecord."

## Group 3: rendered by the formatter itself

| JSON key | What it is |
|---|---|
| `exc_info` | A nested object built by `_render_exc_info()` from `record.exc_info`. Always present — `null` if there was no exception, or `{name, details, stack_trace, locals_dict}` if there was. The KEY name (`exc_info`) matches the stdlib LogRecord attribute it derives from; only the *value shape* is ours. |

## Group 4: open-ended user extras

| JSON key | Source |
|---|---|
| `user_id`, `request_id`, `region`, … | Whatever you passed to `task_context(**extra)` or `static_fields=` in `setup_logging`. The formatter walks `record.__dict__` and emits anything that isn't on the drop-list (Group 5). |

These are user-defined; we don't control their casing.

## Group 5: `_DROPPED_LOGRECORD_ATTRS` — the negative filter

This is the actual implementation primitive: the formatter emits every
attribute on `record.__dict__` *except* the names in this set. Listing
the few we want to suppress (rather than the many we want to keep) is
much shorter and means new stdlib attributes / new context keys are
emitted automatically without code changes.

| Dropped | Why |
|---|---|
| `record.args`, `record.msg` (raw) | Already substituted into `message` via `getMessage()`. Keeping the format string + args in JSON would be redundant and inflate line size. |
| `record.msecs`, `record.relativeCreated`, `record.asctime` | All redundant with the float `created` (`stage.timestamp` parses it to whatever Loki wants). |
| `record.levelno` | Redundant with `levelname`. Filtering by `levelname=ERROR` is sufficient; few queries need the integer. |
| `record.exc_text`, `record.stack_info` | Already encoded inside `exc_info.stack_trace`. |
| `record.processName` | Almost always `"MainProcess"`. Useless noise. |
| `record.taskName` (Python 3.12+) | Asyncio task name, conflicts with our `task_id` semantically (different concept) — emitting both would be confusing. |
| `record.filename` | Redundant with `pathname`. `filename` is just the basename. |

The curation optimises for "useful in Loki, queryable in LogQL, doesn't
waste bytes" — everything redundant or noisy got dropped.

## Why mirror stdlib names?

We previously renamed several keys (`level`, `logger`, `msg`, `func`,
`file`, `line`, `ts`, `thread_name`, …) for "JSON niceness." That was
backed out, for these reasons:

1. **Two naming conventions, one library.** Anyone reading the source
   sees `record.levelname` / `record.funcName`; anyone reading the JSON
   saw `level` / `func`. People had to learn the mapping. Now there is
   no mapping.

2. **Stdlib is the spec.** Python's `LogRecord` documentation page is
   stable, comprehensive, and not going anywhere. Every Python
   programmer either already knows it or knows where to find it. Using
   those names for free buys us a real spec without writing one.

3. **No "JSON niceness" payoff.** The JSON consumer (Alloy / Grafana /
   `jq` / a future Splunk sidecar) doesn't care about case style or
   abbreviations — it just wants stable identifiers. Renaming added zero
   value to those tools and added cognitive load to humans.

4. **Easier to extend.** If we want to add another stdlib field later
   (e.g. `record.asctime` if our timestamp story changes), we don't have
   to invent a name; stdlib already named it.

5. **Loki labels are still ergonomic.** Query syntax like
   `{level="ERROR"}` is unaffected — Alloy's `stage.labels` lets us name
   the *Loki label* whatever we like (we keep it as `level`), independent
   of the JSON field it pulled from (`levelname`). See the Alloy config
   in the README.

The cost was minor — a small change in the formatter and the tests, and
this design note flipping its narrative — and the result is a smaller
mental footprint for everyone who reads logs.

## Loose alignment with OpenTelemetry

We don't conform to OpenTelemetry's log data model — we deliberately
prefer stdlib names where they exist — but the concepts overlap:

| Our key | OTel equivalent |
|---|---|
| `created` | `Timestamp` |
| `levelname` | `SeverityText` |
| `name` | `InstrumentationScope.Name` (loosely) |
| `message` | `Body` |
| `service` | `Resource.service.name` |
| `hostname` | `Resource.host.name` |
| `process` | `Resource.process.pid` |
| `task_id` | (would be a custom Attribute) |
| `exc_info.name` / `exc_info.stack_trace` | `Attributes["exception.type"]` / `Attributes["exception.stacktrace"]` |

We chose stdlib over OTel because nobody is consuming this as OTel —
Alloy reads it as JSON, LogQL queries it as JSON, and the audience for
the schema is humans writing LogQL, not OTel collectors. If/when that
changes, an OTel sidecar/exporter is one config block away.

## Stability promise

Three things govern whether the schema can change:

1. **Top-level key names are stable.** The Alloy config in the README
   references `levelname`, `created`, `task_id` by exact name. Renaming
   any of them is a breaking change for every Alloy/LogQL config in the
   wild.
2. **Names match stdlib `LogRecord` exactly** for any field that has a
   stdlib equivalent. New keys we add can't shadow stdlib names with a
   different meaning.
3. **Adding keys is safe; removing or renaming is a major bump.**
   Append-only. Old log lines are queried for as long as your retention
   is, and consumers shouldn't break when they encounter pre-rename
   records.

The matching code-level guard is in `formatters.py`:

```python
# Top-level keys are kept STABLE — Alloy's stage.json config in the
# README references them by name. Renaming a key here is a breaking
# change for every Alloy config in the wild. Add new keys freely;
# don't rename or remove existing ones without a major bump.
```

## TL;DR

- Keys mirror stdlib `LogRecord` attribute names exactly, no renames.
- Reference: https://docs.python.org/3/library/logging.html#logrecord-attributes
- We add four fields that don't exist on a `LogRecord`: `service`,
  `env`, `hostname`, `task_id`. Plus your `task_context` extras.
- We drop redundant stdlib fields (`msecs`, `levelno`, `relativeCreated`,
  `processName`, `filename`, raw `msg`/`args`, `exc_text`, `stack_info`,
  `taskName`).
- Top-level keys are public API. Rename = major version bump. Add = free.
- Loki *label* names (set by Alloy) are independent of JSON *field*
  names — we promote `levelname` to a label called `level` for query
  ergonomics.
