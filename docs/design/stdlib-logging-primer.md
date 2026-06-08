# A primer on stdlib `logging`

This is a bottom-up tour of stdlib `logging` aimed at anyone who wants to
read or modify this library without surprises. The `logging` module looks
deceptively simple but has some non-obvious mechanics. Once the model
clicks, the source becomes short and predictable.

We go: **`LogRecord` → `Logger` → `Handler` → `Filter` → `Formatter`**, then
how they compose.

## The big picture in one diagram

```
                                              ┌─────────────────────────┐
                                              │  Formatter              │
                                              │  (record → str)         │
                                              └────────────▲────────────┘
                                                           │
your code                                                  │
   │                                                       │
   ▼                                              ┌────────┴────────┐
logger.info("hi")                                 │   Handler       │
   │                                              │   (where to     │
   ▼                                              │    write)       │
┌──────────────────┐                              └────────▲────────┘
│  LogRecord       │                                       │ filters?
│  msg=, level=,   │                                       │
│  module=, ...    │                                       │
└────────┬─────────┘                                       │
         │ Logger.handle(record)                           │
         │                                                 │
   ┌─────┴──────┐                                          │
   │   Logger   │── filters? ─── handlers? ────────────────┘
   │ "myapp.db" │
   └─────┬──────┘
         │ propagate=True (default)
         ▼
   ┌──────────┐
   │  Logger  │── filters? ─── handlers? ─── (no handler? skip)
   │ "myapp"  │
   └─────┬────┘
         │
         ▼
   ┌──────────┐
   │   root   │── filters? ─── handlers? ──▶ Handler ──▶ stderr / file / ...
   │  Logger  │
   └──────────┘
```

Keep this picture in mind. The rest is detail.

## 1. `LogRecord` — the unit of work

When you call `logger.info("hello %s", name)`, `logging` doesn't
immediately format or write anything. It builds a `LogRecord` — a plain
object with a bag of attributes:

```python
record.name          # "myapp.db"
record.levelno       # 20
record.levelname     # "INFO"
record.msg           # "hello %s"      (the format string, NOT formatted)
record.args          # ("alice",)      (the args, NOT applied)
record.pathname      # "/app/db.py"
record.lineno        # 42
record.funcName      # "connect"
record.module        # "db"
record.process       # PID
record.thread        # thread id
record.threadName    # "MainThread"
record.created       # time.time() snapshot
record.exc_info      # (type, value, tb) or None
```

Two things to internalise.

**The message is not formatted yet.** `record.msg` is the format string and
`record.args` are the placeholders. Formatting (`record.getMessage()`)
happens later, in the handler. This is intentional: it lets logging skip
formatting if no handler will emit the record. That's why
`log.info("user %s did %s", user, action)` is preferred over
`log.info(f"user {user} did {action}")` — the f-string formats
unconditionally; the lazy form only formats if the record will actually
be emitted.

**You can stick anything onto a record.** It's a plain object. Filters
routinely do `record.task_id = "..."` to enrich the record before it
reaches the handler. That's exactly what `TaskLogFilter` does.

## 2. `Logger` — a named node in a tree

You don't call `Logger()` directly. You ask the system for one by name:

```python
log = logging.getLogger("myapp.db.pool")
```

The name is dotted, and **dots define a parent-child hierarchy**:

```
root  (the empty-name "" logger)
└── "myapp"
    ├── "myapp.api"
    └── "myapp.db"
        └── "myapp.db.pool"
```

Three rules govern the tree.

### Rule 1: `getLogger()` is idempotent

```python
logging.getLogger("myapp.db") is logging.getLogger("myapp.db")  # True
```

Same name → same instance. The tree is a process-wide singleton.

### Rule 2: Loggers are auto-created on demand

You don't have to register parents. Just asking for `"myapp.db.pool"`
ensures the chain `root → "myapp" → "myapp.db" → "myapp.db.pool"` exists.
Intermediate ones may be **placeholders** (no handlers, no level set);
they exist only to maintain the tree.

### Rule 3: `__name__` is the conventional name

The stdlib idiom in every module is:

```python
log = logging.getLogger(__name__)
```

Because `__name__` is `"myapp.db.pool"` for the file `myapp/db/pool.py`,
the logger names automatically mirror your import structure. Any
configuration you apply at `"myapp"` (like setting level to DEBUG)
automatically affects `myapp.api`, `myapp.db`, `myapp.db.pool`, etc.

