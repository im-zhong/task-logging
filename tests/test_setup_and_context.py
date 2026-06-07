from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from task_logging import (
    ClassFunctionLogger,
    FunctionLogger,
    bind_task_context,
    get_task_id,
    setup_logging,
    task_context,
    unbind_task_context,
)


def _read_json_lines(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _reset_root_logger() -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


@pytest.fixture(autouse=True)
def _isolated_logging():
    _reset_root_logger()
    yield
    _reset_root_logger()


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    return tmp_path / "app.log"


def test_setup_logging_writes_json_with_service_and_hostname(log_file: Path) -> None:
    setup_logging(service="OrderService", log_file=log_file, console=False)

    logging.getLogger("biz").info("order created")

    [record] = _read_json_lines(log_file)
    assert record["service"] == "OrderService"
    assert record["msg"] == "order created"
    assert record["level"] == "INFO"
    assert record["logger"] == "biz"
    assert record["task_id"] is None
    assert record["hostname"]
    assert record["pid"] > 0
    assert record["exc"] is None


def test_task_context_propagates_task_id(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")

    log.info("before context")
    with task_context(task_id="task-42", user_id="u-1"):
        log.info("inside context")
    log.info("after context")

    records = _read_json_lines(log_file)
    assert [r["task_id"] for r in records] == [None, "task-42", None]
    assert records[1]["user_id"] == "u-1"


def test_task_context_auto_generates_task_id_when_omitted(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    with task_context() as ctx:
        logging.getLogger("biz").info("hello")
    [record] = _read_json_lines(log_file)
    assert record["task_id"] == ctx["task_id"]
    assert len(ctx["task_id"]) == 32  # uuid4 hex


def test_task_context_nests_and_restores(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")

    with task_context(task_id="outer"):
        log.info("outer-1")
        with task_context(task_id="inner", note="nested"):
            log.info("inner-1")
        log.info("outer-2")

    records = _read_json_lines(log_file)
    assert [r["task_id"] for r in records] == ["outer", "inner", "outer"]
    assert records[1]["note"] == "nested"
    assert "note" not in records[0]
    assert "note" not in records[2]


def test_bind_unbind_pair(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")

    token = bind_task_context(task_id="bound", region="us-west")
    assert get_task_id() == "bound"
    log.info("bound message")
    unbind_task_context(token)
    assert get_task_id() is None

    [record] = _read_json_lines(log_file)
    assert record["task_id"] == "bound"
    assert record["region"] == "us-west"


def test_third_party_logger_inherits_context(log_file: Path) -> None:
    """A child logger (mimicking urllib3 / requests) is enriched too."""
    setup_logging(service="svc", log_file=log_file, console=False)

    third_party = logging.getLogger("urllib3.connectionpool")
    with task_context(task_id="t-99"):
        third_party.warning("Retrying (Retry(total=2))")

    [record] = _read_json_lines(log_file)
    assert record["logger"] == "urllib3.connectionpool"
    assert record["task_id"] == "t-99"
    assert record["service"] == "svc"


def test_exception_is_captured_with_locals(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")

    def divide(a: int, b: int) -> float:
        return a / b

    try:
        divide(1, 0)
    except ZeroDivisionError:
        log.exception("division failed")

    [record] = _read_json_lines(log_file)
    exc = record["exc"]
    assert exc is not None
    assert exc["name"] == "ZeroDivisionError"
    assert exc["details"] == "division by zero"
    assert "Traceback" in exc["stack_trace"]
    # Locals captured at the deepest frame (inside `divide`).
    assert exc["locals_dict"] == {"a": "1", "b": "0"}


def test_capture_locals_can_be_disabled(log_file: Path) -> None:
    setup_logging(
        service="svc", log_file=log_file, console=False, capture_locals=False
    )
    log = logging.getLogger("biz")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("oops")

    [record] = _read_json_lines(log_file)
    assert record["exc"]["locals_dict"] == {}


def test_quiet_loggers_are_silenced(log_file: Path) -> None:
    setup_logging(
        service="svc",
        log_file=log_file,
        console=False,
        level=logging.DEBUG,
        quiet_loggers={"chatty": logging.WARNING},
    )

    chatty = logging.getLogger("chatty")
    chatty.info("should be dropped")
    chatty.warning("should pass")

    [record] = _read_json_lines(log_file)
    assert record["msg"] == "should pass"


def test_function_logger_emits_enter_and_exit(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")
    func_log = FunctionLogger(logger=log)

    @func_log.log_func()
    def add(x: int, y: int) -> int:
        return x + y

    assert add(2, 3) == 5

    msgs = [r["msg"] for r in _read_json_lines(log_file)]
    assert msgs[0].startswith("ENTER add")
    assert msgs[1].startswith("EXIT add")
    assert "return=5" in msgs[1]


def test_function_logger_logs_exception_and_reraises(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")
    func_log = FunctionLogger(logger=log)

    @func_log.log_func()
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        boom()

    records = _read_json_lines(log_file)
    assert any(r["msg"].startswith("ENTER boom") for r in records)
    raise_records = [r for r in records if r["msg"].startswith("RAISE boom")]
    assert len(raise_records) == 1
    assert raise_records[0]["exc"]["name"] == "ValueError"


def test_class_function_logger_uses_instance_logger(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    method_log = ClassFunctionLogger()

    class Service:
        def __init__(self) -> None:
            self._logger = logging.getLogger("biz.service")

        @method_log.log_func()
        def handle(self, n: int) -> int:
            return n * 2

    assert Service().handle(7) == 14

    records = _read_json_lines(log_file)
    assert records[0]["logger"] == "biz.service"
    assert records[0]["msg"].startswith("ENTER handle")
    assert "return=14" in records[1]["msg"]


def test_class_function_logger_silently_skips_when_attr_missing(
    log_file: Path,
) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    method_log = ClassFunctionLogger()

    class Service:
        @method_log.log_func()
        def handle(self) -> str:
            return "ok"

    assert Service().handle() == "ok"
    assert _read_json_lines(log_file) == []


def test_context_isolated_across_threads(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    log = logging.getLogger("biz")

    barrier = threading.Barrier(2)

    def worker(tid: str) -> None:
        with task_context(task_id=tid):
            barrier.wait()
            log.info(f"hello from {tid}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(worker, ["t-A", "t-B"]))

    task_ids = {r["task_id"] for r in _read_json_lines(log_file)}
    assert task_ids == {"t-A", "t-B"}


def test_repeated_setup_does_not_duplicate_handlers(log_file: Path) -> None:
    setup_logging(service="svc", log_file=log_file, console=False)
    setup_logging(service="svc", log_file=log_file, console=False)

    logging.getLogger("biz").info("once")
    assert len(_read_json_lines(log_file)) == 1
