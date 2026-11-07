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
| `fix.skipped` | — |
| `fix.failed` | `pipeline`, `batch_id`, `table`, `fix`, `error` |
| `log` | `level`, `message` |
| `warning`, `error` | `message` |

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