### Levels and effective levels

Every logger has a `level` (default `NOTSET`). When you call
`log.info(...)`, the logger has to decide whether to handle the record at
all. It uses **effective level**: walk up the tree until you find a
logger with a level set; that's your threshold.

```
root.level         = WARNING   ← set by setup_task_logging
"myapp".level      = NOTSET    (inherit)
"myapp.db".level   = DEBUG     ← explicitly set
"myapp.db.pool"    = NOTSET    (inherit "myapp.db" → DEBUG)
```

So `myapp.db.pool` effectively logs at DEBUG. This is the mechanism
behind `setup_task_logging(quiet_loggers={"urllib3": logging.WARNING})`: we
set the level on the `urllib3` logger so all of `urllib3.connectionpool`,
`urllib3.util`, etc. inherit it.

### Propagation — the part that confuses everyone

When a logger handles a record, it does this:

```python
def handle(self, record):
    if self.disabled:           return
    if not self.filter(record): return         # ← this logger's filters
    self.callHandlers(record)

def callHandlers(self, record):
    c = self
    while c:
        for h in c.handlers:
            if record.levelno >= h.level:
                h.handle(record)               # ← runs h's filters + emit
        if not c.propagate:
            break
        c = c.parent
```

Read that carefully. **A record walks UP the tree, visiting every
ancestor's handlers**, until either:

- it hits a logger with `propagate = False`, or
- it reaches the root.

This is why `task_logging` installs handlers **only on the root logger**:
every log emitted anywhere in the process — yours, `requests`, `urllib3` —
propagates up and ends up at the root's handlers. One handler, one
formatter, one filter, captures everything.

It's also why a common bug is "I added a handler and now I'm getting
duplicate logs": the user added a handler to a child logger, but
`propagate=True` (default) sent the record to the root too, where another
handler emitted it again. **Rule of thumb: handlers go on the root,
configured in one place.**

## 3. `Handler` — where records become bytes

A handler answers one question: **where does the record go?** stdlib ships
with several:

