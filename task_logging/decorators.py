"""`@log_call` decorator: log entry args, exit value, and elapsed time.

Works on plain functions and on instance / class / static methods alike. The
logger is supplied explicitly at decoration time (or auto-resolved from the
wrapped function's module), so the decorator imposes no requirements on the
class it decorates.
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
        # Resolve the logger once at decoration time. Falling back to the
        # function's own module mirrors the stdlib "logging.getLogger(__name__)"
        # convention.
        bound_logger = logger or logging.getLogger(func.__module__)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            bound_logger.log(
                level,
                "ENTER %s args=%r kwargs=%r",
                func.__qualname__,
                args,
                kwargs,
            )
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
            except Exception:
                elapsed_ms = (time.perf_counter() - start) * 1000
                bound_logger.exception(
                    "RAISE %s after %.3fms", func.__qualname__, elapsed_ms
                )
                raise
            elapsed_ms = (time.perf_counter() - start) * 1000
            bound_logger.log(
                level,
                "EXIT %s return=%r cost_ms=%.3f",
                func.__qualname__,
                result,
                elapsed_ms,
            )
            return result

        return wrapper

    return decorator
