# Task Logging

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PyPI](https://img.shields.io/pypi/v/task-logging.svg)](https://pypi.org/project/task-logging/)

Task-aware **structured logging** for distributed Python services.

The library plugs into Python's stdlib `logging`, lets you bind whatever per-request attrs you want (`task_id`, `user_id`, `trace_id`, …) so they propagate automatically through threads and asyncio tasks, and writes JSON to **stdout**. The container runtime captures stdout, **Grafana Alloy** scrapes it, ships it to **Loki**, and you query it through **Grafana** with LogQL.

```
┌────────────────┐    ┌────────────────┐    ┌────────────────┐
│  Service A     │    │  Service B     │    │  Service C     │
│  stdlib logging│    │  stdlib logging│    │  stdlib logging│
│  + task_logging│    │  + task_logging│    │  + task_logging│
└───────┬────────┘    └───────┬────────┘    └───────┬────────┘
        │ JSON to stdout      │ JSON to stdout      │ JSON to stdout
        ▼                     ▼                     ▼
┌────────────────────────────────────────────────────────────┐
│   Container runtime (Docker / Kubernetes) captures stdout  │
└──────────────────────────────┬─────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────┐
│      Grafana Alloy (discovers + scrapes containers)        │
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

- **One central place for logs.** All services write JSON to stdout; the container runtime captures it; Alloy ships it to a single Loki, queryable from one Grafana instance.
- **Trace a single request across services.** Every log line carries whatever attrs you bound — `task_id`, `service`, `user_id`, anything. Pick any of them in Grafana and follow the request end-to-end.
- **Third-party logs come along for free.** Because the library plugs into the stdlib root logger, anything that uses `logging` — `requests`, `urllib3`, `boto3`, `sqlalchemy`, your own modules — automatically gets the same JSON pipeline and the same `task_id` tag.
- **Loki-friendly schema.** `service` / `env` / `level` are low-cardinality (good Loki labels). `task_id` and friends live inside the log line so they don't blow up Loki's stream cardinality.
- **App stays simple — and 12-factor.** No log files, no rotation knobs, no HTTP, no batching, no retries. Just `print` to stdout (effectively). The platform handles capture, rotation, and shipping. See [12factor.net/logs](https://12factor.net/logs).

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
from task_logging import setup_task_logging

setup_task_logging(
    global_log_attrs={
        "service": "OrderService",                # used as a Loki label
        "env": "prod",
    },
    level=logging.INFO,
    quiet_loggers={"urllib3": logging.WARNING},   # tame noisy libs
)
```

After this, **every** stdlib logger in the process — yours and third-party — writes one line of **JSON** to **stdout**, ready for Alloy.

Want human-readable output during local development? Pipe through `jq`:

```bash
python -m myapp | jq
```

That keeps every structured field (`task_id`, `exc_info.locals_dict`, …) visible — a "pretty" formatter would have to drop them.

### 2. Use stdlib logging the normal way

```python
log = logging.getLogger(__name__)
log.info("service started")
```

### 3. Tag work with a `task_id`

```python
from task_logging import task_log_context

def handle_request(req):
    with task_log_context({"task_id": req.id, "user_id": req.user_id}):
        log.info("handling request")
        do_step_1()       # logs from here are tagged too
        do_step_2()
        # third-party libs you call inside the block are tagged as well:
        requests.get("https://api.x.com/v1/foo")
```

`task_log_context()` uses Python `contextvars`, so it works correctly across **threads, `asyncio` tasks, and `concurrent.futures` executors** — each concurrent request gets its own isolated context.

The library doesn't privilege any particular attr name. Pick whatever keys your domain wants — `task_id`, `request_id`, `trace_id`, `correlation_id` — they all ride through.

If you can't use a `with` block (e.g. middleware that binds in a `before_request` hook and unbinds in `after_request`), the same instance also exposes `enter()` / `exit()`:

```python
def before_request(req):
    req.state.log_ctx = task_log_context({"task_id": req.id})
    req.state.log_ctx.enter()

def after_request(req):
    req.state.log_ctx.exit()
```

### 4. View it in Grafana

Once Alloy → Loki → Grafana is running (see **Deployment** below), this LogQL query gives you everything for one request, across services:

```logql
{env="prod"} | json | task_id="abc-123"
```

---

## What gets logged

Every record is a single line of JSON with this stable shape. The keys mirror stdlib `LogRecord` attribute names — anyone who knows [stdlib logging](https://docs.python.org/3/library/logging.html#logrecord-attributes) already knows the schema:

```json
{
  "created":    1717839622.503112,
  "levelname":  "INFO",
  "name":       "billing.settlement",
  "message":    "charging account",
  "process":    4321,
  "thread":     140234567890,
  "threadName": "MainThread",
  "module":     "settlement",
  "funcName":   "charge",
  "pathname":   "/app/billing/settlement.py",
  "lineno":     87,
  "exc_info":   null,

  "service":    "Billing",
  "env":        "prod",
  "task_id":    "task-42",
  "user_id":    "u-1"
}
```

The first block mirrors stdlib LogRecord; the second block is whatever **you** bound. The library does not auto-detect anything — `service`, `env`, `task_id`, `user_id` (and `hostname`, if you want it) are all supplied by you via `setup_task_logging(global_log_attrs=...)` and `task_log_context({...})`.

`exc_info` is `null` for normal records and an object for exceptions:

```json
"exc_info": {
  "name":        "ZeroDivisionError",
  "details":     "division by zero",
  "stack_trace": "Traceback (most recent call last):\n  File ...",
  "locals_dict": {"a": "1", "b": "0"}
}
```

`locals_dict` is a `repr()`-snapshot of the local variables at the deepest stack frame where the exception was raised — invaluable for post-mortem debugging. Disable it with `setup_task_logging(..., capture_locals=False)` if you're worried about secrets leaking into logs.

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

## The `@log_func_call` decorator

For zero-boilerplate enter / exit / timing logs, wrap any function with `log_func_call`. It works on plain functions, instance methods, classmethods, staticmethods — anything — and imposes **no requirements on the surrounding class**.

```python
import logging
from task_logging import log_func_call

log = logging.getLogger(__name__)

@log_func_call(log)
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
    @log_func_call(log)
    def handle(self, payload: dict) -> None:
        ...
# Logs use the qualified name, so methods are easy to tell apart:
#   ENTER Service.handle args=({...},) kwargs={}
```

Omit the logger to auto-resolve `logging.getLogger(func.__module__)` — the stdlib "one logger per module" idiom:

```python
@log_func_call()  # uses logging.getLogger(func.__module__)
def compute() -> int:
    ...
```

Override the level if you want:

```python
@log_func_call(log, level=logging.DEBUG)
def chatty(): ...
```

If the wrapped function raises, `log_func_call` emits a `RAISE` record (with full exception info: stack trace + locals) and re-raises:

```
RAISE add after 0.142ms     (exc=ValueError: nope)
```

---

## Deployment: Loki + Alloy + Grafana

A `docker-compose.yml` with Loki, Grafana, Alloy, and your services is enough to start. Alloy uses **Docker socket discovery** to scrape every container's stdout — no per-service file mounts, no rotation config.

### Tag your service containers

Alloy reads container labels to figure out the `service` / `env` to attach to logs. Add labels to each app service:

```yaml
services:
  order-service:
    image: my-org/order-service:latest
    labels:
      - "logging=true"               # opt this container in
      - "logging.service=OrderService"
      - "logging.env=prod"

  billing:
    image: my-org/billing:latest
    labels:
      - "logging=true"
      - "logging.service=Billing"
      - "logging.env=prod"
```

(Pick the label keys you like — Alloy lets you map any label to a Loki label. The keys above match the example Alloy config below.)

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
      # Mount the Docker socket so Alloy can discover and scrape sibling
      # containers' stdout. Read-only is enough.
      - /var/run/docker.sock:/var/run/docker.sock:ro
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
// Discover all running containers via the Docker socket.
discovery.docker "containers" {
  host = "unix:///var/run/docker.sock"
}

// Keep only containers explicitly opted in via `logging=true`, and promote
// their labels into Prometheus-style targets that loki.source.docker can scrape.
discovery.relabel "containers" {
  targets = discovery.docker.containers.targets

  // Drop containers that didn't opt in.
  rule {
    source_labels = ["__meta_docker_container_label_logging"]
    regex         = "true"
    action        = "keep"
  }

  // Map container labels to Loki labels (low-cardinality only).
  rule {
    source_labels = ["__meta_docker_container_label_logging_service"]
    target_label  = "service"
  }
  rule {
    source_labels = ["__meta_docker_container_label_logging_env"]
    target_label  = "env"
  }
  // The container name is also useful for distinguishing replicas.
  rule {
    source_labels = ["__meta_docker_container_name"]
    target_label  = "container"
  }
}

// Read each opted-in container's stdout/stderr.
loki.source.docker "containers" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.relabel.containers.output
  forward_to = [loki.process.parse.receiver]
}

// Parse the JSON line. Field names match Python stdlib LogRecord
// attributes (levelname, created, ...). We rename `levelname` to a
// shorter `level` Loki label for query ergonomics — that's a labelling
// decision, not a schema change in the JSON.
loki.process "parse" {
  forward_to = [loki.write.default.receiver]

  stage.json {
    expressions = {
      level   = "levelname",  // pull stdlib's `levelname` out as `level`
      created = "created",    // Unix float timestamp from stdlib
      task_id = "task_id",    // our own field
    }
  }

  stage.timestamp {
    source = "created"
    format = "Unix"
  }

  stage.labels {
    values = { level = "" }   // level is a low-cardinality Loki label
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

# only errors, last hour (the Loki label `level` is set by Alloy from the
# JSON `levelname` field — see the Alloy config above)
{env="prod", level=~"ERROR|CRITICAL"}

# filter on a structured field bound via task_log_context({"user_id": ...})
{service="Billing"} | json | user_id="u-42"

# filter on a stdlib LogRecord field after `| json`
{service="Billing"} | json | funcName="charge"

# isolate one container replica
{service="OrderService", container="order-service-2"}
```

### Kubernetes note

In a Kubernetes cluster, replace `discovery.docker` with `discovery.kubernetes` and deploy Alloy as a **DaemonSet**. The kubelet already captures every container's stdout into `/var/log/containers/*.log`; Alloy tails those files and uses the pod's labels / annotations (instead of Docker labels) to attach `service` / `env`. Same `loki.process` JSON pipeline applies. See the official [Alloy install docs](https://grafana.com/docs/alloy/latest/set-up/install/kubernetes/) for the helm chart.

---

## Public API

```python
from task_logging import (
    setup_task_logging,      # call once at startup
    shutdown_task_logging,   # remove the handler setup_task_logging installed
    task_log_context,        # `with task_log_context({...}): ...`,
                             # or imperative ctx.enter() / ctx.exit()
    get_task_log_attrs,      # read the currently-active attrs (merged)
    log_func_call,           # decorator: ENTER / EXIT / RAISE for a function
    TaskLogFilter,           # the underlying logging.Filter
    JsonFormatter,           # the underlying logging.Formatter
)
```

| Symbol | Purpose |
|---|---|
| `setup_task_logging(global_log_attrs=..., level=..., ...)` | One-shot configuration of the root logger. Writes one JSON line per record to stdout. |
| `shutdown_task_logging()` | Pair of `setup_task_logging`. Removes the handler we installed; idempotent. Mainly for tests, hot-reloads, and embedded use; long-running services typically don't call this. |
| `task_log_context(attrs)` | Bind a dict of attrs to the current execution context. Supports both `with task_log_context({...}):` and imperative `ctx.enter()` / `ctx.exit()`. |
| `get_task_log_attrs()` | Return the currently-active merged attrs (empty dict if no context is active). |
| `log_func_call(logger=None, *, level=logging.INFO)` | Decorator that logs ENTER / EXIT / RAISE for a function. `logger=None` auto-resolves to the function's module logger. |
| `TaskLogFilter`, `JsonFormatter` | Exposed for advanced setups (e.g. attaching to a custom handler). |

---

## End-to-end example

```python
import logging
from task_logging import log_func_call, setup_task_logging, task_log_context

setup_task_logging(
    global_log_attrs={"service": "Billing", "env": "prod"},
    quiet_loggers={"urllib3": logging.WARNING, "botocore": logging.WARNING},
)

log = logging.getLogger(__name__)


@log_func_call(log)
def charge(amount: float, currency: str) -> str:
    return f"charged {amount} {currency}"


class Settlement:
    @log_func_call(log, level=logging.DEBUG)
    def settle(self, account: str) -> None:
        log.info("settling %s", account)
        try:
            1 / 0
        except ZeroDivisionError:
            log.exception("settlement failed")


def handle_request(req):
    with task_log_context({"task_id": req.id, "user_id": req.user_id}):
        charge(9.99, "USD")
        Settlement().settle("acct-1")
```

When this runs in a container, every line of stdout is JSON tagged with the request's `task_id` and `user_id` — including any logs from `requests`, `urllib3`, `botocore`, etc. that fired during the request. Alloy picks the lines up via the Docker socket, attaches the `service=Billing` / `env=prod` labels from the container's labels, and ships them to Loki.

---

## Tips & gotchas

- **`service` must be low-cardinality.** It becomes a Loki label. Use `"OrderService"`, never `"OrderService-pod-abc-7"`.
- **`task_id` is per-request, never a label.** It rides inside the JSON payload. Loki ≥ 2.9 + `stage.structured_metadata` lets you filter on it efficiently.
- **Exception capture only works inside `except` blocks.** The formatter reads `sys.exc_info()`, so call `log.exception(...)` while the exception is still being handled.
- **Calling `setup_task_logging()` more than once is safe** — it removes its previous handlers before installing new ones, so tests / hot-reloads don't double-log. Use `shutdown_task_logging()` to undo it explicitly (idempotent; leaves any handlers the host application installed alone).
- **Disable locals capture in regulated environments.** Pass `capture_locals=False` to `setup_task_logging()` if `repr()` of arbitrary local variables could leak secrets.
- **What about loguru?** loguru is not based on stdlib `logging`, so libraries like `requests` and `urllib3` won't be captured by it. This package deliberately uses stdlib so third-party logs flow through the same pipeline. If you want loguru in your own code, use loguru's `InterceptHandler` to bridge stdlib → loguru — but this library does not require it.

---

## Design notes

If you want a deeper mental model than this README provides, see [docs/](docs/):

- [`docs/design/decorators.md`](docs/design/decorators.md) — why one `@log_func_call` instead of `FunctionLogger` + `ClassFunctionLogger`, and why classes don't need a `_logger` attribute
- [`docs/design/task-context.md`](docs/design/task-context.md) — how `task_log_context` makes log attrs flow through threads, asyncio tasks, and third-party libraries' logs
- [`docs/design/stdlib-logging-primer.md`](docs/design/stdlib-logging-primer.md) — bottom-up tour of stdlib `logging` (LogRecord, the logger tree, handlers, filters, formatters) with the rules that prevent the most common pitfalls
- [`docs/design/why-json-logs.md`](docs/design/why-json-logs.md) — Loki accepts arbitrary text; why does this library emit JSON anyway? What do we gain, and what do we trade away?
- [`docs/design/json-schema.md`](docs/design/json-schema.md) — where the JSON keys come from, why we mirror stdlib LogRecord attribute names instead of inventing our own, and the stability promise

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
