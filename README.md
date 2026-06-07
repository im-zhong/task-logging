# Task Logging

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/task-logging.svg)](https://pypi.org/project/task-logging/)

Task-aware **structured logging** for distributed Python services.

The library plugs into Python's stdlib `logging`, tags every record with a `task_id` (propagated automatically through threads and asyncio tasks), and writes JSON to a log file. A sidecar **Grafana Alloy** agent tails the file, ships it to **Loki**, and you query it through **Grafana** with LogQL.

```
┌────────────────┐    ┌────────────────┐    ┌────────────────┐
│  Service A     │    │  Service B     │    │  Service C     │
│  stdlib logging│    │  stdlib logging│    │  stdlib logging│
│  + task_logging│    │  + task_logging│    │  + task_logging│
└───────┬────────┘    └───────┬────────┘    └───────┬────────┘
        │ JSON file           │ JSON file           │ JSON file
        ▼                     ▼                     ▼
┌────────────────────────────────────────────────────────────┐
│                    Grafana Alloy (tails files)             │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
                       ┌──────────────┐
                       │     Loki     │
                       └──────┬───────┘
                              ▼
                       ┌──────────────┐
                       │   Grafana    │
                       └──────────────┘
```

---

## Why this design?

- **One central place for logs.** All services write JSON to local files; Alloy ships them to a single Loki, queryable from one Grafana instance.
- **Trace a single request across services.** Every log line carries `task_id`, `service`, `hostname`, etc. Pick any of them in Grafana and follow the request end-to-end.
- **Third-party logs come along for free.** Because the library plugs into the stdlib root logger, anything that uses `logging` — `requests`, `urllib3`, `boto3`, `sqlalchemy`, your own modules — automatically gets the same JSON pipeline and the same `task_id` tag.
- **Loki-friendly schema.** `service` / `env` / `level` are low-cardinality (good Loki labels). `task_id` and friends live inside the log line so they don't blow up Loki's stream cardinality.
- **App stays simple.** No HTTP, no batching, no retries in app code — just `RotatingFileHandler`. Disk is the buffer; Alloy handles backpressure and reliability.

---

## Installation

```bash
pip install task-logging
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv add task-logging
```

Requires **Python 3.12+**. The library has **zero runtime dependencies** beyond the stdlib.

---

## Quick Start

### 1. Configure logging once at startup

```python
import logging
from task_logging import setup_logging

setup_logging(
    service="OrderService",                       # used as a Loki label
    log_file="/var/log/order-service/app.log",    # what Alloy will tail
    env="prod",
    level=logging.INFO,
    quiet_loggers={"urllib3": logging.WARNING},   # tame noisy libs
)
```

After this, **every** stdlib logger in the process — yours and third-party — emits JSON to `app.log`.

### 2. Use stdlib logging the normal way

```python
log = logging.getLogger(__name__)
log.info("service started")
```

### 3. Tag work with a `task_id`

```python
from task_logging import task_context

def handle_request(req):
    with task_context(task_id=req.id, user_id=req.user_id):
        log.info("handling request")
        do_step_1()       # logs from here are tagged too
        do_step_2()
        # third-party libs you call inside the block are tagged as well:
        requests.get("https://api.x.com/v1/foo")
```

`task_context()` uses Python `contextvars`, so it works correctly across **threads, `asyncio` tasks, and `concurrent.futures` executors** — each concurrent request gets its own isolated context.

If you don't pass `task_id`, a `uuid4` hex is generated for you.

If you can't use a `with` block (e.g. binding in middleware "before" / "after" hooks):

```python
from task_logging import bind_task_context, unbind_task_context

token = bind_task_context(task_id=req.id)
try:
    handle()
finally:
    unbind_task_context(token)
```

### 4. View it in Grafana

Once Alloy → Loki → Grafana is running (see **Deployment** below), this LogQL query gives you everything for one request, across services:

```logql
{env="prod"} | json | task_id="abc-123"
```

---

## What gets logged

Every record is a single line of JSON with this stable shape:

```json
{
  "ts":          "2026-06-07T09:14:22.503112+00:00",
  "level":       "INFO",
  "logger":      "billing.settlement",
  "msg":         "charging account",
  "service":     "Billing",
  "env":         "prod",
  "hostname":    "worker-7",
  "pid":         4321,
  "thread":      140234567890,
  "thread_name": "MainThread",
  "task_id":     "task-42",
  "module":      "settlement",
  "func":        "charge",
  "file":        "/app/billing/settlement.py",
  "line":        87,
  "exc":         null,
  "user_id":     "u-1"
}
```

`exc` is `null` for normal records and an object for exceptions:

```json
"exc": {
  "name":        "ZeroDivisionError",
  "details":     "division by zero",
  "stack_trace": "Traceback (most recent call last):\n  File ...",
  "locals_dict": {"a": "1", "b": "0"}
}
```

`locals_dict` is a `repr()`-snapshot of the local variables at the deepest stack frame where the exception was raised — invaluable for post-mortem debugging. Disable it with `setup_logging(..., capture_locals=False)` if you're worried about secrets leaking into logs.

---

## Logging exceptions

Inside an `except` block, just call `log.exception()` (or pass `exc_info=True` to any other method). The formatter populates the `exc` field automatically:

```python
def divide(a: int, b: int) -> float:
    return a / b

try:
    divide(1, 0)
except ZeroDivisionError:
    log.exception("division failed")
```

Same goes for raising inside a decorated function — see below.

---

## The `@log_call` decorator

For zero-boilerplate enter / exit / timing logs, wrap any callable with `log_call`. It works on plain functions, instance methods, classmethods, staticmethods — anything — and imposes **no requirements on the surrounding class**.

```python
import logging
from task_logging import log_call

log = logging.getLogger(__name__)

@log_call(log)
def add(x: int, y: int) -> int:
    return x + y

add(2, 3)
# Logs (as JSON):
#   ENTER add args=(2, 3) kwargs={}
#   EXIT  add return=5 cost_ms=0.012
```

It works on methods the same way — no `self._logger` attribute, no setup:

```python
class Service:
    @log_call(log)
    def handle(self, payload: dict) -> None:
        ...
# Logs use the qualified name, so methods are easy to tell apart:
#   ENTER Service.handle args=({...},) kwargs={}
```

Omit the logger to auto-resolve `logging.getLogger(func.__module__)` — the stdlib "one logger per module" idiom:

```python
@log_call()  # uses logging.getLogger(__name__) of the module the function lives in
def compute() -> int:
    ...
```

Override the level if you want:

```python
@log_call(log, level=logging.DEBUG)
def chatty(): ...
```

If the wrapped callable raises, `log_call` emits a `RAISE` record (with full exception info: stack trace + locals) and re-raises:

```
RAISE add after 0.142ms     (exc=ValueError: nope)
```

---

## Deployment: Loki + Alloy + Grafana

You don't need anything fancy. A `docker-compose.yml` with three services is enough to start.

### `docker-compose.yml`

```yaml
services:
  loki:
    image: grafana/loki:3.2.0
    ports: ["3100:3100"]
    command: -config.file=/etc/loki/local-config.yaml
    volumes:
      - loki-data:/loki

  alloy:
    image: grafana/alloy:latest
    command: run --server.http.listen-addr=0.0.0.0:12345 /etc/alloy/config.alloy
    volumes:
      - ./alloy/config.alloy:/etc/alloy/config.alloy:ro
      # Mount the host log directory(ies) you want to ship.
      - /var/log/order-service:/var/log/order-service:ro
      - /var/log/billing:/var/log/billing:ro
    ports: ["12345:12345"]
    depends_on: [loki]

  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
    environment:
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
    volumes:
      - grafana-data:/var/lib/grafana

volumes:
  loki-data:
  grafana-data:
```

### `alloy/config.alloy`

```alloy
// Tail JSON log files. Each `targets` entry becomes a Loki stream with the
// labels you list — keep the label set SMALL and LOW-CARDINALITY.
local.file_match "services" {
  path_targets = [
    {__path__ = "/var/log/order-service/*.log", service = "OrderService", env = "prod"},
    {__path__ = "/var/log/billing/*.log",       service = "Billing",      env = "prod"},
  ]
}

loki.source.file "services" {
  targets    = local.file_match.services.targets
  forward_to = [loki.process.parse.receiver]
}

// Parse JSON, promote `level` to a label, drop the rest into structured metadata
// so it's queryable but doesn't create new streams.
loki.process "parse" {
  forward_to = [loki.write.default.receiver]

  stage.json {
    expressions = {
      level   = "level",
      ts      = "ts",
      task_id = "task_id",
    }
  }

  stage.timestamp {
    source = "ts"
    format = "RFC3339Nano"
  }

  stage.labels {
    values = { level = "" }   // level is a low-cardinality label
  }

  stage.structured_metadata {
    values = { task_id = "" } // task_id is HIGH-cardinality; never make it a label
  }
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

> **Why `task_id` is in `structured_metadata`, not labels.** Loki indexes by label combinations, so a high-cardinality label like `task_id` would create a new stream per request and crash Loki. `structured_metadata` (Loki ≥ 2.9) gives you fast filtering on high-cardinality fields without that cost.

### Bring it up

```bash
docker compose up -d
```

- **Loki**: http://localhost:3100
- **Alloy UI**: http://localhost:12345
- **Grafana**: http://localhost:3000

Add Loki as a Grafana data source (`http://loki:3100`), then explore:

```logql
# all logs from one service
{service="OrderService", env="prod"}

# follow one request across services
{env="prod"} | json | task_id="abc-123"

# only errors, last hour
{env="prod", level=~"ERROR|CRITICAL"}

# filter on a structured field
{service="Billing"} | json | user_id="u-42"
```

### Kubernetes note

In a Kubernetes cluster, the canonical setup is to deploy Alloy as a **DaemonSet** and have it tail `/var/log/containers/*.log` instead of a per-service path — your apps just write JSON to **stdout** and the kubelet handles the file persistence. Same `loki.process` pipeline applies. See the official [Alloy install docs](https://grafana.com/docs/alloy/latest/set-up/install/kubernetes/) for the helm chart.

---

## Public API

```python
from task_logging import (
    setup_logging,        # call once at startup
    task_context,         # `with task_context(task_id=...): ...`
    bind_task_context,    # imperative version
    unbind_task_context,
    get_task_id,          # read the active task_id
    get_task_context,     # read the full active context dict
    log_call,             # decorator: ENTER / EXIT / RAISE for any callable
    TaskContextFilter,    # the underlying logging.Filter
    JsonFormatter,        # the underlying logging.Formatter
)
```

| Symbol | Purpose |
|---|---|
| `setup_logging(service=..., log_file=..., ...)` | One-shot configuration of the root logger. |
| `task_context(task_id=..., **extra)` | Context manager that binds fields onto every log inside the block. |
| `bind_task_context(**extra)` / `unbind_task_context(token)` | Imperative pair for non-`with` use. |
| `get_task_id()` / `get_task_context()` | Read the currently active context. |
| `log_call(logger=None, *, level=logging.INFO)` | Decorator that logs ENTER / EXIT / RAISE for the wrapped callable. `logger=None` auto-resolves to the function's module logger. |
| `TaskContextFilter`, `JsonFormatter` | Exposed for advanced setups (e.g. attaching to a custom handler). |

---

## End-to-end example

```python
import logging
from task_logging import log_call, setup_logging, task_context

setup_logging(
    service="Billing",
    log_file="/var/log/billing/app.log",
    env="prod",
    quiet_loggers={"urllib3": logging.WARNING, "botocore": logging.WARNING},
)

log = logging.getLogger(__name__)


@log_call(log)
def charge(amount: float, currency: str) -> str:
    return f"charged {amount} {currency}"


class Settlement:
    @log_call(log, level=logging.DEBUG)
    def settle(self, account: str) -> None:
        log.info("settling %s", account)
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("settlement failed")


def handle_request(req):
    with task_context(task_id=req.id, user_id=req.user_id):
        charge(9.99, "USD")
        Settlement().settle("acct-1")
```

After this runs, every line in `/var/log/billing/app.log` is JSON tagged with the request's `task_id` and `user_id` — including any logs from `requests`, `urllib3`, `botocore`, etc. that fired during the request.

---

## Tips & gotchas

- **`service` must be low-cardinality.** It becomes a Loki label. Use `"OrderService"`, never `"OrderService-pod-abc-7"`.
- **`task_id` is per-request, never a label.** It rides inside the JSON payload. Loki ≥ 2.9 + `stage.structured_metadata` lets you filter on it efficiently.
- **Exception capture only works inside `except` blocks.** The formatter reads `sys.exc_info()`, so call `log.exception(...)` while the exception is still being handled.
- **Calling `setup_logging()` more than once is safe** — it removes its previous handlers before installing new ones, so tests / hot-reloads don't double-log.
- **Disable locals capture in regulated environments.** Pass `capture_locals=False` to `setup_logging()` if `repr()` of arbitrary local variables could leak secrets.
- **What about loguru?** loguru is not based on stdlib `logging`, so libraries like `requests` and `urllib3` won't be captured by it. This package deliberately uses stdlib so third-party logs flow through the same pipeline. If you want loguru in your own code, use loguru's `InterceptHandler` to bridge stdlib → loguru — but this library does not require it.

---

## Design notes

If you want a deeper mental model than this README provides, see [docs/](docs/):

- [`docs/design/decorators.md`](docs/design/decorators.md) — why one `@log_call` instead of `FunctionLogger` + `ClassFunctionLogger`, and why classes don't need a `_logger` attribute
- [`docs/design/task-context.md`](docs/design/task-context.md) — how `task_context` makes `task_id` flow through threads, asyncio tasks, and third-party libraries' logs
- [`docs/design/stdlib-logging-primer.md`](docs/design/stdlib-logging-primer.md) — bottom-up tour of stdlib `logging` (LogRecord, the logger tree, handlers, filters, formatters) with the rules that prevent the most common pitfalls
- [`docs/design/why-json-logs.md`](docs/design/why-json-logs.md) — Loki accepts arbitrary text; why does this library emit JSON anyway? What do we gain, and what do we trade away?

---

## Development

```bash
git clone https://github.com/im-zhong/task-logging.git
cd task-logging
uv sync
uv run pytest
```

```bash
uv run ruff check .
uv run mypy task_logging
uv run pre-commit run --all-files
```

---

## License

MIT — see [LICENSE](LICENSE).
