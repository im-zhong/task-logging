# Why one `@log_func_call`, not two decorators

> Earlier the library exposed `FunctionLogger` for free functions and
> `ClassFunctionLogger` for instance methods. The latter required the class
> to expose a `self._logger` attribute, and silently no-op'd if the attribute
> was missing. We now expose a single `@log_func_call` instead.

## What changed

**Before:**

```python
method_log = ClassFunctionLogger()

class Service:
    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)   # required

    @method_log.log_func()
    def handle(self, payload): ...
```

**Now:**

```python
class Service:
    @log_func_call(log)                                   # no class-level setup
    def handle(self, payload): ...
```

`@log_func_call(logger=None, *, level=logging.INFO)` is the single decorator. It
works on functions, instance methods, classmethods, and staticmethods alike.

## Why the old split existed

The original `TaskLogger` (now removed) bound `(service_name, task_id)` to the
**logger instance itself**. So every task needed its own logger object, and
the natural place to stash that object was on the instance:

```python
self._logger = task_logger_factory.new(service, task_id)
```

`ClassFunctionLogger` then read `self._logger` so the decorator could pick up
*the right logger* for the *right task* on the *right instance*.

That was the entire reason it existed.

## Why the split is no longer justified

The contextvars-based rewrite changed the load-bearing assumption:

- `task_id` no longer lives on the logger. It flows through `contextvars`,
  attached at log-emission time by `TaskLogFilter` regardless of which
  logger you use.
- All loggers in a process share the same handler tree. The stdlib idiom
  `logging.getLogger(__name__)` gives you a logger per module for free.
- Therefore there's no reason for a class to "own" a logger. Any logger that
  reaches the same root handlers will produce the same enriched output.

Once that's true, requiring a `self._logger` attribute is *pure coupling
without any technical payoff*:

- It's boilerplate every class has to opt into.
- The attribute name `_logger` can collide with unrelated private state.
- The "silently skip if attribute is missing" fallback is an **implicit
  failure mode** — a misspelled attribute name produces no error, just
  missing logs.
- It forces users to think about "which classes have a logger and which
  don't," for no payoff.

## Why a single `log_func_call` is enough

`log_func_call` takes the logger explicitly at decoration time, with optional
auto-resolution to `logging.getLogger(func.__module__)` when omitted. That
covers every case the old two-class API covered, plus more:

| Use case | Old API | New API |
|---|---|---|
| Free function | `FunctionLogger(log).log_func()` | `@log_func_call(log)` |
| Instance method | `ClassFunctionLogger().log_func()` + `self._logger` | `@log_func_call(log)` |
| Use the module's logger automatically | not supported | `@log_func_call()` |
| Per-instance logger (different loggers per object) | `ClassFunctionLogger(logger_attr=...)` | not supported (and that's fine — see below) |

### What about per-instance loggers?

`ClassFunctionLogger` could in principle let two instances of the same class
log to different loggers (by storing different objects in `self._logger`).
We dropped that because:

1. It's a feature solving a problem almost nobody has.
2. The legitimate version of "different instances write different things"
   is *different log content / context*, which is exactly what
   `task_log_context` solves — at the per-call level, not per-instance.
3. `__qualname__` in log messages already gives you `Service.handle` so
   you can tell methods on different classes apart in the logs.

If you genuinely need per-instance routing, pass the logger to
`__init__` and do `@log_func_call(self._logger)` — except you can't, because
decorators bind at class-definition time, before `self` exists. That
constraint is fundamental, not specific to our decorator. The honest
answer is: you'd build the routing inside the method body, not the
decorator.

## What `log_func_call` does emit

```
ENTER <qualname> args=... kwargs=...
EXIT  <qualname> return=... cost_ms=...
RAISE <qualname> after Xms          (with exc_info attached)
```

Three design choices worth flagging:

- **`func.__qualname__`, not `func.__name__`.** `__qualname__` is
  `Service.handle` for methods and `handle` for free functions. That's the
  one piece of information the old method-decorator gave you (you knew it
  was on a class because of how you decorated it) that we'd otherwise lose.

- **`%r` formatting for args / kwargs / return values.** `repr()` is far
  more useful than `str()` in logs because it round-trips for primitive
  types and shows `''` vs `None` clearly. You will occasionally see a
  noisy repr; the cure is to define a sane `__repr__` on your model
  classes, not to decode log lines guessing whether the empty string was
  empty or absent.

- **RAISE always uses `logger.exception(...)`,** not the level you passed
  to `log_func_call`. An unhandled exception escaping a function is by
  definition exceptional; it should not be filed at DEBUG even if you
  configured DEBUG-level entry/exit logs.

## Migration

If you're migrating from the old API:

```python
# before
func_log = FunctionLogger(logger=log)

@func_log.log_func()
def add(x, y): ...

# after
@log_func_call(log)
def add(x, y): ...
```

```python
# before
method_log = ClassFunctionLogger()

class Service:
    def __init__(self):
        self._logger = log
    @method_log.log_func()
    def handle(self): ...

# after
class Service:
    @log_func_call(log)
    def handle(self): ...
```

There is no compatibility shim. The library is still pre-1.0 and the
breakage is intentional.
