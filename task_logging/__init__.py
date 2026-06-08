"""Task-aware structured logging primitives for distributed Python services.

The library deliberately does NOT provide a one-call setup function. Wiring
up `logging.Logger` → handler → filter → formatter is stdlib's job, and
hiding it behind a wrapper would force decisions that belong to the user
(which handler? which stream? which logger? idempotent or not?). Compose
the primitives yourself; the README quick-start shows the canonical
six-line recipe.

What this library provides:
    task_log_context     - bind a dict of log attrs to the current ctx
                           (supports both `with` and explicit enter/exit)
    get_task_log_attrs   - read the currently-active task log attrs
    log_func_call        - decorator: log enter/exit/timing for any function
    TaskLogFilter        - logging.Filter that merges global + context attrs
                           onto every record. Attach to a HANDLER, not a logger.
    JsonFormatter        - logging.Formatter emitting one stdlib-named JSON
                           line per record, ready for Alloy / Loki.

Pipeline (a typical deployment):
    app  ──stdlib logging──▶  stdout (JSON)  ──Alloy scrape──▶  Loki  ──▶  Grafana
"""

from .context import get_task_log_attrs, task_log_context
from .decorators import log_func_call
from .filters import TaskLogFilter
from .formatters import JsonFormatter

__all__: list[str] = [
    "JsonFormatter",
    "TaskLogFilter",
    "get_task_log_attrs",
    "log_func_call",
    "task_log_context",
]
