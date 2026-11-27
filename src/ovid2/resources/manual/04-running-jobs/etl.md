---
title: ETL runs
summary: Run the ETL pipeline (format → deduplicate → transform → map → fix) — choose tables, stages, engine and workers, and understand what you get back.
audience: [user]
---

An `etl` job runs a pipeline package's formatters, mappers and fixers to
produce CDM tables. The stages are explained in
[The ETL stages](../02-concepts/etl-stages.md).

## Required inputs

| Field | CLI flag | Notes |
| --- | --- | --- |
| Pipeline | `--pipeline` | an installed package (`ovid2 pipeline list`) |
| Input address | `--input_address` | the **exact** folder or schema holding the raw extract |
| Output address | `--output_address` | the **exact** folder or `DATABASE/SCHEMA` to write to |
| Batch id | `--batch_id` | CLI only: a label, normally the input's Data Space path. The web derives it for you. |

## Options

| Option | CLI | Default | Notes |
| --- | --- | --- | --- |
| Tables | `--tables` | `all` | names from the CDM, e.g. `demographic encounter` |
| Stages | `--transformations` | `all` | any of `format deduplicate transform map fix`, applied in canonical order (`transform` runs the chains in `transforms/assignments.yaml`; a no-op when none) |
| Engine | `--engine` | `pyspark` | or a named engine config. See [Engines](../02-concepts/engines.md). |
| Workers | `--workers` | 25 | Spark worker containers. Ignored by DuckDB / Snowpark. |
| CDM | `--cdm` | from `pipeline_settings.cdm` | override, rarely needed |
| Overwrite | `--overwrite` | off | required if the output already holds tables |
| Local | `--local` | off | in-process, no Docker (development) |
| Log to stderr | `--stderr-log` | off | mirror every event as a plain line |

## Typical recipes

```bash
# Re-run only the fix stage's tables after changing a fixer chain
ovid2 etl --pipeline mysite --input_address … --output_address … \
  --batch_id … --tables diagnosis procedures --overwrite

# Format only, to inspect raw shaping without mapping or fixes
ovid2 etl … --transformations format --tables demographic

# Small batch, single host, fastest start (no Workers: one host-sized container)
ovid2 etl … --engine duckdb
```

## What you get

- One table per CDM table processed, written at the exact output address.
  On warehouses each column gets its CDM type and length.
- An **instance id** and an event log; the run page shows per-table
  status, timing, fixes and errors.
- On failure, the tables that finished are still written; failed tables
  are listed with their error, both in the run's timeline and on the
  console.

> [!TIP]
> Every pipeline, `legacy_ovid` ports included, runs on every engine and
> every storage driver. The only pairing refused **at submit time** is a
> read-only output (an Access database). See
> [Engines](../02-concepts/engines.md#engine-and-storage-compatibility).

## Before production

1. `ovid2 pipeline validate --pipeline <name>` loads every spec.
2. Run once with `--transformations format` against a sample batch, and
   check the output in the [Data Space](../03-web-panel/dataspace.md).
3. Run the full chain, then a [health check](health-checks.md) on the
   output.
