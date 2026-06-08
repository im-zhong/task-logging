# Where the JSON keys come from

> The `JsonFormatter` emits keys like `created`, `levelname`, `name`,
> `message`, `pathname`, `funcName`, `lineno`, `process`, `thread`,
> `threadName`, `module`, `exc_info`, plus whatever attrs the user
> supplied via `global_log_attrs` and `task_log_context`. Where do those
> names come from? Is there a standard?

Short answer: **the keys mirror stdlib `LogRecord` attribute names
exactly**. The library itself does not invent any field — `service`,
`env`, `task_id` and friends are all conventions the user picks. The
stdlib reference is *also* the JSON schema reference:

- https://docs.python.org/3/library/logging.html#logrecord-attributes

This note records why we made that choice, the per-key origin tables,
and the stability promise.

## The pipeline

There are three "sources" feeding the JSON payload:

```
LogRecord (built by stdlib logging, ~25 attrs on record.__dict__)
    │
    ▼
TaskLogFilter  →  stamps onto record.__dict__:
    │                   global_log_attrs (from TaskLogFilter(...))
    │                 + every key from the active task_log_context
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
binding a new one via `task_log_context`) shows up in the JSON
automatically.

Every key in the JSON falls into one of the four groups below.

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

Note we deliberately do NOT add a `pid` field. Stdlib already populates
`record.process`, which becomes the JSON `process` key — duplicating it
under a second name would contradict "JSON keys mirror LogRecord."

## Group 2: rendered by the formatter itself

| JSON key | What it is |
|---|---|
| `exc_info` | A nested object built by `_render_exc_info()` from `record.exc_info`. Always present — `null` if there was no exception, or `{name, details, stack_trace, locals_dict}` if there was. The KEY name (`exc_info`) matches the stdlib LogRecord attribute it derives from; only the *value shape* is ours. |

## Group 3: user-supplied attrs

Anything in `TaskLogFilter(global_log_attrs={...})` or
`task_log_context({...})` rides through to the JSON. The library does
not name any field — `service`, `env`, `task_id`, `user_id`,
`request_id`, `region`, `hostname` are all conventions you pick, not
names the library bakes in.

| JSON key | Source |
|---|---|
| `service`, `env`, `region`, `hostname`, ... | Process-wide via `TaskLogFilter(global_log_attrs={...})` (typically picked at app startup). |
| `task_id`, `request_id`, `user_id`, ... | Per-context via `task_log_context({...})` (typically picked per request). |

Inner `task_log_context` overrides outer, and `task_log_context` overrides
`global_log_attrs`. There is no protection against user keys colliding
with stdlib `LogRecord` attribute names — if you bind
`task_log_context({"name": "X"})`, `record.name` becomes "X". See
[task-context.md](task-context.md) "Why we don't protect stdlib field
names from being overwritten" for the rationale.

## Group 4: `_DROPPED_LOGRECORD_ATTRS` — the negative filter

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

## Why `message` and `exc_info` are special

If the formatter is "emit `record.__dict__` minus a drop-list," why does
`format()` have explicit lines for `message` and `exc_info`? They look
like noise — until you notice they're there for **two completely
different reasons**, and removing either special would break something.

### `message`: the value isn't on the record yet

`record.__dict__` contains `record.msg` (the format string `"hello %s"`)
and `record.args` (the tuple `("alice",)`), but **not** `record.message`
(the formatted result `"hello alice"`).

Why? Because stdlib delays the `%` substitution until something asks
for it. `LogRecord.getMessage()` is what does the work:

```python
def getMessage(self):
    msg = str(self.msg)
    if self.args:
        msg = msg % self.args
    return msg
