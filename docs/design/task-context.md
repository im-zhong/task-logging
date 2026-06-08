# How `task_log_context` works

`task_log_context` is the most important piece of the library and the part whose
mechanics are least obvious. This note explains how it makes a user-supplied
dict of log attrs flow through threads, asyncio tasks, and even third-party
libraries' logs — without modifying anything about those libraries.

(In the examples below we bind `task_id` because it's a familiar name, but the
library doesn't privilege any particular key; pick whatever your domain
calls for.)

## The problem

We want this:

```python
with task_log_context({"task_id": "task-42"}):
    log.info("step 1")               # ← tagged "task-42"
    do_some_work()                   # ← logs in here are tagged
    requests.get("https://...")      # ← urllib3's internal logs are tagged
log.info("after")                    # ← back to no task_id
```

Three things have to be true for this to work:

1. The bound attrs must be **available to every logger in the process** without
   passing them explicitly down through every function call.
2. They must be **automatically restored** when the block exits — including
   when the block exits via an exception.
3. They must be **isolated per request** — concurrent requests in different
   threads or asyncio tasks must not see each other's attrs.

`task_log_context` solves this by combining three stdlib primitives:
`contextvars.ContextVar`, `logging.Filter`, and `logging.LogRecord`.

## Piece 1: `contextvars.ContextVar` — the storage

```python
_local_log_attrs: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "task_logging_local_log_attrs", default=None
)
```

A `ContextVar` is **like a global variable, but each "logical thread of
execution" sees its own copy**. "Logical thread" here means:

- Each OS thread has its own context.
- Each `asyncio.Task` has its own context.
- Each `concurrent.futures` worker has its own context.

So when thread A sets the var to `{"task_id": "A"}` and thread B sets it to
`{"task_id": "B"}`, neither one clobbers the other. That's how the test
`test_context_isolated_across_threads` passes — each thread gets the
`task_id` it bound, no cross-talk.

### Why the default is `None` and not `{}`

A `ContextVar` with a mutable default (like `{}`) is a footgun. Every code
path that never calls `.set()` would share *the same dict object*. One
mutation anywhere — even in a third-party library that imports the var —
would leak everywhere, in every thread, forever. ruff's `B039` rule catches
this exact bug.

So our default is `None`, and we allocate a fresh dict every time we set the
var. `get_task_log_attrs()` returns an empty dict when the var is `None`,
which keeps callers from having to handle the None case themselves.

## Piece 2: `task_log_context` — set / restore via tokens

```python
class task_log_context:
    def __init__(self, attrs: dict[str, Any] | None = None, /) -> None:
        self._attrs = dict(attrs) if attrs else {}
        self._token = None

    def __enter__(self) -> dict[str, Any]:
        parent = _local_log_attrs.get() or {}
        merged = {**parent, **self._attrs}
        self._token = _local_log_attrs.set(merged)   # ← returns a token
        return merged

    def __exit__(self, *exc_info) -> None:
        _local_log_attrs.reset(self._token)          # ← restores the prior state
```

Three design choices to notice.

### The dict is positional-only and the only argument

`task_log_context({...})`, not `task_log_context(task_id=..., **extra)`. The
library does not name any field — it accepts whatever dict you pass and
merges it into the current context. The earlier API privileged `task_id`
with a positional kwarg and an auto-UUID fallback; that's been removed
because it baked a domain assumption ("there's always a task with an id")
into a generic logging library. If you want a UUID, generate one yourself.

### Inheritance: nested contexts merge with their parent

If you nest `task_log_context` blocks, the inner one inherits everything the
outer one set:

```python
with task_log_context({"task_id": "outer", "region": "us-west"}):
    # ctx = {task_id: "outer", region: "us-west"}
    with task_log_context({"task_id": "inner"}):
        # ctx = {task_id: "inner", region: "us-west"}  ← region inherited
        ...
```

This is what you want in practice: the outer context represents enclosing
work (a request) and the inner one represents a nested unit of work (a
sub-task). The sub-task should see everything the parent set unless it
chooses to override it.

### Restoration: tokens, not "save-and-restore"

`ContextVar.set(x)` doesn't just overwrite the value — it returns an
opaque "token" that records *exactly* what was there before. `reset(token)`
restores precisely that, even if multiple `set()` calls happened in
between. Tokens compose under nesting and are exception-safe (the
`finally` runs even when the body raises).

A naïve "save the old value, restore on exit" would also work for simple
cases but break under reentrancy. Tokens are the stdlib's blessed mechanism
for this and it's worth using them as designed.

## Piece 3: `TaskLogFilter` — connecting context to logs

So `_local_log_attrs` holds the right value. How does it end up on a log line?

Through a `logging.Filter`. Filters in stdlib `logging` are misnamed — yes
they can drop records by returning `False`, but they are also the
**canonical place to enrich records before formatting**. Ours never drops
anything; it only enriches, and it does so on a *copy* of the record:

```python
class TaskLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        record = copy.copy(record)               # ← see "Why we copy"

        for key, value in {
            **self._global_log_attrs,            # global (lowest priority)
            **get_task_log_attrs(),              # local context (highest)
        }.items():
            setattr(record, key, value)

        return record                            # ← LogRecord, not bool
```

`setup_task_logging` attaches this filter to the stdout handler. So every
record emitted by **any** logger in the process — your code, `urllib3`,
`boto3`, anything — passes through this filter on its way out, and gets
every key from `global_log_attrs` and the active `task_log_context`
attrs, set as attributes on a fresh copy.

Then `JsonFormatter` reads those attributes off the (copied) record and
writes them as JSON.

### Why we copy the record instead of mutating it

`logging` passes each `LogRecord` *by reference* to every handler in the
chain. If we mutated the record in place, our enrichment would leak onto
the same record object as it travels to **other** handlers the host
application has installed:

```
log.info(...)
  → makeRecord(...)                    # one LogRecord r
  → handler_A (ours)
       → TaskLogFilter mutates r.task_id = "abc"
  → handler_B (e.g. Sentry, debug StreamHandler)
       → sees r.task_id = "abc" too — wasn't asked for, may not want
```

For our specific topology (one handler we own) this never bites in
practice. But "the host app might install another handler" is a
plausible thing — Sentry's breadcrumb handler, a debug
`StreamHandler`, a custom audit logger — and the cookbook
[Imparting contextual information in handlers][cookbook-handlers]
documents the canonical fix: have the filter return a *new* record
instead of modifying in place.

[cookbook-handlers]: https://docs.python.org/3/howto/logging-cookbook.html#imparting-contextual-information-in-handlers

stdlib supports this directly. `Filterer.filter` (the base class behind
both `Logger` and `Handler`) accepts either a `bool` or a `LogRecord`
from each filter; if a filter returns a record, that record replaces
the original *for this chain only*. So:

- `return True` / `return False` → keep / drop the original record
- `return record` (a fresh `LogRecord`) → use this enriched copy in this
  chain, leave the original unchanged for any sibling handlers

We use the third option: `copy.copy(record)`, mutate the copy, return
it. The cost is one shallow copy per record per handler. `LogRecord`'s
state is mostly its `__dict__`, which `copy.copy` duplicates; the
expensive bits — `exc_info` tuples, traceback frames, source code paths
— are immutable from our perspective and shared safely by reference.

The upshot: even if a host app adds another handler to the root
logger, our enrichment confines itself to the records flowing through
*our* handler. Their records are pristine.

### Why we don't protect stdlib field names from being overwritten

An earlier revision of the filter had a `_RESERVED_LOGRECORD_ATTRS`
set listing every stdlib `LogRecord` attribute name (`msg`, `levelname`,
`name`, ...) and silently dropped any user key that collided. The
intent was to stop `task_log_context({"name": "PaymentService"})`
from clobbering `record.name` (the logger name).

It's gone. Three reasons:

1. **Silent drop is bad UX.** A user binds
   `task_log_context({"name": "PaymentService"})`, sees no `name=PaymentService`
   in their JSON output, has no idea why. With no protection, they
   *immediately* see `name=PaymentService` in the output, realise it's
   replacing the logger name, and pick a different key. Tighter feedback
   loop, no debugging required.

