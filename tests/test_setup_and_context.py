from __future__ import annotations

import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from task_logging import (
    get_task_log_attrs,
    log_func_call,
    setup_task_logging,
    task_log_context,
)


def _read_json_lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


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
def buf() -> io.StringIO:
    """A non-TTY stream; setup_task_logging will write JSON into it."""
    return io.StringIO()


def test_setup_task_logging_writes_json_with_global_attrs(buf: io.StringIO) -> None:
    setup_task_logging(
        global_log_attrs={"service": "OrderService", "env": "prod"},
        stream=buf,
    )

    logging.getLogger("biz").info("order created")

    [record] = _read_json_lines(buf)
    assert record["service"] == "OrderService"
    assert record["env"] == "prod"
    assert record["message"] == "order created"
    assert record["levelname"] == "INFO"
    assert record["name"] == "biz"
    # `process` comes from stdlib LogRecord.process, not from us.
    assert record["process"] > 0
    assert record["hostname"]
    assert record["exc_info"] is None


def test_no_global_attrs_yields_only_auto_detected(buf: io.StringIO) -> None:
    """global_log_attrs is optional; without it, only hostname is auto-stamped."""
    setup_task_logging(stream=buf)
    logging.getLogger("biz").info("hi")

    [record] = _read_json_lines(buf)
    assert record["hostname"]
    assert "service" not in record  # we don't invent it
    assert "env" not in record