```

This is the lazy-formatting optimisation discussed in the
[stdlib primer](stdlib-logging-primer.md): if a record is filtered out
before any handler emits it, we never paid the formatting cost. Stdlib's
own `Formatter.format()` calls `record.message = self.formatMessage(record)`
at the very start to make the formatted text available — and we have to
do the same thing because we're a custom Formatter.

So the line

```python
payload["message"] = record.getMessage()
```

is what *creates* the `message` key. It's not a special case in the
"this attribute needs custom handling" sense; it's a bridge between
stdlib's lazy-formatting protocol and our format-and-emit pipeline. We
then drop `msg` and `args` from the output (via
`_DROPPED_LOGRECORD_ATTRS`) because they're already encoded inside
`message` and keeping them would just bloat each line with the
un-substituted form.

### `exc_info`: the value isn't JSON-serialisable

`record.exc_info` on a `LogRecord` is the raw 3-tuple from `sys.exc_info()`:

```python
(<class 'ZeroDivisionError'>, ZeroDivisionError('division by zero'), <traceback object at 0x7f...>)
```

Three values, **none of them JSON-serialisable**:

| Element | Type | Why JSON can't handle it |
|---|---|---|
| `exc_type` | `type` (the exception class) | Class objects aren't a JSON type |
| `exc_value` | `BaseException` instance | Arbitrary Python object |
| `exc_tb` | `TracebackType` | Linked list of frames; frames hold code objects, locals dicts of arbitrary objects, etc. |

If we let it fall through the comprehension, `json.dumps` would hit
our `_json_default` fallback, which calls `repr()`, producing:

```
"(<class 'ZeroDivisionError'>, ZeroDivisionError('division by zero'), <traceback object at 0x7f...>)"
```

Technically valid JSON, but **all the actual debugging value is gone**
— no formatted stack trace, no locals at the throw site, no programmatic
access to the exception type. We'd have failed at our one job.

So `exc_info` gets rendered into a JSON-friendly structure:

```json
{
  "name": "ZeroDivisionError",
  "details": "division by zero",
  "stack_trace": "Traceback (most recent call last):\n  File ...",
  "locals_dict": {"a": "1", "b": "0"}
}
```

We keep the *key name* `exc_info` (matching stdlib), and only replace
the *value shape*. That's why the drop-list contains `"exc_info"` — to
suppress the raw tuple — and we then assign our rendered version under
the same key.

### Alternatives we rejected

Could we eliminate the specials entirely? Yes, in two ways, both worse:

**Option A — render at filter time, not formatter time.** Have
`TaskLogFilter` write `record.message` and a rendered
`record.exc_info` *onto the record*, so by the time the formatter runs,
`record.__dict__` already has the JSON-friendly values. The
comprehension would then be a true one-liner with no specials.

We don't do this because:

- Filters become coupled to the *output format*'s serialisation
  constraints. Today the filter only stamps fields that are already
  JSON-friendly (strings/ints/None) and is format-agnostic.
- We'd lose the lazy-formatting optimisation. `record.message` would be
  computed on every record that passes through the filter, even ones a
  downstream handler-filter or level threshold would have dropped.
- It muddles the separation of concerns: filters enrich, formatters
  serialise.

**Option B — pattern-match the tuple shape in `_json_default`.** Have
the `default=` callback recognise `(type, exc, tb)` shapes and render
them, so the comprehension can pass `exc_info` through unchanged.

We don't do this because:

- Pattern-matching a tuple shape in a generic callback is fragile —
  what if user code binds an unrelated 3-tuple via `task_log_context`?
- The transformation is *structural* (one tuple → one nested object
  with four keys), not just *encoding* (which is what `default=` is for).
- It buries an important design decision in a fallback path most
  readers won't read.

The current shape — drop the raw value via the negative filter, write
the rendered shape under the same key — is explicit about what's
happening, and the cost is two extra blocks of code.

### TL;DR

| Field | Why special | Could it not be special? |
|---|---|---|
| `message` | The value isn't on `record.__dict__`; it's lazily computed via `getMessage()` | Only by giving up lazy formatting (worse) |
| `exc_info` | The value (a `(type, exc, tb)` tuple) isn't JSON-serialisable | Only by rendering it earlier in a filter (worse separation of concerns) or via fragile shape-matching in `default=` (worse explicitness) |

Every other field on the record is already JSON-friendly *and* already
on `record.__dict__`, so it sails through the comprehension without
help.

## Why we don't override `formatException`

The Python [logging cookbook][cookbook-customex] has an example of a
custom formatter that **overrides `formatException`** to flatten a
traceback to one line. People who've read that page sometimes ask:
"Shouldn't `JsonFormatter` override `formatException` too — for
consistency?"

[cookbook-customex]: https://docs.python.org/3/howto/logging-cookbook.html#customized-exception-formatting

The answer is no, and the reason is worth recording because it's
exactly the kind of thing that looks like an inconsistency until you
see why.

### What `formatException` is for in stdlib

In stdlib's `Formatter.format()`, the relevant logic is roughly:

```python
def format(self, record):
    record.message = record.getMessage()
    s = self.formatMessage(record)              # the "main" line
    if record.exc_info:
        if not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
    if record.exc_text:
        s = s + "\n" + record.exc_text          # appended to the main line
    if record.stack_info:
        s = s + "\n" + self.formatStack(record.stack_info)
    return s
