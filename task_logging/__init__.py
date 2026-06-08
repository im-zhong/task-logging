"""Task-aware structured logging for distributed Python services.

Pipeline:  app  ──stdlib logging──▶  stdout (JSON)  ──Alloy scrape──▶  Loki  ──▶  Grafana

Public API:
    setup_task_logging   - install handlers/filters on the root logger
    task_log_context     - bind a dict of log attrs to the current ctx
                           (supports both `with` and explicit enter/exit)
    get_task_log_attrs   - read the currently-active task log attrs
    log_func_call        - decorator: log enter/exit/timing for any function
    TaskLogFilter        - the underlying logging.Filter
    JsonFormatter        - the underlying JSON logging.Formatter
"""

from .context import get_task_log_attrs, task_log_context
from .decorators import log_func_call
from .filters import TaskLogFilter
from .formatters import JsonFormatter
from .setup import setup_task_logging

__all__: list[str] = [
    "JsonFormatter",
    "TaskLogFilter",
    "get_task_log_attrs",
    "log_func_call",
    "setup_task_logging",
    "task_log_context",
]
