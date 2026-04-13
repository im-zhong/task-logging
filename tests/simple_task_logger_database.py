from collections import defaultdict
from datetime import UTC, datetime

from task_logging.models import OneTaskLog, TaskLogIn
from task_logging.task_logging_database_interface import TaskLoggingDatabaseInterface


class SimpleTaskLoggingDatabase(TaskLoggingDatabaseInterface):
    def __init__(self) -> None:
        # Store logs in a dictionary with (service_name, task_id) as the key
        self._logs: defaultdict[tuple[str, str], list[OneTaskLog]] = defaultdict(list)

    def append_task_log(
        self,
        service_name: str,
        task_id: str,
        task_log: TaskLogIn,
    ) -> None:
        log_entry = OneTaskLog(
            level=task_log.level,
            message=task_log.message,
            ctx_msg=task_log.ctx_msg,
            exc_msg=task_log.exc_msg,
            logged_at=datetime.now(tz=UTC),
        )
        self._logs[(service_name, task_id)].append(log_entry)

    def get_all_logs(self, service_name: str, task_id: str) -> list[OneTaskLog]:
        return self._logs.get((service_name, task_id), [])

    def delete_all_logs(self, service_name: str, task_id: str) -> None:
        if (service_name, task_id) in self._logs:
            del self._logs[(service_name, task_id)]
