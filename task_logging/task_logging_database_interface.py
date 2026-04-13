from abc import ABC, abstractmethod

from .models import OneTaskLog, TaskLogIn


class TaskLoggingDatabaseInterface(ABC):
    @abstractmethod
    def append_task_log(
        self, service_name: str, task_id: str, task_log: TaskLogIn
    ) -> None:
        pass

    @abstractmethod
    def get_all_logs(self, service_name: str, task_id: str) -> list[OneTaskLog]:
        pass

    @abstractmethod
    def delete_all_logs(self, service_name: str, task_id: str) -> None:
        pass
