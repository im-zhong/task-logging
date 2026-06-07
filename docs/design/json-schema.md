# Where the JSON keys come from

> The `JsonFormatter` docstring lists keys like `ts`, `level`, `logger`,
> `msg`, `service`, `task_id`, `func`, `file`, `line`, `exc`, … Where do
> those names come from? Is there a standard?

Short answer: there's no standard the formatter is conforming to. The keys
were **picked**, with a loose lean toward the OpenTelemetry log data model.
This note records exactly where each key came from, what was renamed, what
was dropped, and why — so future contributors and curious users have
something to consult before proposing schema changes.

## The pipeline

There are three "sources" feeding the JSON payload:

```
LogRecord (built by stdlib logging, ~25 attrs)
    │
    ▼
TaskContextFilter  →  adds: service, env, hostname, pid, task_id,
    │                       *task_context extras*
    ▼
JsonFormatter      →  picks which to keep, what to call them, what to drop
    │
    ▼
JSON payload {ts, level, logger, msg, service, ...}
```

Every key in the JSON falls into one of six groups below.

## Group 1: renamed from stdlib `LogRecord`

stdlib gives us attributes with names that are slightly awkward in JSON
(camelCase mixed with weird abbreviations). Several were renamed:

| JSON key | stdlib `LogRecord` attribute | Why renamed |
|---|---|---|
| `ts` | `record.created` (a `time.time()` float) | "ts" is the de-facto name for log timestamps in Grafana / Loki / OTel / Datadog. We also convert the float into an ISO-8601 string with the formatter; it's no longer just the raw `created` value. |
| `level` | `record.levelname` | Drop the redundant "name" suffix. Everyone calls this "level." |
| `logger` | `record.name` | "name" alone is ambiguous (whose name?). "logger" makes it self-describing. |
| `msg` | `record.getMessage()` (NOT `record.msg`) | `record.msg` is the *format string*; `getMessage()` is the formatted result. We emit the formatted result and call it `msg` because that's the colloquial term. |
| `func` | `record.funcName` | Shorter, equally clear. |
| `file` | `record.pathname` | "pathname" is jargon; "file" is what humans say. |
| `line` | `record.lineno` | Strip the redundant "no." |
| `thread_name` | `record.threadName` | Snake_case-ify camelCase to match the rest of the JSON. |

## Group 2: copied verbatim from `LogRecord`

| JSON key | stdlib attribute | Why kept |
|---|---|---|
| `thread` | `record.thread` | Thread *id* (an int). Already short and common. |
| `module` | `record.module` | Already a fine name. |

## Group 3: added by `TaskContextFilter`

These don't exist on a stock `LogRecord`. The filter writes them on, and
the formatter reads them back off:

| JSON key | Source | Why this name |
|---|---|---|
| `service` | the `service=` arg to `setup_logging` | "service" is the standard term in microservice telemetry (Datadog, OTel, Grafana docs all use it). |
| `env` | the `env=` arg | Standard in deployment contexts ("prod", "staging", "dev"). |
| `hostname` | `socket.gethostname()` | Self-explanatory. |
| `pid` | `os.getpid()` | "pid" is universal; "process_id" felt verbose. |
| `task_id` | `contextvars` lookup via `get_task_id()` | This is the library's own concept. Kept "task_id" because that's what the rest of the API (`task_context(task_id=...)`, `get_task_id()`) calls it. |

## Group 4: rendered by the formatter itself

| JSON key | What it is |
|---|---|
| `exc` | A nested object built by `_render_exc_info()` from `record.exc_info`. Always present — `null` if there was no exception, or `{name, details, stack_trace, locals_dict}` if there was. |

## Group 5: open-ended user extras

| JSON key | Source |
|---|---|
| `user_id`, `request_id`, `region`, … | Whatever you passed to `task_context(**extra)` or `static_fields=` in `setup_logging`. The formatter walks `record.__dict__` and emits anything that isn't a built-in `LogRecord` attribute. |

That's why the docstring lists them as `...: any extra fields you bound`.

## Group 6: stdlib attributes deliberately *dropped*

Worth being explicit about what's NOT in the payload, because the ones we
kept were a curation, not a copy:

| Dropped | Why |
|---|---|
| `record.args`, `record.msg` (raw) | Already substituted into `msg` via `getMessage()`. Keeping the format string + args in JSON would be redundant and inflate line size. |
| `record.created` (raw float), `record.msecs`, `record.relativeCreated`, `record.asctime` | All redundant with the ISO `ts`. |
| `record.levelno` | Redundant with `levelname`. Filtering by `level=ERROR` is sufficient; few queries need the integer. |
| `record.exc_text`, `record.stack_info` | Already encoded inside `exc.stack_trace`. |
| `record.processName` | Almost always `"MainProcess"`. Useless noise. |
| `record.taskName` (Python 3.12+) | Asyncio task name, conflicts with our `task_id` semantically (different concept) — emitting both would be confusing. |
| `record.filename` | Redundant with `pathname` (which we kept as `file`). `filename` is just the basename. |

The curation optimises for "useful in Loki, queryable in LogQL, doesn't
waste bytes" — everything redundant got dropped.

## What we're loosely following

The schema isn't strictly conforming to any one specification, but it
leans toward **OpenTelemetry's log data model**, which has converged
enough that most modern logging tools recognise it:

| Our key | OTel equivalent |
|---|---|
| `ts` | `Timestamp` |
| `level` | `SeverityText` |
| `logger` | `InstrumentationScope.Name` (loosely) |
| `msg` | `Body` |
| `service` | `Resource.service.name` |
| `hostname` | `Resource.host.name` |
| `pid` | `Resource.process.pid` |
| `task_id` | (would be a custom Attribute) |
| `exc.name` / `exc.stack_trace` | `Attributes["exception.type"]` / `Attributes["exception.stacktrace"]` |

### Where we deliberately diverge from OTel

If we were being maximally OTel-compatible, we'd flatten `exc.*` to
dot-keyed attributes (`exception.type`, `exception.stacktrace`) and use
`service.name` etc. with dots. We kept it flat-ish and human-friendly
because:

1. Nobody's actually consuming this as OTel — Alloy reads it as JSON,
   LogQL queries it as JSON.
2. Dotted keys in JSON make `| json` extraction in LogQL more awkward
   (you have to write `service\.name`).
3. The schema's audience is humans writing LogQL, not OTel collectors.

That's why you see `service` (not `service.name`), `pid` (not
`process.pid`), and `exc` as a nested object (not flattened).

## Stability promise

Three things govern whether the schema can change:

1. **Top-level key names are stable.** The Alloy config in the README
   references `level`, `ts`, `task_id` by exact name. Renaming any of
   them is a breaking change for every Alloy/LogQL config in the wild.
2. **Casing is consistent.** Everything is `snake_case` (`task_id`,
   `thread_name`). New keys must follow.
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

## Variations we considered (and rejected)

| Change | Pro | Con |
|---|---|---|
| `ts` → `timestamp` | More explicit | 4 extra bytes on every line, multiplied by billions of log lines |
| `msg` → `message` | More explicit | Same as above; `msg` is also what stdlib calls it internally |
| `task_id` → `trace_id` | Aligns with OTel/distributed tracing terminology | Conflates two concepts — a "task" in your system might span multiple OTel traces, or a single trace might cover work that's not a "task" |
| Flatten `exc` to `exc_name`, `exc_details`, `exc_stack`, `exc_locals` | Easier to query in LogQL (no nested-key syntax) | Bigger lines, less obviously grouped, harder to add new exc fields later |
| Add `level_no` as int | Cheap range queries (`level_no >= 40`) in LogQL | Redundant with `level`; LogQL `=~ "ERROR\|CRITICAL"` works fine |

## TL;DR

- The keys were chosen, not standardised. There's no JSON-log spec the
  formatter conforms to.
- Mappings: see Groups 1–4 above. Drops: see Group 6.
- Loose alignment with OpenTelemetry log data model, but flattened where
  OTel's dotted keys would make LogQL awkward.
- Top-level keys are public API. Rename = major version bump. Add = free.

If you want to propose a schema change, open an issue with:
- the new/renamed key
- which Group above it falls into
- a concrete LogQL or Alloy use case that the current schema makes
  awkward
