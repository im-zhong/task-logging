import os
import socket
import threading

from task_logging import (
    OneTaskLog,
    TaskLoggerFactory,
)

from .simple_task_logger_database import SimpleTaskLoggingDatabase

task_logging_db = SimpleTaskLoggingDatabase()
task_logger_factory = TaskLoggerFactory(task_logging_db=task_logging_db)


def test_log_context() -> None:
    service1_name = "TestService"
    task_id = "test-task-id"

    # 我发现我们测试的时候，我们必须借助数据库才能测试
    # 这明显不对啊
    # 生成日志，和插入日志应该是两个函数才对
    # 这样我们就可以不借助数据库来测试日志是否正常生成了

    db = task_logging_db
    db.delete_all_logs(service_name=service1_name, task_id=task_id)

    logger = task_logger_factory.new(service_name=service1_name, task_id=task_id)
    # ok! 我们写的是对的！
    logger.info("info message")
    line_no = 30
    # 从数据库中把值读出来
    # 然后看看对不对

    task_logs = db.get_all_logs(service_name=service1_name, task_id=task_id)
    assert len(task_logs) == 1
    assert task_logs[0].level == "INFO"
    assert task_logs[0].message == "info message"
    assert task_logs[0].ctx_msg is not None
    print(task_logs[0].ctx_msg)
    assert task_logs[0].ctx_msg.filename == __file__
    assert task_logs[0].ctx_msg.line_no == line_no
    assert task_logs[0].ctx_msg.function_name == "test_log_context"
    assert task_logs[0].ctx_msg.module_name == __name__
    assert task_logs[0].ctx_msg.process_id == os.getpid()
    assert task_logs[0].ctx_msg.thread_id == threading.get_ident()
    assert task_logs[0].ctx_msg.thread_name == threading.current_thread().name
    assert task_logs[0].ctx_msg.hostname == socket.gethostname()


def test_task_logger() -> None:
    service1_name = "TestService1"
    service2_name = "TestService2"
    task_id = "test-task-id"
    # 我们需要实现一个函数，删掉某个task_id对应的所有日志
    db = task_logging_db
    db.delete_all_logs(service_name=service1_name, task_id=task_id)
    db.delete_all_logs(service_name=service2_name, task_id=task_id)
    # 确保我们通过上面的函数确实插入了数据/  n hnh

    # 这TM乱的！到底叫taskid还是taskname？
    # taskid是真实存在的东西，而且这个就是task scheduler的task id
    # 要不我们用雪花id
    # 因为只看taskid看不到时间戳啊
    # 不过因为有专门的表来存taskid和taskname的对应关系 所以也没有必要
    # 就是uuid吧
    # TODO
    # 咱们应该按照日志级别来判断是否应该有异常的栈和异常的类型
    # 比如大于等于error的就应该有额外的参数，用来写错误类型和错误栈
    logger1 = task_logger_factory.new(service_name=service1_name, task_id=task_id)
    logger1.info("info message")
    logger1.error("error message")
    logger1.warning("warning message")
    logger1.debug("debug message")
    logger1.critical("critical message")

    logger2 = task_logger_factory.new(service_name=service2_name, task_id=task_id)
    logger2.info("info message")
    logger2.error("error message")
    logger2.warning("warning message")
    logger2.debug("debug message")
    logger2.critical("critical message")

    logs_len = 5
    # 使用数据库的API来查询
    # 为了实现测试，我们需要实现删除的API
    task_logs: list[OneTaskLog] = db.get_all_logs(
        service_name=service1_name, task_id=task_id
    )
    assert len(task_logs) == logs_len

    # 我们还要看看日志的内容是不是和我们插入的一样
    # assert task_logs.task_id == task_id
    # assert task_logs.service_name == service1_name
    assert task_logs[0].level == "INFO"
    assert task_logs[0].message == "info message"
    assert task_logs[1].level == "ERROR"
    assert task_logs[1].message == "error message"
    assert task_logs[2].level == "WARNING"
    assert task_logs[2].message == "warning message"
    assert task_logs[3].level == "DEBUG"
    assert task_logs[3].message == "debug message"
    assert task_logs[4].level == "CRITICAL"
    assert task_logs[4].message == "critical message"

    # 咱们把service1的给删了 看看会不会影响service2
    db.delete_all_logs(service_name=service1_name, task_id=task_id)
    task_logs = db.get_all_logs(service_name=service1_name, task_id=task_id)
    assert len(task_logs) == 0

    task_logs = db.get_all_logs(service_name=service2_name, task_id=task_id)
    assert len(task_logs) == logs_len


def raise_zero_division_error() -> tuple[int, int, float]:
    a = 1
    b = 2
    return a, b, 1 / 0


def test_exception_log() -> None:
    service1_name = "TestService"
    task_id = "test-task-id"
    db = task_logging_db
    db.delete_all_logs(service_name=service1_name, task_id=task_id)

    # 这里要主要的触发一个异常
    # 然后捕获

    logger = task_logger_factory.new(service_name=service1_name, task_id=task_id)
    try:
        raise_zero_division_error()
    except ZeroDivisionError:
        logger.exception("ExceptionDetails")

    task_exceptions = db.get_all_logs(service_name=service1_name, task_id=task_id)
    assert len(task_exceptions) == 1
    assert task_exceptions[0].exc_msg is not None
    assert task_exceptions[0].exc_msg.name == "ZeroDivisionError"
    assert task_exceptions[0].exc_msg.details == "division by zero"
    assert task_exceptions[0].exc_msg.stack_trace is not None
    assert task_exceptions[0].exc_msg.locals_dict is not None
    assert task_exceptions[0].exc_msg.locals_dict == {"a": "1", "b": "2"}
    # assert task_exceptions[0].stack_trace == "StackTrace"
    # 也可以
    print("-----------------------")
    print(task_exceptions[0].exc_msg.stack_trace)
    # 我感觉日志的时间不对，测试用的sqlite，换成pgsql应该就没这个问题了
    print(task_exceptions[0].logged_at)