def test_task_log_context_propagates_attrs(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    log.info("before context")
    with task_log_context({"task_id": "task-42", "user_id": "u-1"}):
        log.info("inside context")
    log.info("after context")

    records = _read_json_lines(buf)
    assert [r.get("task_id") for r in records] == [None, "task-42", None]
    assert records[1]["user_id"] == "u-1"
    assert "user_id" not in records[0]
    assert "user_id" not in records[2]


def test_task_log_context_no_args_is_a_no_op(buf: io.StringIO) -> None:
    """Empty / missing dict should still work — just no extras stamped."""
    setup_task_logging(stream=buf)
    with task_log_context():
        logging.getLogger("biz").info("hello")

    [record] = _read_json_lines(buf)
    assert record["message"] == "hello"
    assert "task_id" not in record  # the library never invents one


def test_task_log_context_nests_and_inner_overrides(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    with task_log_context({"task_id": "outer", "region": "us-west"}):
        log.info("outer-1")
        with task_log_context({"task_id": "inner", "note": "nested"}):
            log.info("inner-1")
        log.info("outer-2")

    records = _read_json_lines(buf)
    assert [r["task_id"] for r in records] == ["outer", "inner", "outer"]
    # Inner inherits region from outer.
    assert all(r.get("region") == "us-west" for r in records)
    # `note` is only set inside the inner block.
    assert records[1]["note"] == "nested"
    assert "note" not in records[0]
    assert "note" not in records[2]


def test_local_attrs_override_global_attrs(buf: io.StringIO) -> None:
    """The user-given key in task_log_context wins over setup_task_logging's."""
    setup_task_logging(global_log_attrs={"region": "us-west"}, stream=buf)
    log = logging.getLogger("biz")

    log.info("global only")
    with task_log_context({"region": "eu-central"}):
        log.info("local override")

    records = _read_json_lines(buf)
    assert records[0]["region"] == "us-west"
    assert records[1]["region"] == "eu-central"


def test_global_attrs_override_auto_hostname(buf: io.StringIO) -> None:
    """User-supplied hostname wins over what we auto-detect."""
    setup_task_logging(global_log_attrs={"hostname": "container-a"}, stream=buf)
    logging.getLogger("biz").info("hi")
    [record] = _read_json_lines(buf)
    assert record["hostname"] == "container-a"


def test_task_log_context_imperative_enter_exit(buf: io.StringIO) -> None:
    """The same instance can be used via .enter()/.exit() instead of `with`."""
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    ctx = task_log_context({"task_id": "imperative-1"})
    ctx.enter()
    try:
        log.info("inside")
    finally:
        ctx.exit()
    log.info("outside")

    records = _read_json_lines(buf)
    assert records[0]["task_id"] == "imperative-1"
    assert "task_id" not in records[1]


def test_task_log_context_cannot_be_reentered() -> None:
    """One instance, one entry. Re-entering is a programming error."""
    ctx = task_log_context({"task_id": "x"})
    ctx.enter()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            ctx.enter()
    finally:
        ctx.exit()


def test_get_task_log_attrs_reads_active_context() -> None:
    assert get_task_log_attrs() == {}
    with task_log_context({"task_id": "t-1", "user_id": "u-1"}):
        attrs = get_task_log_attrs()
        assert attrs == {"task_id": "t-1", "user_id": "u-1"}
    assert get_task_log_attrs() == {}


def test_third_party_logger_inherits_context(buf: io.StringIO) -> None:
    """A child logger (mimicking urllib3 / requests) is enriched too."""
    setup_task_logging(global_log_attrs={"service": "svc"}, stream=buf)

    third_party = logging.getLogger("urllib3.connectionpool")
    with task_log_context({"task_id": "t-99"}):
        third_party.warning("Retrying (Retry(total=2))")

    [record] = _read_json_lines(buf)
    assert record["name"] == "urllib3.connectionpool"
    assert record["task_id"] == "t-99"
    assert record["service"] == "svc"


def test_filter_does_not_mutate_the_original_record(buf: io.StringIO) -> None:
    """Enrichment must land on a copy, so other handlers see the unmodified record.

    A host application might install a second handler (Sentry, a debug
    StreamHandler, ...) on the same logger tree. Our filter must not leak its
    enriched attributes onto records they're processing.
    """
    setup_task_logging(global_log_attrs={"service": "svc"}, stream=buf)
    log = logging.getLogger("biz")

    # Pytest installs its own log-capture handler on root; pick our one out
    # by the private tag.
    our_handler = next(
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_task_logging_tag", None) == "task_logging.handler"
    )
    [ctx_filter] = our_handler.filters
    original = log.makeRecord(
        name="biz",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )

    with task_log_context({"task_id": "t-1", "user_id": "u-1"}):
        enriched = ctx_filter.filter(original)

    assert enriched is not original
    assert isinstance(enriched, logging.LogRecord)

    # Enriched record carries the context.
    assert enriched.task_id == "t-1"
    assert enriched.user_id == "u-1"
    assert enriched.service == "svc"

    # Original record carries NONE of it — the contract another handler relies on.
    assert not hasattr(original, "task_id")
    assert not hasattr(original, "user_id")
    assert not hasattr(original, "service")


def test_exception_is_captured_with_locals(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    def divide(a: int, b: int) -> float:
        return a / b

    try:
        divide(1, 0)
    except ZeroDivisionError:
        log.exception("division failed")

    [record] = _read_json_lines(buf)
    exc = record["exc_info"]
    assert exc is not None
    assert exc["name"] == "ZeroDivisionError"
    assert exc["details"] == "division by zero"
    assert "Traceback" in exc["stack_trace"]
    # Locals captured at the deepest frame (inside `divide`).
    assert exc["locals_dict"] == {"a": "1", "b": "0"}


def test_capture_locals_can_be_disabled(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf, capture_locals=False)
    log = logging.getLogger("biz")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception("oops")

    [record] = _read_json_lines(buf)
    assert record["exc_info"]["locals_dict"] == {}


def test_quiet_loggers_are_silenced(buf: io.StringIO) -> None:
    setup_task_logging(
        stream=buf,
        level=logging.DEBUG,
        quiet_loggers={"chatty": logging.WARNING},
    )

    chatty = logging.getLogger("chatty")
    chatty.info("should be dropped")
    chatty.warning("should pass")

    [record] = _read_json_lines(buf)
    assert record["message"] == "should pass"


def test_output_is_always_json_even_for_a_tty_like_stream() -> None:
    """No matter what the stream looks like, output is JSON."""

    class FakeTTY(io.StringIO):
        def isatty(self) -> bool:  # type: ignore[override]
            return True

    fake = FakeTTY()
    setup_task_logging(stream=fake)
    logging.getLogger("biz").info("hello")
    output = fake.getvalue()
    assert output.strip().startswith("{")  # JSON, regardless of TTY status
    record = json.loads(output)
    assert record["message"] == "hello"


def test_log_func_call_emits_enter_and_exit(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    @log_func_call(log)
    def add(x: int, y: int) -> int:
        return x + y

    assert add(2, 3) == 5

    msgs = [r["message"] for r in _read_json_lines(buf)]
    assert msgs[0].startswith("ENTER")
    assert "add" in msgs[0]
    assert msgs[1].startswith("EXIT")
    assert "return=5" in msgs[1]


def test_log_func_call_logs_exception_and_reraises(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    @log_func_call(log)
    def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        boom()

    records = _read_json_lines(buf)
    assert any(
        r["message"].startswith("ENTER") and "boom" in r["message"] for r in records
    )
    raise_records = [
        r
        for r in records
        if r["message"].startswith("RAISE") and "boom" in r["message"]
    ]
    assert len(raise_records) == 1
    assert raise_records[0]["exc_info"]["name"] == "ValueError"


def test_log_func_call_decorates_a_method_without_any_class_setup(
    buf: io.StringIO,
) -> None:
    """Method use case: no `self._logger`, no extra plumbing required."""
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz.service")

    class Service:
        @log_func_call(log)
        def handle(self, n: int) -> int:
            return n * 2

    assert Service().handle(7) == 14

    records = _read_json_lines(buf)
    assert records[0]["name"] == "biz.service"
    # qualname includes the class, so we can tell methods from functions in logs.
    assert "Service.handle" in records[0]["message"]
    assert "return=14" in records[1]["message"]


def test_log_func_call_auto_resolves_logger_from_module(buf: io.StringIO) -> None:
    """When `logger` is omitted, fall back to logging.getLogger(func.__module__)."""
    setup_task_logging(stream=buf)

    @log_func_call()
    def compute() -> int:
        return 42

    assert compute() == 42

    records = _read_json_lines(buf)
    # The decorated function lives in this test module, so its logger should too.
    assert records[0]["name"] == compute.__module__
    assert records[0]["name"] == __name__


def test_log_func_call_respects_custom_level(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf, level=logging.DEBUG)
    log = logging.getLogger("biz")

    @log_func_call(log, level=logging.DEBUG)
    def step() -> str:
        return "ok"

    step()
    records = _read_json_lines(buf)
    assert all(r["levelname"] == "DEBUG" for r in records)


def test_context_isolated_across_threads(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    log = logging.getLogger("biz")

    barrier = threading.Barrier(2)

    def worker(tid: str) -> None:
        with task_log_context({"task_id": tid}):
            barrier.wait()
            log.info(f"hello from {tid}")

    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(worker, ["t-A", "t-B"]))

    task_ids = {r["task_id"] for r in _read_json_lines(buf)}
    assert task_ids == {"t-A", "t-B"}


def test_repeated_setup_does_not_duplicate_handlers(buf: io.StringIO) -> None:
    setup_task_logging(stream=buf)
    setup_task_logging(stream=buf)

    logging.getLogger("biz").info("once")
    assert len(_read_json_lines(buf)) == 1