```

Two things to notice:

1. `formatException` exists because stdlib's `format()` builds **a
   single string** with the traceback **appended after a newline**. The
   hook lets subclasses change *how the appended text looks*.
2. The result is **cached on the record** as `record.exc_text` so
   subsequent handlers don't re-format the same traceback.

The cookbook example uses both: it overrides `formatException` to
flatten the traceback to one line, and uses `record.exc_text` to detect
"an exception was attached" so it can post-process the assembled
string.

### What our formatter does instead

We don't build a string with a traceback appended. We build a JSON
object with a structured `exc_info` field:

```json
"exc_info": {
  "name": "ZeroDivisionError",
  "details": "division by zero",
  "stack_trace": "Traceback ...",
  "locals_dict": {"a": "1", "b": "0"}
}
```

The traceback isn't appended to anything — it's a value at a specific
JSON key. So the question is: does our exception rendering need the
hook stdlib provides? Three reasons it doesn't:

#### 1. We never call `super().format()` or `super().formatException()`

The cookbook example does `s = super().format(record)` and
`super().formatException(exc_info)`. Stdlib's machinery runs, builds
the appended-traceback string, caches `exc_text`, and they post-process
it.

We bypass that entirely. Our `format()` builds a dict from
`record.__dict__`, computes `message`, renders our own `exc_info`
object, and `json.dumps`. **The base-class string-assembly path never
runs**, so the hook it offers (`formatException`) has no execution path
that would call it. Overriding it would be defining a method that
nothing invokes.

#### 2. Our equivalent is `_render_exc_info`, and overriding *that* makes more sense

We *do* have a method whose job is "turn `record.exc_info` into our
preferred shape" — it's just called `_render_exc_info`. A future
subclass that wants to customise exception rendering (drop locals,
anonymise paths, redact secrets in stack traces, …) should override
`_render_exc_info`, not `formatException`. The naming reflects what it
actually does:

| Method | Returns | Where it's called |
|---|---|---|
| `Formatter.formatException` | a `str` (multi-line traceback) | inside stdlib's string-assembly `format()` |
| `JsonFormatter._render_exc_info` | a `dict` (structured exception object) | inside our JSON-assembly `format()` |

If we *did* override `formatException`, it would have to return a `str`
(the LSP contract for the base class). But our exception rendering
returns a `dict`. So overriding it would either lie about the return
type or be a useless wrapper that flattens our dict back to a string
nobody asks for.

#### 3. The cached `record.exc_text` would create a subtle bug

Stdlib's `formatException` populates `record.exc_text` as a side effect,
"for caching." If a record then propagates to *another* handler whose
formatter is a stdlib-style text formatter, that second formatter sees
a non-empty `exc_text` and skips its own `formatException` call — using
whatever string our override returned.

That's fine for the cookbook example (they're the only handler and own
the format end-to-end). For us, if a host application has both a
`JsonFormatter` handler and a stock `logging.StreamHandler` with a
stdlib formatter, an override would silently corrupt the second
handler's output.

The current design sidesteps this entirely: we never touch
`record.exc_text`, so other handlers' formatters render exceptions
normally.

### The pattern at the abstract level

stdlib `Formatter` has three subclass-overridable points:

```
format(record)
  ├─ formatMessage(record)      ← override to change the main line shape
  ├─ formatException(exc_info)  ← override to change the appended traceback
  └─ formatStack(stack_info)    ← override to change appended stack info
```

These three exist because stdlib's `format()` is **a
string-concatenation pipeline with three stages**, and the hooks let
you swap each stage independently.

We replaced the whole pipeline with a **dict-construction pipeline**
that has a different shape:

```
format(record)
  ├─ comprehension over record.__dict__
  ├─ getMessage()
  ├─ _render_exc_info(record.exc_info)     ← our hook
  └─ json.dumps(...)
```

So we have one hook in the analogous position (`_render_exc_info`),
and it returns a `dict` because that's what JSON wants. There's no
`formatStack` analogue because `record.stack_info` is already a string
and rides through the comprehension; no `formatMessage` analogue
because there's no "main line shape" to customise — the JSON shape *is*
the schema.

`formatException` is part of stdlib's *text*-formatting interface.
Implementing it on a *JSON* formatter would be cargo-culting an
abstraction that doesn't apply. The honest analogue is
`_render_exc_info`, and we've got it.

### When *would* we override `formatException`?

If our formatter inherited the stdlib string-assembly path — i.e.
produced text with the traceback appended — and we wanted, say, a
one-line traceback. Or if we explicitly wanted to participate in the
`record.exc_text` caching protocol so other handlers benefit.

Neither applies to JSON output. The cache is a bug surface for us, not
a feature.

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
- The library auto-detects nothing. `service`, `env`, `hostname`, `task_id`
  and friends are all supplied by you via `global_log_attrs` or
  `task_log_context`.
- We drop redundant stdlib fields (`msecs`, `levelno`, `relativeCreated`,
  `processName`, `filename`, raw `msg`/`args`, `exc_text`, `stack_info`,
  `taskName`).
- Top-level keys are public API. Rename = major version bump. Add = free.
- Loki *label* names (set by Alloy) are independent of JSON *field*
  names — we promote `levelname` to a label called `level` for query
  ergonomics.
