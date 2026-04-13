import functools
import inspect
import logging
import os
import socket
import sys
import threading

# 就像一个门卫一样，守在函数的入口和出口，记录函数的执行时间和返回值
import time
import traceback
from collections.abc import Callable
from logging import Logger
from types import TracebackType
from typing import Any, ParamSpec, TypeVar, cast

from .models import ContextLogMessage, ExceptionLogMessage, TaskLogIn
from .task_logging_database_interface import TaskLoggingDatabaseInterface

P = ParamSpec("P")  # Represents all parameters
R = TypeVar("R")  # Represents return value

# 感觉这个不好使啊
# 因为每次都要在参数里面加上一个logger
# 应该可以实现一个 FunctionLogger
# 然后它的参数是一个logger
# 然后它有一个log的装饰器


class FunctionLogger:
    def __init__(self, logger: Logger) -> None:
        self._logger: Logger = logger

    # 必须定义在这个模块里面，才能正常工作！
    # TODO：日志级别
    def log_func(
        self, level: int = logging.INFO
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        def log_func_impl(func: Callable[P, R]) -> Callable[P, R]:
            """
            A decorator that logs the input parameters and return value of a function.
            """

            @functools.wraps(wrapped=func)
            def wrapper(*args, **kwargs) -> Any:  # type: ignore
                # Log function call with parameters
                # The stacklevel parameter is designed to specify how many levels up the stack to look to find the source location of the logging call. By setting stacklevel=2, the logger will report the location of the call to the decorated function, not the location inside the decorator.
                # 日志一定要选择既精简，又遍于阅读 搜索的方式
                # 比如，这里有几种风格
                # ENTER fund_name
                # [ENTER] func_name
                # ENTER [func_name]
                # 额外的那个括号不仅没有什么用，还会让搜索变得复杂，比如我只想搜索ENTER 我只想搜索func_name
                # 但是如果我想同时搜索 ENTER func_name就会变的复杂，因为[]在regex中是特殊字符，
                # 单纯的大写已经足够醒目了，不需要额外的符号

                # 现在要根据level来选择调用不同的日志函数，哦 fuck
                # 这个要怎么写
                # 这个函数可以写在 先不急

                self._logger.log(
                    level=level,
                    msg=f"Enter {func.__name__}, args: {args}, kwargs: {kwargs}.",
                    # stacklevel=2,
                )

                start_time = time.time()  # Capture start time
                result = func(*args, **kwargs)
                end_time = time.time()

                # Log function return value
                execution_ms = (end_time - start_time) * 1000
                self._logger.log(
                    level=level,
                    msg=f"EXIT {func.__name__}, return: {result}, cost: {execution_ms:.3f} ms.",
                    # stacklevel=2,
                )
                return result

            return wrapper

        return log_func_impl


class ClassFunctionLogger:
    """
    A decorator factory for logging class methods that have access to a class logger instance.
    This decorator will use the class's logger attribute to log method entry and exit.
    """

    def __init__(self, logger_attr: str = "_logger") -> None:
        """
        Initialize with the name of the logger attribute in the class.

        Args:
            logger_attr: The attribute name of the logger in the class (default: "_logger")
        """
        self._logger_attr = logger_attr

    def log_func(
        self, level: int = logging.INFO
    ) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """
        Create a decorator that logs method calls using the class's logger.

        Args:
            level: The logging level to use

        Returns:
            A decorator function
        """

        def decorator(func: Callable[P, R]) -> Callable[P, R]:
            """
            Decorator that logs the input parameters and return value of a class method.
            """

            @functools.wraps(wrapped=func)
            def wrapper(self_obj: Any, *args: P.args, **kwargs: P.kwargs) -> R:
                # Get the logger from the class instance
                # If not found, just call the function normally
                if not hasattr(self_obj, self._logger_attr):
                    return func(self_obj, *args, **kwargs)

                logger = getattr(self_obj, self._logger_attr)

                # Log method entry
                logger.log(
                    level=level,
                    msg=f"ENTER {func.__name__}, args: {args}, kwargs: {kwargs}.",
                )

                # Execute the method and measure execution time
                start_time = time.time()
                result = func(self_obj, *args, **kwargs)
                execution_ms = (time.time() - start_time) * 1000

                # Log method exit
                logger.log(
                    level=level,
                    msg=f"EXIT {func.__name__}, return: {result}, cost: {execution_ms:.3f} ms.",
                )
                return result

            return wrapper  # type: ignore

        return decorator


# TODO: 咱们把service给改成module名字？
class TaskLogger(logging.Logger):
    # python的logger本来就有名字
    def __init__(
        self,
        task_logging_db: TaskLoggingDatabaseInterface,
        task_id: str,
        service_name: str,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(name=service_name, level=level)
        self._service_name: str = service_name
        self._task_id: str = task_id
        self._db = task_logging_db
        # 插入日志到数据库
        # 标准库有关于日志级别的定义，那咱们就用这个了，也不需要自己来定义

    def debug(self, msg, *args, **kwargs) -> None:  # type: ignore
        super().debug(msg, *args, **kwargs)
        self._append_task_log(level=logging.DEBUG, message=msg)

    def info(self, msg, *args, **kwargs) -> None:  # type: ignore
        super().info(msg, *args, **kwargs)
        self._append_task_log(level=logging.INFO, message=msg)

    def warning(self, msg, *args, **kwargs) -> None:  # type: ignore
        super().warning(msg, *args, **kwargs)
        self._append_task_log(level=logging.WARNING, message=msg)

    # warn is deprecated

    def error(self, msg, *args, **kwargs) -> None:  # type: ignore
        super().error(msg, *args, **kwargs)
        self._append_task_log(level=logging.ERROR, message=msg)

    def exception(self, msg, *args, exc_info=True, **kwargs) -> None:  # type: ignore
        """
        Convenience method for logging an ERROR with exception information.
        """
        self.error(msg, *args, exc_info=exc_info, **kwargs)

    def critical(self, msg, *args, **kwargs) -> None:  # type: ignore
        super().critical(msg, *args, **kwargs)
        self._append_task_log(level=logging.CRITICAL, message=msg)

    def fatal(self, msg, *args, **kwargs) -> None:  # type: ignore
        """
        Don't use this method, use critical() instead.
        """
        self.critical(msg, *args, **kwargs)

    def log(self, level, msg, *args, **kwargs) -> None:  # type: ignore
        super().log(level, msg, *args, **kwargs)
        self._append_task_log(level=level, message=msg)

    # 其实都这样了，不如直接用一个函数来实现
    # 但是不行，我们的函数必须是本模块的
    # 不然get context就无法正常工作了

    # 如果我把迭代器定义在这里怎么样？

    # 咱们要不用一个Model来定义一下Exception
    # 防止以后还想加东西，结果就要改函数签名

    # 现在咱们可以去掉这两个函数
    #
    # def error_with_exception(
    #     self, msg: str, exc_msg: ExceptionLogMessage, *args, **kwargs
    # ) -> None:
    #     self._append_exceptional_task_log(
    #         level=logging.ERROR,
    #         msg=msg,
    #         exc_msg=exc_msg,
    #     )
    #     super().critical(msg, *args, **kwargs)

    # def critical_with_exception(
    #     self, msg: str, esc_msg: ExceptionLogMessage, *args, **kwargs
    # ) -> None:
    #     self._append_exceptional_task_log(
    #         level=logging.CRITICAL,
    #         msg=msg,
    #         exc_msg=esc_msg,
    #     )
    #     super().critical(msg, *args, **kwargs)

    def _append_task_log(self, level: int, message: str) -> None:
        ctx_msg = self._get_context()
        exc_msg = self._get_exception_log_message()

        if self.isEnabledFor(level=level):
            self._db.append_task_log(
                service_name=self._service_name,
                task_id=self._task_id,
                task_log=TaskLogIn(
                    level=logging.getLevelName(level=level),
                    message=message,
                    ctx_msg=ctx_msg,
                    exc_msg=exc_msg,
                ),
            )

    # def _append_exceptional_task_log(
    #     self, level: int, msg: str, exc_msg: ExceptionLogMessage
    # ) -> None:
    #     self._db.append_exception_task_log(
    #         service_name=self._service_name,
    #         task_id=self._task_id,
    #         log_level=logging.getLevelName(level=level),
    #         message=msg,
    #         exc_msg=exc_msg,
    #     )

    # 这里应该返回异常信息的类就行了
    def _get_exception_log_message(self) -> ExceptionLogMessage | None:
        """
        在 except 块中调用时，返回包含异常实例、类名和完整错误堆栈的字典。
        若不在异常上下文中调用，抛出 ValueError。
        """
        # # 获取当前异常信息
        exc_type, exc_value, exc_traceback = sys.exc_info()

        if not exc_type:
            return None

        # # 确保在异常上下文中调用
        # if exc_type is None:
        #     # 倒也不用
        #     # 如果不是在异常上下文中，那么就不管这个了
        #     # raise ValueError("log_error() must be called within an except block")
        #     return None

        # # 生成完整的错误堆栈字符串
        stack_trace = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )

        """
        获取包含异常信息及触发点上下文的日志消息
        返回包含以下信息的对象：
        - 异常类名
        - 异常详细信息
        - 完整堆栈跟踪
        - 异常触发点的全局变量（快照）
        - 异常触发点的局部变量（快照）
        """
        # exc_type, exc_value, exc_traceback = sys.exc_info()

        # 获取最底层的异常堆栈帧
        deepest_traceback = exc_traceback
        while cast(TracebackType, deepest_traceback).tb_next:
            deepest_traceback = cast(TracebackType, deepest_traceback).tb_next

        # 获取异常触发点的帧信息
        exception_frame = cast(TracebackType, deepest_traceback).tb_frame

        # 获取触发点的变量信息（转换为字典避免引用问题）
        # frame_globals = (
        #     dict(exception_frame.f_globals) if exception_frame.f_globals else {}
        # )
        frame_locals: dict[str, Any] = (
            dict(exception_frame.f_locals) if exception_frame.f_locals else {}
        )

        # 把这个frame全部变成str
        # 因为有些变量是无法json化的 直接写入 pydantic model 话，在执行json化的时候会报错
        frame_locals_str = {k: repr(v) for k, v in frame_locals.items()}

        # print("-----------------------------------------")
        # # globals几乎没用，而且很长很长
        # print("frame_globals:", frame_globals)
        # # frame_locals: {'a': 1, 'b': 2} 这个非常有用了
        # print("frame_locals:", frame_locals)
        # print("-----------------------------------------")

        # return ExceptionLogMessage(
        #     name=exc_type.__name__,
        #     details=str(exc_value),
        #     stack_trace="".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
        #     globals=frame_globals,
        #     locals=frame_locals
        # )

        return ExceptionLogMessage(
            name=exc_type.__name__,
            details=str(exc_value),
            stack_trace=stack_trace,
            # locals_dict=frame_locals,
            locals_dict=frame_locals_str,
        )

    # 这个函数不容易测试，因为只有从log进行测试才能成功
    # 这个设计如果可以改进一下就好了
    # 我知道了，应该实现一个函数
    # 教过 get context
    def _get_context(self) -> ContextLogMessage:
        # 不断的向上寻找
        # 知道找到
        stacks = inspect.stack(context=0)
        # 从上往下找 调用栈是随着调用顺序从下往上的
        # 所以我们从上往下找
        # 不管怎么说
        # 调用我们的函数就是logger里面的那几个
        # 哪些接口是固定的

        ctx_msg = ContextLogMessage(
            hostname=socket.gethostname(),
            process_id=os.getpid(),
            thread_name=threading.current_thread().name,
            filename="",
            module_name="",
            function_name="",
            line_no=-1,
            # Thread identifier of this thread or None if it has not been started.
            #
            # This is a nonzero integer. See the get_ident() function. Thread
            # identifiers may be recycled when a thread exits and another thread is
            # created. The identifier is available even after the thread has exited.
            thread_id=threading.current_thread().ident or 0,
            stack_depth=len(stacks),
            # function_args="",
        )

        logger_module = self.__class__.__module__  # 获取Logger所在模块名

        # 遍历调用栈，寻找第一个非Logger模块的栈帧
        for frame_info in inspect.stack(context=0):
            frame_module = frame_info.frame.f_globals.get("__name__", "")
            if frame_module == logger_module:
                continue  # 跳过Logger自身模块的帧

            # 找到调用者帧，填充信息
            ctx_msg.filename = frame_info.filename
            ctx_msg.module_name = frame_module
            ctx_msg.function_name = frame_info.function
            ctx_msg.line_no = frame_info.lineno
            break

        # for i, stack in enumerate(stacks):
        #     if stack.function in [
        #         "info",
        #         "error",
        #         "warning",
        #         "debug",
        #         "critical",
        #     ]:
        #         # 如果下一层是wrapper 那就就下去两层

        #         # 这个时候下一层就是
        #         if i + 1 >= len(stacks):
        #             break

        #         frame = stacks[i + 1]
        #         if frame.function == "wrapper" and i + 2 < len(stacks):
        #             frame = stacks[i + 2]
        #         # # print(frame)
        #         # print(frame.filename)
        #         # print(frame.function)
        #         # print(frame.lineno)
        #         # print(frame.frame.f_globals["__name__"])
        #         # # 我保留可以获得全部局部变量的能力，就先不写了
        #         # # 等0.2.0 写成 enter exit log
        #         # # 这个其实也不重要了
        #         # print(frame.frame.f_locals)
        #         ctx_msg.module_name = frame.frame.f_globals["__name__"]
        #         ctx_msg.function_name = frame.function
        #         ctx_msg.line_number = frame.lineno
        #         ctx_msg.filename = frame.filename

        #         break
        # # else:
        # # 找不到函数的调用信息
        # # 那么就只天蝎hostnane pid tid 即可

        return ctx_msg

        # 这个函数大概是通过栈来实现的


class TaskLoggerFactory:
    def __init__(self, task_logging_db: TaskLoggingDatabaseInterface) -> None:
        self._task_logging_db = task_logging_db

    def new(self, service_name: str, task_id: str) -> TaskLogger:
        return TaskLogger(
            task_logging_db=self._task_logging_db,
            service_name=service_name,
            task_id=task_id,
        )
