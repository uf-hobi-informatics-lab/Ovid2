---
title: Overwrite & batches
summary: Why re-running asks for confirmation, how to confirm on each surface, and what a batch id is.
audience: [user]
---

## Batch ids

The **batch id** labels one run's input. It is always the **full Data
Space path** of the input location:

| Input | Batch id |
| --- | --- |
| folder `landing/2026_03` in account `inbox` | `inbox/landing/2026_03` |
| Snowflake `CHAR_DEMO.PUBLIC` in account `warehouse` | `warehouse/CHAR_DEMO/PUBLIC` |

The web, API and MCP derive it for you, and the Submit page shows it
read-only. On the CLI you pass `--batch_id`. It is **only a label**: it
is never used to build a read or write path.

## The overwrite gate

Because the output address is the exact write location, running twice
writes to the same place. Before writing, Ovid2 checks whether the output
already holds tables, and asks you to confirm if it does.

| Surface | How to confirm |
| --- | --- |
| CLI | add `--overwrite` |
| Web Submit | the *Overwrite existing data?* page lists the tables. Confirm, or go back. |
| Web run page (launch / rerun) | an in-page dialog lists the tables. *Cancel* sends nothing. |
| REST API | without `confirm_overwrite: true` the API answers **`409 overwrite_required`** with the table list |
| MCP / assistant | the confirmation becomes a **`run.overwrite` approval** that a human must grant |
| Scheduled / watch configs | set `allow_overwrite` on the config |

Which tables count:

- `copy` and `move` count only the tables they will write;
- `health` counts only `dcq_results`;
- other kinds count every table at the output location.

> [!CAUTION]
> Overwriting replaces the tables at the output. If you need to keep the
> previous version, write to a new schema or folder instead. For example,
> include the refresh date in the output location.
