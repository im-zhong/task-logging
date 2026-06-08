# Migrating from `TaskLogger` (v0.0.1)

The original v0.0.1 of this library exposed a `TaskLogger` class that
inherited from `logging.Logger` and bound `(service_name, task_id)` to
the logger instance. Each call to its `info()` / `error()` / etc.
methods walked the stack with `inspect.stack()` to capture context, then
persisted the record via a `TaskLoggingDatabaseInterface` (PostgreSQL,
in practice).

That whole class is gone. Its capabilities aren't — they're
redistributed across **three** components, and the redistribution
fixed two real issues with the original design.

This note records the mapping (so anyone porting v0.0.1 code knows
where each capability went) and the design rationale (so the split
isn't just an opaque rewrite).

## The three-component split

| Old: bound to `TaskLogger` instance | New: where it lives |
|---|---|
| `service_name` | `setup_task_logging(global_log_attrs={"service": ...})` — process-wide, set once at startup |
| `task_id` | `task_log_context({"task_id": ...})` — per-request, flows via `contextvars` |
| every other field (hostname, exc_info, frame info, …) | `TaskLogFilter` + stdlib's auto-populated `LogRecord` attributes |

The combination — not any single one of them — replaces what
`TaskLogger` did. `task_log_context` alone is **not** a drop-in for
`TaskLogger`; you also need `setup_task_logging` running and the filter
attached.

## Capability-by-capability mapping

| Old `TaskLogger` did | Now done by | Notes |
|---|---|---|
| Bind `service_name` to the logger | `TaskLogFilter` (set at `setup_task_logging` time) | `service` is process-wide; it belongs to one-time setup, not per-call. |
| Bind `task_id` to the logger | `contextvars` + `task_log_context()` | `task_id` is per-request and now flows through threads / asyncio tasks automatically. See [task-context.md](task-context.md). |
| Capture `hostname` | `TaskLogFilter` (cached at module import) | Same — was always per-process. |
| Capture `process_id` | stdlib auto-populates `record.process` | We don't even have to set it. |
| Capture `thread_name` / `thread_id` | stdlib auto-populates `record.threadName` / `record.thread` | Free from stdlib. |
| Capture `filename` / `module_name` / `function_name` / `line_no` (via `inspect.stack()` walk) | stdlib auto-populates `record.pathname` / `record.module` / `record.funcName` / `record.lineno` | The biggest improvement — see "Why this is better" below. |
| Capture exception `name` / `details` / `stack_trace` / `locals_dict` | `JsonFormatter._render_exc_info()` | Same logic (walks to the deepest frame, `repr()`s locals). |
| `stack_depth` (length of `inspect.stack()`) | — | Not captured. See "What went away" below. |
| Persist via `TaskLoggingDatabaseInterface` | stdout → Alloy → Loki | The whole pivot you read about in the README. |

## Why the new design is strictly better

### 1. No more `inspect.stack()` walks

The old `_get_context()` did roughly:

```python
stacks = inspect.stack(context=0)
for frame_info in inspect.stack(context=0):    # twice!
    if frame_info.frame.f_globals.get("__name__") == logger_module:
        continue
    ctx_msg.filename = frame_info.filename
    ...
```

Two problems with this:

- **Performance.** `inspect.stack()` constructs `FrameInfo` objects for
  *every* frame in the stack, including reading source code from disk.
  For a deep stack and a hot logging path, this is slow.
- **Correctness depends on guessing.** "Skip frames in our own module"
  only works if the caller is in a *different* module. If someone
  subclassed `TaskLogger` in a third file, or wrote a wrapper that
  happened to live in `task_logger.py`, the walk would land on the
  wrong frame and report misleading filenames.

Stdlib solves both. `LogRecord.__init__` uses `sys._getframe()` directly
with the `stacklevel` argument that the standard `Logger.info()` /
`.debug()` / etc. methods set correctly. **The frame is identified by
stdlib's own logger machinery, not by guessing module names.** Filename,
module, funcName, and lineno are correct by construction.

### 2. Third-party library logs come along for free

This was the unstated weakness of the v0.0.1 design. `TaskLogger` only
enriched logs that went through *its own* `info()` / `error()` / etc.
methods. So:

```python
# v0.0.1 — only YOUR code was enriched
task_logger.info("starting")    # ← tagged with task_id
requests.get("https://...")     # ← urllib3.warning("retrying") — NOT tagged
```

`requests` calls `logging.getLogger("urllib3").warning(...)`, which has
nothing to do with your `TaskLogger` instance. Those records went to
the stdlib root handler (or were dropped), un-enriched.

The new design enriches *every* record that propagates to the root
handler, regardless of which logger emitted it. The `Filter`-on-handler
approach captures everything stdlib's tree carries upward — see
[task-context.md "Why third-party libraries' logs are tagged too"](task-context.md).

### 3. One write path instead of two

The old `TaskLogger` did this in every method:

```python
def info(self, msg, *args, **kwargs) -> None:
    super().info(msg, *args, **kwargs)         # ← stdlib path: handlers, filters, formatters
    self._append_task_log(level=logging.INFO, message=msg)  # ← DB path, bypasses everything
```

Two completely separate paths for the same log call:

1. The stdlib path went to whatever handlers were installed (often
   nothing useful in production configurations).
2. The DB path went straight to `TaskLoggingDatabaseInterface`,
   bypassing every stdlib handler / filter / formatter.

If a host application installed, say, a Sentry handler, it would see
the stdlib version (without enrichment) and not the DB version. The
two views could disagree about what was logged. The new design has
**one** path: the record is built once, enriched once, formatted once,
written once. Whatever consumer you point at the JSON gets the same
content as everyone else.

## What went away

**`stack_depth`** (the literal length of `inspect.stack()` at the call
site) is the only capability not preserved. It was almost certainly
unused — depth-of-stack is rarely actionable, and computing it required
the same expensive `inspect.stack()` call we just got rid of.

If you do want it back, a one-line filter recovers it:

```python
import inspect, logging

class StackDepthFilter(logging.Filter):
    def filter(self, record):
        record.stack_depth = len(inspect.stack())
        return True

setup_task_logging()
logging.getLogger().handlers[0].addFilter(StackDepthFilter())
```

The field will then ride through `JsonFormatter` automatically (it's
just another attribute on `record.__dict__`).

## Code-level migration

If you're porting code:

```python
# Before (v0.0.1)
factory = TaskLoggerFactory(task_logging_db=db)

def handle_request(req):
    logger = factory.new(service_name="OrderService", task_id=req.id)
    logger.info("handling request")
    requests.get("https://api.x.com")  # ← was NOT tagged
```

```python
# After
setup_task_logging(
    global_log_attrs={"service": "OrderService", "env": "prod"},
)

log = logging.getLogger(__name__)

def handle_request(req):
    with task_log_context({"task_id": req.id}):
        log.info("handling request")
        requests.get("https://api.x.com")  # ← is now tagged
```

Three differences worth pointing out:

1. **Setup is global, not per-task.** `setup_task_logging` runs once at
   process startup. There's no factory, no per-task logger instance.
2. **Use module loggers, not service loggers.** Replace
   `factory.new(service_name=...)` with the stdlib idiom
   `logging.getLogger(__name__)`. The `service` field is still on every
   record — it's now stamped by the filter from `global_log_attrs`,
   not bound to the logger.
3. **`task_id` is a context, not an arg.** Wrap each request in
   `with task_log_context({"task_id": req.id}):`, or use the imperative
   form (`ctx = task_log_context({...}); ctx.enter() / ctx.exit()`) for
   middleware that splits enter/exit across separate hook callbacks.
   Logs *anywhere* inside the active context — yours, third-party libs,
   decorated functions — get tagged automatically.
