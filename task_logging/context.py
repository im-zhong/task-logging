"""Task log context propagation via contextvars.

A `task_log_context` binds a dict of arbitrary log attrs (`task_id`, `user_id`,
whatever your app's domain calls for) to the current execution context. Any log
emitted inside that context — including from third-party libraries that use
stdlib logging — gets those attrs attached to its `LogRecord` via
`TaskLogFilter`.

The context object supports both forms:

    # `with` form — preferred
    with task_log_context({"task_id": "abc"}):
        log.info("...")

    # imperative enter/exit — for middleware that has separate hook callbacks
    ctx = task_log_context({"task_id": "abc"})
    ctx.enter()
    try:
        log.info("...")
    finally:
        ctx.exit()

`enter`/`exit` are explicit aliases of `__enter__`/`__exit__` — same single
object, two usage forms. The library does NOT privilege any particular attr
name (no auto-`task_id`, no domain-specific kwargs); the dict is whatever
the caller decides.

Why ContextVar (not threading.local, not LoggerAdapter, not extra=...):
    - threading.local breaks under asyncio: all coroutines on one event loop
      share a thread, so they'd share the same context.
    - LoggerAdapter requires every log site to use a special logger instance.
      It can't capture third-party libraries (`requests`, `urllib3`, ...) that
      call logging.getLogger("urllib3") themselves.
    - extra={...} on every log call has the same problem and is pure
      boilerplate.
    ContextVars cover all three (threads, asyncio, opaque to user code) and
    are the stdlib's blessed mechanism for "ambient request-scoped state."

See docs/design/task-context.md for the full design discussion.
"""

from __future__ import annotations

import contextvars
from types import TracebackType
from typing import Any

# A single ContextVar holds the entire log-attrs dict so we can swap it
# atomically and restore it on exit via the token returned by .set().
# Per-key ContextVars would also work, but a single var keeps the bookkeeping
# simple and lets nested task_log_contexts inherit the parent's keys with a
# single dict merge.
#
# Default is None (not {}) — ContextVars MUST NOT have mutable defaults. The
# default is shared across every context that never calls .set(), so a mutable
# default would let one mutation leak into every other thread/task that has
# never bound a context. ruff's B039 rule flags this exact bug. We allocate a
# fresh dict every set() and treat None as "no context active."
_local_log_attrs: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("task_logging_local_log_attrs", default=None)
)


def get_task_log_attrs() -> dict[str, Any]:
    """Return a shallow copy of the currently-active task log attrs.

    Combines all enclosing `task_log_context` blocks (inner overrides outer).
    Returns an empty dict if no context is active.
    """
    current = _local_log_attrs.get()
    return dict(current) if current else {}


class task_log_context:  # noqa: N801 (callable-named-as-class is intentional — it reads as a verb at the call site)
    """Bind a dict of log attrs to the current execution context.

    Logs emitted inside the active context — yours and third-party — will
    carry these attrs in their JSON output. Nested contexts inherit and
    override parent attrs (inner wins).

    The dict is the only positional argument and is **positional-only**: the
    public API has no privileged attr names. Pick any keys your app cares
    about (`task_id`, `request_id`, `user_id`, `region`, ...).

    Two equivalent usage forms:

        # `with` form — preferred
        with task_log_context({"task_id": "abc"}):
            log.info("processing")

        # imperative — for middleware split across hook callbacks
        ctx = task_log_context({"task_id": "abc"})
        ctx.enter()
        try:
            log.info("processing")
        finally:
            ctx.exit()

    The same instance can only be entered once. Re-entering an already-active
    context raises RuntimeError; do not share an instance across threads or
    repeated calls.
    """

    __slots__ = ("_attrs", "_token")

    def __init__(self, attrs: dict[str, Any] | None = None, /) -> None:
        # We store a defensive copy so subsequent caller mutations to `attrs`
        # don't bleed into the bound context after the fact.
        self._attrs: dict[str, Any] = dict(attrs) if attrs else {}
        self._token: contextvars.Token[dict[str, Any] | None] | None = None

    def __enter__(self) -> dict[str, Any]:
        if self._token is not None:
            msg = "task_log_context instance is already active; cannot re-enter"
            raise RuntimeError(msg)

        # Inheritance: a nested context inherits everything its parent set.
        # This matches user intent — a sub-task should see the enclosing
        # request-scoped fields (region, user_id, ...) unless it explicitly
        # overrides them. Our keys win on collision because dict-merge is
        # left-to-right.
        parent = _local_log_attrs.get() or {}
        merged: dict[str, Any] = {**parent, **self._attrs}

        # `Token` is contextvars' undo-receipt for a single set() call. It
        # records the var's INTERNAL STATE before the set — not just the
        # previous value. This distinction matters for the unset case:
        #
        #     # naive save-and-restore:
        #     old = ctx_var.get()    # var was never set, returns the default
        #     ctx_var.set("hi")
        #     ctx_var.set(old)       # now var IS explicitly set to the default,
        #                            # not "still unset"
        #
        # The two states (explicitly-set-to-default vs never-set) look the
        # same to .get() but differ when the Context is copied or run via
        # Context.run(). Token is how stdlib captures and restores the
        # precise state without that ambiguity. It also pins the token to
        # the var instance — passing token_a from var_a into var_b.reset()
        # raises at runtime, so misuse fails loudly.
        #
        # Tokens nest: every set() returns a fresh token, every reset()
        # consumes one, mirroring the call stack. The finally in __exit__
        # ensures we reset even if the body raises.
        self._token = _local_log_attrs.set(merged)
        return merged

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._token is None:
            return
        _local_log_attrs.reset(self._token)
        self._token = None

    # Explicit aliases for the imperative form. Same machinery, named so the
    # call site reads naturally without `__dunder__` access.
    def enter(self) -> dict[str, Any]:
        """Enter the context (alias for `__enter__`).

        Use when middleware splits enter/exit across separate callbacks and a
        `with` block won't fit. Otherwise prefer `with task_log_context(...)`.
        """
        return self.__enter__()

    def exit(self) -> None:
        """Exit the context (alias for `__exit__`)."""
        self.__exit__(None, None, None)
