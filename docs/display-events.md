# Display events

Ovid2's pipeline never talks to any renderer directly. Instead it emits
typed **events** onto an :class:`EventBus`; renderers subscribe. This is
the "display isolation" invariant, enforced by
`tests/contract/test_display_isolation.py`.

## Event shape

Every event is one JSON object per line ("JSONL"). The schema lives in
`src/ovid2/resources/schemas/events.v1.json`.

Common envelope fields:

| Field         | Type    | Notes |
| ---           | ---     | --- |
| `v`           | int     | Schema version. Bumped on breaking changes. |
| `kind`        | string  | Dotted event name (see below). |
| `ts`          | string  | ISO 8601 UTC timestamp. |
| `instance_id` | string  | `YYYYMMDD-HHMMSS-{6hex}` of the owning `OvidInstance`. |

## Event kinds

| Kind | Emitted when | Additional required fields |
| --- | --- | --- |
| `instance.started`   | Pipeline process boots.                              | — |
| `instance.finished`  | Pipeline process exits.                              | `extra.success` |
| `batch.received`     | Scheduler picked up a new batch (P5).                | `pipeline`, `batch_id` |
| `batch.dispatched`   | Scheduler launched an ETL run for a batch.          | `pipeline`, `batch_id` |
| `batch.completed`    | Every table in a batch finished.                    | `pipeline`, `batch_id` |
| `batch.failed`       | Batch aborted with an unrecoverable error.          | `pipeline`, `batch_id`, `error` |
| `table.started`      | About to run the pipeline for one CDM table.        | `pipeline`, `batch_id`, `table` |
| `table.completed`    | Table write succeeded.                              | `pipeline`, `batch_id`, `table` |
| `table.failed`       | Table raised.                                        | `pipeline`, `batch_id`, `table`, `error` |
| `stage.progress`     | Coarse progress update from a stage.                | `stage` |
| `fix.applied`        | A common fixer ran successfully.                    | `pipeline`, `batch_id`, `table`, `fix` |
| `fix.skipped`        | A fix was declared but not applicable.              | — |
| `fix.failed`         | A fix raised; pipeline continued with pre-fix DF.   | `pipeline`, `batch_id`, `table`, `fix`, `error` |
| `log`                | Free-form log line.                                 | `level`, `message` |
| `warning`            | Non-fatal warning.                                  | `message` |
| `error`              | Non-fatal error surfaced via the bus.               | `message` |

## Producer side

Every event is constructed via a **typed helper** in
`ovid2.core.events` — never as a raw `Event(kind="…", payload={…})` —
so Python and the JSON Schema stay in sync (`tests/contract/test_event_schema.py`
enforces this).

## Consumer side

The **JSONL event log** lives at `WORK_DIR/events-<instance_id>.jsonl`.
Renderers subscribe by tailing that file.

Ships:

- `ovid2.display.renderers.plain_log.PlainLogBus` — one line per event
  written to `stderr`; also usable as an in-process bus leg when
  `ovid2 etl --stderr-log` is set.
- `ovid2.display.renderers.json_file.JsonlFileBus` — appends to a daily
  archive (`/var/log/ovid2/YYYY-MM-DD.jsonl`) with automatic rotation.
- `ovid2.display.renderers.tui` — Rich Live TUI (`ovid2 watch`).
- `ovid2.display.renderers.email_digest` — SMTP-based batch digest.

## Isolation contract

Nothing under `src/ovid2/core/`, `src/ovid2/transformations/`, or
`src/ovid2/formatters/` may import `ovid2.display`. Enforced by
`tests/contract/test_display_isolation.py`.
