# Why JSON-formatted logs?

> Loki accepts arbitrary text. Why does this library emit JSON anyway?

The short answer: **Loki doesn't require it; we choose it because it's the
format that makes every downstream tool — Alloy, Grafana, ad-hoc `jq`
pipelines, future Splunk sidecars — agree on what the fields mean without
us shipping a regex with every consumer.**

The longer answer follows.

## What each component actually requires

| Component | What it accepts |
|---|---|
| **Loki** | Any UTF-8 string per line. It just stores them. |
| **Alloy** (the agent) | Any text. It can parse JSON, logfmt, regex, multiline, … whatever you tell it to do in the pipeline. |
| **Grafana** | Whatever Loki returns. |

So the format is **entirely your choice**. You could ship plain text:

```
2026-06-07 09:14:22 INFO  biz: order created task_id=task-42 user_id=u-1
```

…and Loki would happily store it. You'd query with
`{service="OrderService"} |= "task_id=task-42"` (substring match) and it
would work, just slower and clunkier.

## Why JSON wins anyway

### 1. You get *typed*, *named* fields, not text grep

LogQL with JSON parsing:

```logql
{service="Billing"}
  | json
  | task_id="task-42"           # exact equality, not substring
  | duration_ms > 500           # numeric comparison
  | user_id =~ "u-.*"           # regex on a specific field
```

Same query against plain text:

```logql
{service="Billing"} |= "task-42"      # could match "task-420", "task-4200"...
# numeric comparisons? regex on a specific field? You'd write a custom regex
# and pray the log format never changes.
```

JSON gives you the difference between "search" and "query."

### 2. Alloy can promote fields cleanly

In the Alloy config in our README:

```alloy
loki.process "parse" {
  stage.json {
    expressions = {
      level   = "levelname",  // pull stdlib's `levelname` field out as `level`
      created = "created",    // Unix float timestamp
      task_id = "task_id",
    }
  }
  stage.timestamp { source = "created" format = "Unix" }
  stage.labels   { values = { level = "" } }
  stage.structured_metadata { values = { task_id = "" } }
}
```

That config takes a JSON line (whose keys mirror stdlib `LogRecord`
attribute names — see [json-schema.md](json-schema.md)) and:

- Promotes the JSON `levelname` field to a Loki **label** named `level`
  (cheap, indexed) — so `{level="ERROR"}` queries are fast. The Loki
  label name is independent of the JSON field name; we keep `level`
  for query ergonomics.
- Pushes `task_id` into Loki **structured metadata** (filterable but not
  indexed) — high-cardinality task IDs don't blow up Loki's stream count,
  yet you can still filter by them quickly.
- Uses the application's `created` timestamp (Unix float, from
  stdlib's `LogRecord.created`) as the canonical timestamp instead of
  "the time Alloy happened to read the line" — which matters when there's
  lag between writing and shipping.

If the line is plain text, you can still extract these with `stage.regex`,
but now you're maintaining a regex in Alloy that has to stay in sync with
whatever log format your apps emit. That's a coupling worth avoiding.

### 3. Schema flexibility without breaking parsers

A regex assumes a fixed column order:

```
^(\S+)\s+(\S+)\s+(\S+):\s+(.*)$    # ts level logger msg
```

The day someone adds a `request_id` field, you have a choice: change the
regex (and potentially break old log lines), or shove the new field
somewhere awkward. With JSON, adding a key never breaks the consumer. The
schema *grows*, it doesn't *shift*.

This matters more than it sounds, because logs are append-only and you'll
be querying old log lines that pre-date your latest schema for as long as
your retention is.

### 4. Stack traces, locals, nested objects

Look at what the formatter actually emits for an exception:

```json
"exc_info": {
  "name": "ZeroDivisionError",
  "details": "division by zero",
  "stack_trace": "Traceback (most recent call last):\n  File ...",
  "locals_dict": {"a": "1", "b": "0"}
}
```

How would you put that on one line of plain text without it becoming an
unreadable mess? You'd either:

- Spread it across multiple lines (now Alloy needs `stage.multiline` —
  gnarly).
- Encode it inline somehow (which is just JSON with extra steps).
- Drop the structure (and lose the ability to query `exc_info.name="ValueError"`).

JSON handles nested structure natively. One line, one record, queryable
shape.

### 5. Stable contract between app and ingestion

This is the soft reason but probably the most important one:

> JSON is a contract. Plain text is a habit.

