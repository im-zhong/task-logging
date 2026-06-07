# How `task_context` works

`task_context` is the most important piece of the library and the part whose
mechanics are least obvious. This note explains how it makes `task_id` flow
through threads, asyncio tasks, and even third-party libraries' logs —
without modifying anything about those libraries.

## The problem

We want this:

```python
with task_context(task_id="task-42"):
    log.info("step 1")               # ← tagged "task-42"
    do_some_work()                   # ← logs in here are tagged
    requests.get("https://...")      # ← urllib3's internal logs are tagged
log.info("after")                    # ← back to no task_id
```

Three things have to be true for this to work:

1. The `task_id` must be **available to every logger in the process** without
   passing it explicitly down through every function call.
2. It must be **automatically restored** when the block exits — including
   when the block exits via an exception.
3. It must be **isolated per request** — concurrent requests in different
   threads or asyncio tasks must not see each other's `task_id`.

`task_context` solves this by combining three stdlib primitives:
`contextvars.ContextVar`, `logging.Filter`, and `logging.LogRecord`.

## Piece 1: `contextvars.ContextVar` — the storage

```python
_task_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "task_logging_ctx", default=None
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
var. `get_task_context()` returns an empty dict when the var is `None`,
which keeps callers from having to handle the None case themselves.

## Piece 2: `task_context()` — set / restore via tokens

```python
@contextmanager
def task_context(task_id=None, **extra):
    parent = _task_ctx.get() or {}
    merged = {**parent, **extra}
    merged["task_id"] = task_id if task_id is not None else uuid.uuid4().hex
    token = _task_ctx.set(merged)        # ← returns a token
    try:
        yield merged
    finally:
        _task_ctx.reset(token)           # ← restores the prior state
```

Two design choices to notice.

### Inheritance: nested contexts merge with their parent

If you nest `task_context` calls, the inner one inherits everything the outer
one set:

```python
with task_context(task_id="outer", region="us-west"):
    # ctx = {task_id: "outer", region: "us-west"}
    with task_context(task_id="inner"):
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

## Piece 3: `TaskContextFilter` — connecting context to logs

So `_task_ctx` holds the right value. How does it end up on a log line?

Through a `logging.Filter`. Filters in stdlib `logging` are misnamed — yes
they can drop records by returning `False`, but they are also the
**canonical place to mutate records before formatting**. Ours never drops
anything; it only enriches:

```python
class TaskContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self._service
        record.env = self._env
        record.hostname = _HOSTNAME
        record.pid = _PID

        ctx = get_task_context()
        record.task_id = ctx.get("task_id")

        for key, value in {**self._extra, **ctx}.items():
            if key in _RESERVED_LOGRECORD_ATTRS:
                continue
            setattr(record, key, value)
        return True
```

`setup_logging` attaches this filter to the stdout handler. So every
record emitted by **any** logger in the process — your code, `urllib3`,
`boto3`, anything — passes through this filter on its way out, and gets
`record.task_id`, `record.service`, plus every key in the active context
dict, slapped onto it as an attribute.

Then `JsonFormatter` reads those attributes off the record and writes them
as JSON.

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
   handler — the one we installed in `setup_logging`.

2. **The filter is on the handler, not on individual loggers.** So every
   record that reaches the root handler — regardless of which logger
   emitted it — passes through `TaskContextFilter`.

3. **ContextVars don't care who you are.** Inside the `with task_context(...)`
   block, `_task_ctx.get()` returns the right dict no matter where in the
   call stack you are. `requests` calls `urllib3` calls some socket code
   calls `logging.getLogger("urllib3.connectionpool").warning(...)` — the
   warning's record reaches the root handler, the filter calls
   `_task_ctx.get()`, and the answer is still
   `{"task_id": "task-42", ...}`.

That's the whole magic. There's no patching of third-party libraries, no
monkey-patching of `logging`, no special integration. Just stdlib
mechanisms used as designed.

## End-to-end trace

Here's what happens when you write:

```python
setup_logging(service="OrderService")
log = logging.getLogger("biz")

with task_context(task_id="task-42", user_id="u-1"):
    log.info("hello")
```

1. `setup_logging` creates a `StreamHandler(sys.stdout)`, attaches a
   `TaskContextFilter(service="OrderService")` and a `JsonFormatter`,
   and adds the handler to the **root** logger.

2. `task_context(...)` builds `{"task_id": "task-42", "user_id": "u-1"}`
   and calls `_task_ctx.set(...)`, getting back a token.

3. `log.info("hello")` creates a `LogRecord` and propagates it up the
   logger tree to the root logger's handler.

4. The root handler's filters run. `TaskContextFilter.filter(record)`
   calls `_task_ctx.get()`, which (because we're inside the `with`)
   returns `{"task_id": "task-42", "user_id": "u-1"}`. The filter writes
   `record.service`, `record.task_id = "task-42"`,
   `record.user_id = "u-1"`.

5. `JsonFormatter.format(record)` reads all those attributes off the
   record and produces:
   ```json
   {"ts":"...","level":"INFO","msg":"hello","service":"OrderService","task_id":"task-42","user_id":"u-1",...}
   ```

6. The handler writes that line to **stdout**. The container runtime
   (Docker daemon / Kubernetes kubelet) captures it into its standard
   per-container log location.

7. The `with` block exits. `_task_ctx.reset(token)` restores the prior
   state. Any logs emitted after the block have `task_id: null`.

8. Alloy discovers the container via the Docker socket (or K8s API),
   tails its stdout, parses the JSON, ships it to Loki, and you query
   `{service="OrderService"} | json | task_id="task-42"` in Grafana.

## Alternatives we rejected

| Alternative | Why not |
|---|---|
| **Thread-local storage** (`threading.local`) | Doesn't work with `asyncio` — all coroutines on one event loop share a thread, so they'd share the same `task_id`. ContextVars work for both. |
| **`logging.LoggerAdapter`** | Forces every log site to use a special logger object. Doesn't capture third-party libraries that call `logging.getLogger("urllib3")` themselves. |
| **`extra={"task_id": ...}` on every call** | Same problem: every log site has to know to do it. Defeats the entire point of having implicit context. |
| **`logging.setLogRecordFactory`** | Works, but is a process-global mutation that's hard to reverse and tests can't isolate cleanly. A per-handler filter is local and reversible (and `setup_logging` can replace its own handlers idempotently). |
| **Make `task_id` a Loki label** | Would crash Loki — Loki indexes by label combinations, and one stream per task is high-cardinality hell. `task_id` rides inside the JSON; Alloy promotes it to "structured metadata" so it stays queryable but unindexed. See [why-json-logs.md](why-json-logs.md). |

## TL;DR

```
task_context()         →  contextvars.ContextVar.set/reset
                          (per-thread / per-asyncio-task storage with a
                           token-based stack, so nesting & restoration
                           just work, exception-safe by construction)

TaskContextFilter      →  logging.Filter on the root handler that reads
                          the ContextVar and stamps task_id and extras
                          onto every LogRecord — yours and third-party —
                          on its way out

JsonFormatter          →  reads those attributes off the record and
                          emits one line of JSON
```

Everything else (the container runtime capturing stdout, Alloy
discovering and scraping it, Loki ingesting, Grafana querying) is
plumbing layered on top of that JSON.
