---
title: Events & renderers
summary: How pipeline code emits typed events, the event schema, and how to write a renderer.
audience: [developer]
---

Pipeline code (`core/`, `transformations/`, `formatters/`, `mappers/`)
**never** imports `ovid2.display` and never prints. It emits events, and
renderers subscribe.

## Emitting

Build every event with a **typed helper** from `ovid2.core.events`, never
as a raw `Event(kind="…", payload={…})`. This keeps Python and the JSON
Schema in step.

## Envelope

| Field | Type | Notes |
| --- | --- | --- |
| `v` | int | schema version |
| `kind` | string | dotted event name |
| `ts` | string | ISO 8601 UTC |
| `instance_id` | string | `YYYYMMDD-HHMMSS-<6hex>` |

## Kinds

| Kind | Required extra fields |
| --- | --- |
| `instance.started` | — |
| `instance.finished` | `extra.success` |
| `batch.received`, `batch.dispatched`, `batch.completed` | `pipeline`, `batch_id` |
| `batch.failed` | `pipeline`, `batch_id`, `error` |
| `table.started`, `table.completed` | `pipeline`, `batch_id`, `table` |
| `table.failed` | `pipeline`, `batch_id`, `table`, `error` |
| `stage.progress` | `stage` |
| `fix.applied` | `pipeline`, `batch_id`, `table`, `fix` |
| `fix.skipped` | — (fix ran but did not apply; `message` holds the reason) |
| `fix.failed` | `pipeline`, `batch_id`, `table`, `fix`, `error` |
| `log` | `level`, `message` |
| `warning`, `error` | `message` |

A `stage.progress` event that carries `table` and `extra.state`
(`waiting` / `active`, built by `ovid2.core.events.table_stage`) pauses or
resumes that table's run-page clock. Multi-phase runs use it — the
`legacy_ovid` runner maps every table, then fixes, then publishes — so a
table's time is the time it was actually worked on, and a table between
phases shows as *waiting* rather than running. Within phase 1 the same
event (state `active`) names the step a table is in: `format`, then each
pre-fix stage (`deduplicate`, `transform`, `map`), then `execute` — the
single lazy write that runs them all.

Every other pipeline (the one path in `core/pipeline.py`, every engine)
emits a per-table **stage enter** event (`ovid2.core.events.stage_entered`)
as the table reaches `read`, `format`, each chain stage
(`deduplicate` / `transform` / `map` / `fix`) and `write`. It carries
`extra.step = "enter"`, `extra.current` / `extra.total` and `extra.lazy`.
`lazy` is true before `write`, because those stages only build the plan.
These events never pause a clock. `run_items_svc` folds them into `stage`
items (`<table>/<stage>`) that the [Atlas](../03-web-panel/navigator.md)
animates.

An operator **pause** (`POST /api/v1/jobs/{id}/pause`) holds the run at
the next table boundary. While it holds, the run emits a run-level
`stage.progress` with `stage = "paused"` (no `table`):
`extra.state = "waiting"` when the hold starts and `"active"` when it
resumes (`run_paused` / `run_resumed`).

`legacy_ovid` ports report their fixes like native pipelines: one
`fix.applied` / `fix.skipped` / `fix.failed` per entry of the table's
legacy `fixes` dict, with `current_fix` / `total_fixes`, `fix_type`,
`src1`, `src2`. `multi_tables_fixer.py` entries use the table name
`multi_tables`.

Schema: `src/ovid2/resources/schemas/events.v1.json`. Enforced by
`test_event_schema.py`.

## The log file

Each run appends JSON lines to `WORK_DIR/events-<instance_id>.jsonl`.
Renderers tail that file by byte offset, so a reader can always restart
and catch up.

## Shipped renderers

| Renderer | Module |
| --- | --- |
| plain stderr lines | `ovid2.display.renderers.plain_log` |
| daily JSONL archive | `ovid2.display.renderers.json_file` |
| Rich terminal UI | `ovid2.display.renderers.tui` (`ovid2 watch`) |
| e-mail digest | `ovid2.display.renderers.email_digest` |
| web live canvas | `ovid2.web` SSE + `static/display/` widgets |

To write a new renderer, tail the JSONL with `ovid2.display.reader` and
handle the kinds you care about. Ignore unknown kinds; the schema can gain
new ones without bumping `v`.
