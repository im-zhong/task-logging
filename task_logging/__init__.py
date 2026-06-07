"""Task-aware structured logging for distributed Python services.

Pipeline:  app  ──stdlib logging──▶  stdout (JSON)  ──Alloy scrape──▶  Loki  ──▶  Grafana

Public API:
    setup_logging          - install handlers/filters on the root logger
    task_context           - bind a task_id (and extras) to the current ctx
    bind_task_context      - imperative version of task_context
    unbind_task_context    - companion to bind_task_context
    get_task_id            - read the active task_id
    get_task_context       - read the full active context dict
    log_call               - decorator: log enter/exit/timing for any callable
    TaskContextFilter      - the underlying logging.Filter
    JsonFormatter          - the underlying JSON logging.Formatter
"""

from .context import (
    bind_task_context,
    get_task_context,
    get_task_id,
    task_context,
    unbind_task_context,
)
from .decorators import log_call
from .filters import TaskContextFilter
from .formatters import JsonFormatter
from .setup import setup_logging

__all__: list[str] = [
    "JsonFormatter",
    "TaskContextFilter",
    "bind_task_context",
    "get_task_context",
    "get_task_id",
    "log_call",
    "setup_logging",
    "task_context",
    "unbind_task_context",
]
