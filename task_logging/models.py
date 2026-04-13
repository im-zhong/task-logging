from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# should be same with database_service.models.task_logging
# otherwise you will have to write a lot of code to convert between them
class ExceptionLogMessage(BaseModel):
    name: str = Field(..., title="Exception Name", description="Exception Name")
    details: str = Field(
        ..., title="Exception Details", description="Exception Details"
    )
    stack_trace: str = Field(..., title="Stack Trace", description="Stack Trace")
    locals_dict: dict[str, Any] = Field(
        ..., title="Locals Dict", description="Locals Dict"
    )


class ContextLogMessage(BaseModel):
    hostname: str = Field(..., title="Hostname", description="Hostname")
    process_id: int = Field(..., title="Process ID", description="Process ID")
    thread_name: str = Field(..., title="Thread Name", description="Thread Name")
    module_name: str = Field(..., title="Module Name", description="Module Name")
    function_name: str = Field(..., title="Function Name", description="Function Name")
    line_no: int = Field(..., title="Line Number", description="Line Number")
    filename: str = Field(..., title="Filename", description="Filename")
    # function_args: str = Field(..., title="Function Args", description="Function Args")
    thread_id: int = Field(..., title="Thread ID", description="Thread ID")
    stack_depth: int = Field(..., title="Stack Depth", description="Stack Depth")


class TaskLogIn(BaseModel):
    level: str = Field(..., title="Log Level", description="Log Level")
    message: str = Field(..., title="Message", description="Message")
    ctx_msg: ContextLogMessage = Field(
        ..., title="Context Message", description="Context Message"
    )
    exc_msg: ExceptionLogMessage | None = Field(
        title="Exception Message", description="Exception Message"
    )


class OneTaskLog(TaskLogIn):
    logged_at: datetime = Field(default=..., title="Timestamp", description="Timestamp")
