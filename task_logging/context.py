"""Task context propagation via contextvars.

`task_id` (and any extra fields you bind) flow through threads, asyncio tasks,
and decorated function calls, then get attached to every `LogRecord` by
`TaskContextFilter`.

Why ContextVar (not threading.local, not LoggerAdapter, not extra=...):
    - threading.local breaks under asyncio: all coroutines on one event loop
      share a thread, so they'd share the same task_id.
    - LoggerAdapter requires every log site to use a special logger instance.
      It can't capture third-party libraries (`requests`, `urllib3`, ...) that
      call logging.getLogger("urllib3") themselves.
    - extra={"task_id": ...} on every log call has the same problem and is
      pure boilerplate.
    ContextVars cover all three (threads, asyncio, opaque to user code) and
    are the stdlib's blessed mechanism for "ambient request-scoped state."

See docs/design/task-context.md for the full design discussion.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# A single ContextVar holds the entire context dict so we can swap it atomically
# in `task_context()` and restore it on exit via the token returned by .set().
# Per-key ContextVars would also work, but a single var keeps the bookkeeping
# simple and lets nested task_context() calls inherit the parent's keys with a
# single dict merge.
#
# Default is None (not {}) — ContextVars MUST NOT have mutable defaults. The
# default is shared across every context that never calls .set(), so a mutable
# default would let one mutation leak into every other thread/task that has
# never bound a context. ruff's B039 rule flags this exact bug. We allocate a
# fresh dict every set() and treat None as "no context active."
_task_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "task_logging_ctx", default=None
)


def get_task_context() -> dict[str, Any]:
    """Return a shallow copy of the current task context."""
    current = _task_ctx.get()
    return dict(current) if current else {}


def get_task_id() -> str | None:
    """Return the current `task_id`, or None if no context is active."""
    current = _task_ctx.get()
    if not current:
        return None
    value = current.get("task_id")
    return value if isinstance(value, str) else None


@contextmanager
def task_context(
    task_id: str | None = None, **extra: Any
) -> Iterator[dict[str, Any]]:
    """Bind a task context for the duration of the `with` block.

    Any logs emitted inside the block — including from third-party libraries
    that use stdlib logging — will be tagged with these fields.

    Args:
        task_id: The task id. If None, a uuid4 hex is generated.
        **extra: Arbitrary extra fields to attach to every log record
                 (e.g. `user_id="u-1"`, `request_id="r-9"`).

    Yields:
        The merged context dict that is now active.

    Example:
        >>> with task_context(task_id="task-42", user_id="u-1"):
        ...     logging.getLogger(__name__).info("processing")
    """
    # Inheritance: a nested task_context inherits everything its parent set.
    # This matches user intent — a sub-task should see the request-scoped
    # fields the outer task bound (region, user_id, ...) unless it explicitly
    # overrides them. The inner task_id wins because dict-merge is left-to-right.
    parent = _task_ctx.get() or {}
    merged: dict[str, Any] = {**parent, **extra}
    merged["task_id"] = task_id if task_id is not None else uuid.uuid4().hex
    # ContextVar.set returns an opaque token that records EXACTLY what was
    # there before. reset(token) restores precisely that, even under nesting
    # and even when the body raises (the finally always runs). This is the
    # stdlib's blessed pattern for save/restore; rolling our own would break
    # under reentrancy.
    token = _task_ctx.set(merged)
    try:
        yield merged
    finally:
        _task_ctx.reset(token)


def bind_task_context(**extra: Any) -> contextvars.Token[dict[str, Any] | None]:
    """Bind extra fields onto the current task context, returning a reset token.

    Useful when you cannot use a `with` block (e.g. middleware that binds at
    request start and unbinds in a separate teardown hook). Pair every
    `bind_task_context()` call with `unbind_task_context(token)`.
    """
    parent = _task_ctx.get() or {}
    return _task_ctx.set({**parent, **extra})


def unbind_task_context(token: contextvars.Token[dict[str, Any] | None]) -> None:
    """Restore the task context to what it was before `bind_task_context`."""
    _task_ctx.reset(token)