When the log line is `{"levelname":"INFO","message":"...","task_id":"..."}`, every
consumer — Alloy, an ad-hoc `jq` pipeline, a one-off Python script that
greps archived logs, a Grafana dashboard, a Splunk sidecar somebody adds
three years from now — agrees on what the fields are. When the line is
`2026-06-07 09:14 INFO biz: hi`, every consumer has its own regex, and
they all silently disagree about edge cases.

In a multi-service environment that's the right hill to die on.

## What we *give up* by choosing JSON

To be honest about the tradeoffs:

1. **Humans can't read raw JSON logs as easily.** Pipe through `jq`
   for local development: `python -m myapp | jq`, or
   `docker logs <ctr> | jq`. We considered shipping a "pretty" text
   formatter that auto-engages at a TTY, and decided against it — see
   "Why one format, not two" below.

2. **Slightly larger on-disk footprint.** All those `"key":` repetitions
   add up. In practice Loki/Alloy compress chunks heavily and this
   disappears, but if you're running at extreme scale you'd benefit from a
   more compact format like Protobuf or
   [logfmt](https://brandur.org/logfmt)
   (`level=info msg="hi" task_id=task-42`). For 99% of use cases, JSON is
   fine.

3. **Marginally more CPU.** Building a dict and `json.dumps`-ing it is
   more work than `f"{ts} {lvl} {msg}\n"`. Measured cost on modern Python:
   roughly a few microseconds per log call. Negligible unless you're
   logging in a tight inner loop, in which case you should batch or
   sample.

## Why one format, not two

Earlier versions of `setup_task_logging` shipped a second "human-readable"
formatter that engaged automatically when stdout was a TTY. We removed
it. The reasons:

1. **Two code paths, two test surfaces.** Every fix to the JSON formatter
   had to be checked against the human one, and vice versa. Bugs leaked
   between them.

2. **Different output in dev vs prod is a footgun.** A subtle field
   missing from the human formatter wouldn't be noticed locally but
   would matter the moment you read the same log line in Grafana. One
   format means what you see at `python -m myapp | jq` is what Alloy
   sees in production.

3. **`jq` is already a better human formatter.** It pretty-prints, it
   colourises, and crucially it shows *every* structured field — `task_id`,
   `exc.locals_dict`, `user_id`, … — that a hand-rolled "pretty" formatter
   would have hidden to keep lines short. So at a terminal,
   `python -m myapp | jq` (or `docker logs <ctr> | jq`) is the recommended
   path.

4. **TTY auto-detection is fragile.** `stdout.isatty()` returns False
   inside `pytest` capture, inside CI, inside `tmux pipe-pane`, and
   sometimes inside Docker exec depending on flags. The "right" format
   was hard to predict.

If you want a different output in production vs development, the answer
is a wrapper script (`python -m myapp | jq` for one, raw stdout for the
other), not a library knob.

## When *not* to use JSON

If your environment already has structured-logging infrastructure that
prefers something else, follow it:

- **logfmt** is popular at companies that grew up in the Heroku era
  (Grafana itself emits logfmt). Alloy has a `stage.logfmt` that's just
  as good as `stage.json`.
- **OpenTelemetry logs** (OTLP) is the emerging "post-JSON" standard —
  protobuf over gRPC, with a real schema. If your org is going OTel-first,
  you'd skip JSON and ship OTLP via the OTel collector or Alloy's OTel
  pipeline.
- **CEE / RFC 5424 syslog** if you're plugging into existing syslog
  infra.

For a greenfield Python service shipping to Loki, JSON is the path of
least surprise and least lock-in. That's why this library defaults to it.
The pipeline is "structured logs in a format anyone can parse" — the fact
that JSON also happens to be Loki/Alloy's smoothest input format is icing.

## TL;DR

- Loki accepts anything; nothing forces JSON on you.
- JSON wins because it lets Alloy cleanly **promote some fields to Loki
  labels** (low-cardinality, indexed) and **others to structured
  metadata** (high-cardinality, filterable), turn Grafana's `| json`
  operator into typed queries, and preserve nested data like exception
  structures.
- The one-line JSON contract is also the most durable choice for a
  multi-service environment where the same log stream might be consumed
  by tools you haven't picked yet.
- The library emits JSON unconditionally. For human reading at a
  terminal, pipe through `jq`: `python -m myapp | jq` or
  `docker logs <ctr> | jq`. One format means dev and prod logs look
  the same.