2. **Damage is contained.** We copy the record per handler-call (see
   above), so any weirdness only affects this one record's JSON. If the
   user manages to pick a collision that breaks formatting (e.g.
   clobbers `msg` in a way `getMessage()` can't substitute), stdlib's
   `Handler.emit` catches the formatter exception via `handleError`,
   prints a traceback to stderr, and drops that line. Their program
   keeps running. Other handlers' records are untouched.

3. **The library doesn't pretend to know better than the user.** If
   you ask for `levelname=URGENT` you get `levelname=URGENT`. The
   library's job is to ferry your dict to the JSON output, not to
   referee what your dict contains.

A separate set with overlapping membership lives on in `formatters.py`
as `_DROPPED_LOGRECORD_ATTRS` — but its job is different: it lists
stdlib attrs the formatter chooses to *omit from the JSON output*
(redundant timestamps, internal `exc_text`, ...), not attrs that need
protection from user clobbering. The two sets used to share a stdlib
attribute name list (centralised in `task_logging/_logrecord.py` to
avoid drift); now only the formatter side remains.

### Why the filter is on the handler, not on a logger

Filters on a logger only run for records emitted *directly* on that
logger. They do **not** run for records that propagate up from child
loggers. So a filter on the root logger wouldn't see anything emitted from
`logging.getLogger("urllib3")`.

Filters on a handler run for **every** record that reaches the handler,
no matter which logger emitted it. That's exactly the behaviour we want
— we install handlers on the root, every record propagates up to those
handlers, and the filter enriches every record on its way through.

This asymmetry is documented in the stdlib but trips most people up the
first time they encounter it. See
[stdlib-logging-primer.md](stdlib-logging-primer.md) for the full
explanation.

## Why third-party libraries' logs are tagged too

This is the part that often surprises people: how does
`requests.get(...)` inside the `with` block end up with a `task_id`?

Three things conspire:

1. **stdlib loggers form a tree rooted at the empty-name root logger.**
   `urllib3.connectionpool` is a child of `urllib3` is a child of root.
   When `urllib3.connectionpool` emits a record, the record propagates up
   the tree until it hits a handler. By default, only the root has a
   handler — the one we installed in `setup_task_logging`.

2. **The filter is on the handler, not on individual loggers.** So every
   record that reaches the root handler — regardless of which logger
   emitted it — passes through `TaskLogFilter`.

3. **ContextVars don't care who you are.** Inside the `with task_log_context(...)`
   block, `_local_log_attrs.get()` returns the right dict no matter where in the
   call stack you are. `requests` calls `urllib3` calls some socket code
   calls `logging.getLogger("urllib3.connectionpool").warning(...)` — the
   warning's record reaches the root handler, the filter calls
   `_local_log_attrs.get()`, and the answer is still
   `{"task_id": "task-42", ...}`.

That's the whole magic. There's no patching of third-party libraries, no
monkey-patching of `logging`, no special integration. Just stdlib
mechanisms used as designed.

## End-to-end trace

Here's what happens when you write:

```python
setup_task_logging(global_log_attrs={"service": "OrderService"})
log = logging.getLogger("biz")

with task_log_context({"task_id": "task-42", "user_id": "u-1"}):
    log.info("hello")
```

1. `setup_task_logging` creates a `StreamHandler(sys.stdout)`, attaches a
   `TaskLogFilter(global_log_attrs={"service": "OrderService"})` and a
   `JsonFormatter`, and adds the handler to the **root** logger.

2. Entering `task_log_context({"task_id": "task-42", "user_id": "u-1"})`
   merges `{"task_id": "task-42", "user_id": "u-1"}` with any enclosing
   parent (none here), calls `_local_log_attrs.set(...)`, and stores the
   returned token on the context object.

3. `log.info("hello")` creates a `LogRecord` and propagates it up the
   logger tree to the root logger's handler.

4. The root handler's filters run. `TaskLogFilter.filter(record)` reads
   `_local_log_attrs.get()` (`{"task_id": "task-42", "user_id": "u-1"}`),
   merges it with `global_log_attrs`, and `setattr`s every key onto the
   record copy: `record.service`, `record.task_id`, `record.user_id`.

5. `JsonFormatter.format(record)` reads all those attributes off the
   record and produces (key names mirror stdlib `LogRecord` attributes;
   see [json-schema.md](json-schema.md)):
   ```json
   {"created":1717839622.5,"levelname":"INFO","message":"hello","name":"biz","service":"OrderService","task_id":"task-42","user_id":"u-1",...}
   ```

6. The handler writes that line to **stdout**. The container runtime
   (Docker daemon / Kubernetes kubelet) captures it into its standard
   per-container log location.

7. The `with` block exits. `_local_log_attrs.reset(token)` restores the
   prior state. Any logs emitted after the block carry no `task_id`.

8. Alloy discovers the container via the Docker socket (or K8s API),
   tails its stdout, parses the JSON, ships it to Loki, and you query
   `{service="OrderService"} | json | task_id="task-42"` in Grafana.

## Alternatives we rejected

| Alternative | Why not |
|---|---|
| **Thread-local storage** (`threading.local`) | Doesn't work with `asyncio` — all coroutines on one event loop share a thread, so they'd share the same `task_id`. ContextVars work for both. |
| **`logging.LoggerAdapter`** | Forces every log site to use a special logger object. Doesn't capture third-party libraries that call `logging.getLogger("urllib3")` themselves. |
| **`extra={"task_id": ...}` on every call** | Same problem: every log site has to know to do it. Defeats the entire point of having implicit context. |
| **`logging.setLogRecordFactory`** | Works, but is a process-global mutation that's hard to reverse and tests can't isolate cleanly. A per-handler filter is local and reversible (and `setup_task_logging` can replace its own handlers idempotently). |
| **Make `task_id` a Loki label** | Would crash Loki — Loki indexes by label combinations, and one stream per task is high-cardinality hell. `task_id` rides inside the JSON; Alloy promotes it to "structured metadata" so it stays queryable but unindexed. See [why-json-logs.md](why-json-logs.md). |

## TL;DR

```
task_log_context()         →  contextvars.ContextVar.set/reset
                          (per-thread / per-asyncio-task storage with a
                           token-based stack, so nesting & restoration
                           just work, exception-safe by construction)

TaskLogFilter      →  logging.Filter on the root handler that reads
                          the ContextVar and stamps task_id and extras
                          onto every LogRecord — yours and third-party —
                          on its way out

JsonFormatter          →  reads those attributes off the record and
                          emits one line of JSON
```

Everything else (the container runtime capturing stdout, Alloy
discovering and scraping it, Loki ingesting, Grafana querying) is
plumbing layered on top of that JSON.
