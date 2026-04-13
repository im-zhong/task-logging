import time

from task_logging import FunctionLogger, TaskLogger, TaskLoggerFactory

from .simple_task_logger_database import SimpleTaskLoggingDatabase

task_logging_db = SimpleTaskLoggingDatabase()
task_logger_factory = TaskLoggerFactory(task_logging_db=task_logging_db)

# 直接构建TaskLogger不太好，咱们写一个get_task_logger
task_logger: TaskLogger = task_logger_factory.new(
    service_name="TestService", task_id="test-task-id"
)

func_logger = FunctionLogger(logger=task_logger)


# TODO： 这个装饰器怎么没有level呀，得加上这个参数啊，可以带一个默认的级别呗，info
# ok, 用一种非常丑陋的方法实现了这个功能
@func_logger.log_func()
def add(x: int, y: int) -> int:
    time.sleep(3)
    return x + y


def test_func_logger() -> None:
    db = task_logging_db
    db.delete_all_logs(service_name="TestService", task_id="test-task-id")

    # 可以看到，这里日志记录的是我们的log_func的位置信息
    # 这显然不是我们想要的，我们想要的是我们的add函数的位置信息
    # 我需要看看装饰器的实际的调用栈是什么样子，可能需要一些特殊的处理
    add(1, 2)

    task_logs = db.get_all_logs(service_name="TestService", task_id="test-task-id")
    len_logs = 2
    assert len(task_logs) == len_logs
    print(task_logs)
