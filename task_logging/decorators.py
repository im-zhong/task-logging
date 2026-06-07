"""Decorators for zero-boilerplate enter/exit/timing logging.

Both decorators sit on top of stdlib `logging.Logger` so they integrate
naturally with `setup_logging()` and the rest of the pipeline.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class FunctionLogger:
    """Decorator factory that logs entry args, exit value, and elapsed time.

    Use for module-level functions. For methods, use `ClassFunctionLogger`.

    Example:
        >>> log = logging.getLogger(__name__)
        >>> func_log = FunctionLogger(logger=log)
        >>>
        >>> @func_log.log_func()
        ... def add(x: int, y: int) -> int:
        ...     return x + y
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def log_func(
        self, level: int = logging.INFO
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            @functools.wraps(func)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                self._logger.log(
                    level,
                    "ENTER %s args=%r kwargs=%r",
                    func.__name__,
                    args,
                    kwargs,
                )
                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                except Exception:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    self._logger.exception(
                        "RAISE %s after %.3fms", func.__name__, elapsed_ms
                    )
                    raise
                elapsed_ms = (time.perf_counter() - start) * 1000
                self._logger.log(
                    level,
                    "EXIT %s return=%r cost_ms=%.3f",
                    func.__name__,
                    result,
                    elapsed_ms,
                )
                return result

            return wrapper

        return decorator


class ClassFunctionLogger:
    """Decorator factory for instance methods.

    Pulls a `logging.Logger` out of an attribute on `self` (default `_logger`),
    so different instances can route logs to different loggers if you want.
    If the attribute is missing, the method is called without logging — handy
    for opting individual instances out.

    Example:
        >>> method_log = ClassFunctionLogger()  # uses self._logger
        >>>
        >>> class Service:
        ...     def __init__(self) -> None:
        ...         self._logger = logging.getLogger(__name__)
        ...
        ...     @method_log.log_func()
        ...     def handle(self, payload: dict) -> None:
        ...         ...
    """

    def __init__(self, logger_attr: str = "_logger") -> None:
        self._logger_attr = logger_attr

    def log_func(
        self, level: int = logging.INFO
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            @functools.wraps(func)
            def wrapper(self_obj: Any, *args: Any, **kwargs: Any) -> R:
                logger = getattr(self_obj, self._logger_attr, None)
                if not isinstance(logger, logging.Logger):
                    return func(self_obj, *args, **kwargs)

                logger.log(
                    level,
                    "ENTER %s args=%r kwargs=%r",
                    func.__name__,
                    args,
                    kwargs,
                )
                start = time.perf_counter()
                try:
                    result = func(self_obj, *args, **kwargs)
                except Exception:
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    logger.exception(
                        "RAISE %s after %.3fms", func.__name__, elapsed_ms
                    )
                    raise
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.log(
                    level,
                    "EXIT %s return=%r cost_ms=%.3f",
                    func.__name__,
                    result,
                    elapsed_ms,
                )
                return result

            return wrapper  # type: ignore[return-value]

        return decorator
