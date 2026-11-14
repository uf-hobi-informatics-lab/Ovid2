---
title: Submit — the job builder
summary: Build a job visually — pick a type and pipeline, a source and a target, set options, and submit.
audience: [user]
---

**Operate → Submit** (`/submit`) is a graphical job builder. You need the
`submit_jobs` capability (pipeline operator or above).

## Layout

- **Navigator (left).** Four sections, one open at a time. A section closes
  once you fill it in, and the next one opens.
- **Job graph (right).** *Source → Pipeline → Target*. Nodes are grey and
  dashed until you assign them, then turn green. Click a node to reopen
  its section.

## 1 · Job & pipeline

1. Pick a **job type** tile: ETL, Analysis, DQ checks, Health, Copy or
   Move. Each tile shows how many saved pipelines of that type exist.
2. Pick a **pipeline**. A package appears only under the job types that
   have a saved pipeline referencing it. If none match, a *Create one →*
   link opens the new-pipeline form.

Health types also list the generic EDC labels, such as `pcornet_edc`.
Copy and Move take a free-form job name. Changing the job type always
clears the chosen pipeline.

## 2 · Source and 3 · Target

Each section is a lazy tree of **saved addresses** and **storage
accounts**. Accounts expand into folders, or into databases and schemas,
and then into files or tables.

> [!IMPORTANT]
> **To select an item, click its icon.** The icon gets a green ring and a
> check badge. Clicking a **name** only opens or closes the node.
> (Files, tables and saved addresses can't be opened, so clicking their
> name selects them.)

Keyboard: <kbd>↑</kbd>/<kbd>↓</kbd> move, <kbd>→</kbd> expand,
<kbd>←</kbd> collapse, <kbd>Enter</kbd> select.

You can't select a warehouse account root. Clicking its icon opens the
account and asks you to pick a database or schema.

## 4 · Options (optional)

| Option | Applies to | Default |
| --- | --- | --- |
| Tables | ETL, Analysis, Copy/Move | `all` |
| Stages (transformations) | ETL, Analysis | `all` |
| Report cycle / checks | Health, DQ checks | the newest cycle |
| Engine | all | `settings.defaults.engine` (`pyspark`) |
| Workers | ETL, Analysis, Health, DQ checks on Spark / DuckDB | `settings.defaults.workers` (25) |
| Strict CDM type check | Move | off |
| *Advanced:* Snowflake ACCOUNT override | ETL, Analysis, Health, DQ checks on Snowpark | — |
| *Advanced:* Mirror events to stderr | ETL, Analysis, Health, DQ checks | off |
| *Advanced:* Extra bind mounts | Copy/Move | — |

Only the options that apply to the chosen job type and engine are shown;
the rest are hidden. The chips under the graph show the current values,
and highlight any you changed from the defaults.

## Batch id and submit

The read-only **Batch id** under the graph fills in from the chosen
source: its full Data Space path. **Submit job** stays disabled until every
node is green.

If the target already holds tables, you are asked to confirm the
overwrite. See [Overwrite & batches](../04-running-jobs/overwrite-and-batches.md).

After submitting, you land on the job's run page. See [Jobs & runs](jobs.md).
