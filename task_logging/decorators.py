"""`@log_call` decorator: log entry args, exit value, and elapsed time.

Works on plain functions and on instance / class / static methods alike. The
logger is supplied explicitly at decoration time (or auto-resolved from the
wrapped function's module), so the decorator imposes no requirements on the
class it decorates.

Why one decorator (not separate FunctionLogger / ClassFunctionLogger):
    The earlier two-class split existed because TaskLogger bound
    (service, task_id) to the logger instance, so methods needed a
    per-instance logger via self._logger. The contextvars-based rewrite moved
    task_id off the logger entirely (it now flows through ContextVar), so
    that whole motivation disappeared. Forcing classes to expose self._logger
    became pure coupling — boilerplate, attribute-name collisions, and a
    silent no-op when the attribute is missing — solving a problem that no
    longer exists.

See docs/design/decorators.md for the full reasoning.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


def log_call(
    logger: logging.Logger | None = None,
    *,
    level: int = logging.INFO,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wrap a callable so each invocation emits ENTER / EXIT / RAISE records.

    The wrapper logs:
        - ``ENTER <name> args=... kwargs=...`` before the call,
        - ``EXIT  <name> return=... cost_ms=...`` after a successful return,
        - ``RAISE <name> after <ms>ms`` (with ``exc_info``) on exception, then
          re-raises.

    Args:
        logger: The logger to write to. If ``None``, the decorator resolves
            ``logging.getLogger(func.__module__)`` lazily at decoration time —
            i.e. it follows the stdlib "one logger per module" idiom.
        level: Log level for ENTER / EXIT records. RAISE always uses
            ``logging.ERROR`` (via ``logger.exception``).

    Examples:
        Plain function::

            log = logging.getLogger(__name__)

            @log_call(log)
            def add(x: int, y: int) -> int:
                return x + y

        Method — no special class setup required, no `self._logger`::

            class Service:
                @log_call(log)
                def handle(self, payload: dict) -> None: ...

        Auto-resolve logger from the function's module::

            @log_call()  # uses logging.getLogger(__name__) of the caller
            def compute(): ...

        Custom level::

            @log_call(log, level=logging.DEBUG)
            def chatty(): ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        # Resolve the logger ONCE at decoration time, not on every call.
        # Falling back to func.__module__ mirrors the stdlib idiom
        # `logging.getLogger(__name__)` — same logger you'd get if you
        # had written `log = logging.getLogger(__name__)` at the top of
        # the module the function lives in.
        bound_logger = logger or logging.getLogger(func.__module__)

        # @functools.wraps(func) makes `wrapper` IMPERSONATE `func` for
        # introspection. Without it, `wrapper` is a brand-new function object
        # and every tool that asks "what is this callable?" gets `wrapper`'s
        # answers instead of `func`'s:
        #
        #   - tracebacks blame "wrapper" instead of the real function name
        #   - record.funcName (the LogRecord attr stdlib auto-populates from
        #     the call site) becomes "wrapper" for every decorated function
        #     in the codebase
        #   - help(decorated_fn), pydoc, Sphinx, IDE hover — all show the
        #     wrapper's empty docstring instead of the real one
        #   - inspect.signature(decorated_fn) sees `(*args, **kwargs)` instead
        #     of the real (x: int, y: int) -> int signature
        #   - serialization tools that look up callables by __qualname__ break
        #
        # `functools.wraps` is sugar for functools.update_wrapper, which
        # copies __module__, __name__, __qualname__, __annotations__, __doc__,
        # and updates __dict__ from func onto wrapper, plus sets
        # wrapper.__wrapped__ = func so inspect.signature() can see through
        # the wrapper transparently.
        #
        # This decorator REFERENCES func.__qualname__ in its log messages
        # explicitly, so the messages themselves don't depend on @wraps —
        # but record.funcName, traceback frames, and every other consumer
        # of the wrapper's identity DO depend on it. Don't remove this line
        # "to clean up imports."
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            # __qualname__ (not __name__) so methods show up as
            # "Service.handle" instead of just "handle". This is the one
            # piece of class-context the old ClassFunctionLogger gave us
            # implicitly; we keep it without requiring class setup.
            bound_logger.log(
                level,
                "ENTER %s args=%r kwargs=%r",
                func.__qualname__,
                args,
                kwargs,
            )
            # perf_counter is monotonic — immune to NTP adjustments and
            # daylight-saving jumps that would corrupt elapsed-time math
            # if we used time.time().
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception:
                elapsed_ms = (time.perf_counter() - start) * 1000
                # RAISE always uses logger.exception (== ERROR level + exc_info)
                # regardless of the user's chosen `level`. An unhandled
                # exception escaping a function is by definition exceptional;
                # filing it at DEBUG just because entry/exit logs are at DEBUG
                # would be wrong.
                bound_logger.exception(
                    "RAISE %s after %.3fms", func.__qualname__, elapsed_ms
                )
                # Re-raise: the decorator is a passive observer, not a
                # swallower. Callers must still see the exception.
                raise
            elapsed_ms = (time.perf_counter() - start) * 1000
            bound_logger.log(
                level,
                # %r (repr) for args/kwargs/return values: round-trips for
                # primitives, distinguishes '' from None, and surfaces useful
                # detail for objects with a sane __repr__.
                "EXIT %s return=%r cost_ms=%.3f",
                func.__qualname__,
                result,
                elapsed_ms,
            )
            return result

        return wrapper

    return decorator