| Handler | Sink |
|---|---|
| `StreamHandler` | a file-like object (default: `sys.stderr`) |
| `FileHandler` | a single file |
| `RotatingFileHandler` | file with size-based rotation |
| `TimedRotatingFileHandler` | file with time-based rotation |
| `SysLogHandler` | syslog daemon |
| `SMTPHandler` | email (please don't) |
| `QueueHandler` / `QueueListener` | hand off to a background thread |
| `NullHandler` | swallow everything (libraries should attach this by default) |

A handler has:

- **A level** — its own threshold, applied *after* the logger's
  effective-level check. Useful for "log everything to a file at DEBUG,
  but only WARN+ to stderr."
- **A formatter** — turns the record into the final string. If you don't
  set one, you get `record.getMessage()` plus newline, with no timestamp.
- **A list of filters** — same `Filter` objects as on loggers; covered next.

The handler's `handle(record)` method runs filters, calls `format(record)`
to build the string, and writes it to the sink.

### `NullHandler` — a library convention

Library code (yours, mine, third-party) shouldn't configure logging
— that's the *application's* job. But if you do
`logging.getLogger("mylib").info(...)` and the application hasn't
configured anything, stdlib falls back to a "last resort" handler that
writes WARN+ to stderr, which can surprise users. The fix is for libraries
to attach a `NullHandler` to their top-level logger:

```python
# at the top of your library's __init__.py
logging.getLogger("mylib").addHandler(logging.NullHandler())
```

This says "yes, this logger has a handler, so don't fall back to last
resort, but do nothing." `task_logging` itself doesn't install one because
it's a logging-configuration tool rather than a library you'd log *from* —
but most other libraries should.

## 4. `Filter` — record gatekeepers (and enrichers)

A filter is anything with `.filter(record) -> bool`. Returning `False`
drops the record; returning `True` lets it through. Filters can live on
**either loggers or handlers**, with a key behavioural difference (next
section).

The stdlib also ships a built-in `logging.Filter("myapp.db")` that drops
records whose name doesn't start with `"myapp.db"`. Useful, but boring.

The interesting use of filters is **enrichment**, not filtering. Inside
`filter()`, before returning `True`, you mutate the record:

```python
class TaskLogFilter(logging.Filter):
    def filter(self, record):
        for key, value in get_task_log_attrs().items():    # contextvar lookup
            setattr(record, key, value)
        return True   # never drop
```

Now any handler whose formatter walks `record.__dict__` will see those
fields. This is the canonical stdlib pattern for "I want to add a field
to every log line."

### The asymmetry between logger-filters and handler-filters

This trips people up.

- **Logger-filters apply only to records emitted *directly* on that
  logger.** They do NOT run for records that propagate up from a child
  logger. So a filter on `"myapp"` will NOT see records from
  `"myapp.db"`.

- **Handler-filters run on every record that reaches the handler**,
  no matter which logger originally emitted it.

That asymmetry is exactly why `task_logging` puts `TaskLogFilter` on
the *handler*, not on a logger:

```python
file_handler.addFilter(ctx_filter)        # ✓ catches everything
# vs.
logging.getLogger().addFilter(ctx_filter) # ✗ only catches root's own records
```

Subtle, but important.

## 5. `Formatter` — the last mile

After a handler accepts a record and runs its filters, the formatter turns
it into a string. The default is `logging.Formatter("%(message)s")`, but
you'd typically use:

```python
logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
```

For structured logging, you write a custom formatter that overrides
`format(record) -> str` and emits JSON — that's exactly what
`JsonFormatter` does. It reads attributes off the record (including the
ones the filter stamped on, like `record.task_id`) and dumps a JSON
object.

Two methods worth knowing:
- `formatter.format(record)` — the entry point handlers call
- `formatter.formatTime(record, datefmt)` — formats `record.created` as a
  timestamp

## Putting it together: what `setup_task_logging` actually does

Now you can read the whole flow without surprise:

```python
def setup_task_logging(*, global_log_attrs=None, level=INFO, ...):
    root = logging.getLogger()                # the tree's root
    root.setLevel(level)                      # global threshold

    ctx_filter = TaskLogFilter(global_log_attrs=global_log_attrs)
    formatter  = JsonFormatter()

    handler = StreamHandler(sys.stdout)       # one handler, stdout
    handler.setFormatter(formatter)           # how to render
    handler.addFilter(ctx_filter)             # what to enrich
    root.addHandler(handler)                  # attach to ROOT
```

When `urllib3.connectionpool` later does `log.warning("retrying")`:

1. The `urllib3.connectionpool` logger creates a `LogRecord`.
2. Effective level check: `urllib3.connectionpool` has no level →
   `urllib3` has WARNING (set via `quiet_loggers`) → passes.
3. `callHandlers` walks up the tree: `urllib3.connectionpool` (no
   handlers) → `urllib3` (no handlers) → root (has our stdout handler).
4. The handler:
   - Check handler level (default NOTSET → pass)
   - Run handler filters: `TaskLogFilter` runs, stamps
     `record.task_id = current_task_id_from_contextvar`, returns `True`
   - Format: `JsonFormatter.format(record)` reads the freshly-stamped
     fields and emits a JSON line
   - Write to stdout — the container runtime captures it from there.
5. Done.

Notice nothing in `urllib3` was modified. The whole effect comes from
stdlib's normal propagation + a handler-level filter + a custom
formatter. The library never even knows it's being instrumented.

## Six rules that will save you grief

1. **Always `logging.getLogger(__name__)`.** It gives you the right place
   in the tree for free.
2. **Configure handlers in exactly one place, on the root.** Anything
   else risks duplicates or gaps.
3. **Set logger levels to control *what gets emitted*; set handler levels
   to control *what each sink sees*.** They're different knobs for
   different jobs.
4. **Use filters for enrichment, not for filtering.** It's the only good
   place to attach contextual fields to every record.
5. **Prefer `log.info("user %s did %s", u, a)` over f-strings.** Lazy and
   (for things like Sentry) preserves the message template as a stable
   identity.
6. **Libraries: attach a `NullHandler` to your top-level logger.** Don't
   configure handlers in library code.

## What to read next

If you want to go deeper, the stdlib docs have three pages, in this order:

1. [Logging HOWTO](https://docs.python.org/3/howto/logging.html) — the
   introductory tutorial.
2. [Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html)
   — recipes including the `QueueHandler` / `QueueListener` async
   pattern, multi-process logging, contextual logging via `LoggerAdapter`
   and `Filter`.
3. [`Lib/logging/__init__.py` source](https://github.com/python/cpython/blob/main/Lib/logging/__init__.py)
   — surprisingly readable. ~2000 lines. Once the model clicks, the
   source is short.

Or just open `task_logging/setup.py`, `filters.py`, and `formatters.py`
and re-read them — they should read very differently than they did
before.
