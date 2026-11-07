---
title: Engines
summary: The four compute engines, what each is good at, and the rules that keep results identical across them.
audience: [user, developer]
---

An **engine** is what actually computes a job. Choose it per run with
`--engine <name>` (CLI), the **Engine** chip on the Submit page, or
`submit_run(engine=…)` (MCP). The default is `pyspark`.

| Engine | Family | Where compute runs | Best for |
| --- | --- | --- | --- |
| `pyspark` | spark | an isolated per-run Spark cluster (or `local[N]`) | every pipeline; very large batches across many workers |
| `databricks_spark` | spark | a Databricks cluster via Connect | data that already lives in Databricks |
| `snowpark` | snowpark | inside Snowflake | Snowflake → Snowflake routes, PCORnet → OMOP in-warehouse |
| `duckdb` | duckdb | in-process, vectorised, one host | batches that fit on one host — fastest start, no JVM, no cluster |

A named **engine config** in `$XDG_CONFIG_HOME/ovid2/engines/<name>.json`
can pin settings, for example a Snowpark engine bound to a Snowflake
connection:

```json
{ "type": "snowpark", "args": { "connection": "warehouse" } }
```

Manage engines in the panel under **Configure → Warehouses → Engines**.

## How to choose

- **Everything fits on one machine?** Use `duckdb`. It spills to disk
  beyond its memory limit and typically finishes small and medium batches
  fastest.
- **Very large data, or a pipeline with Spark-only custom formatters?**
  Use `pyspark`.
- **Source and destination both in Snowflake?** Use `snowpark`. Data never
  leaves the warehouse.
- **Source and destination in Databricks?** Use `databricks_spark`.

## What *Workers* means

| Engine | `--workers N` / *Workers* field |
| --- | --- |
| `pyspark` | number of Spark worker containers in the run's cluster |
| `duckdb` | number of tables processed **at once** in the single driver container |
| `snowpark` / `databricks_spark` | compute is remote; the field only bounds local concurrency |

A DuckDB run is always **one** container. It is sized from the host (75%
of RAM and every CPU by default), not from Spark's driver heap.

## Same answer on every engine

DuckDB reproduces Spark's production semantics: ANSI mode off, LEGACY date
parsing, lenient casts, and the same CSV read/write contract. A table
produces the same rows on either engine, with two deliberate exceptions:

> [!WARNING]
> - **Hash-built surrogate ids** (OMOP `*_id` columns made with
>   `stable_hash`) differ between engines. Don't switch an existing
>   route's engine without re-deriving downstream ids.
> - **Arbitrary ties**, such as which of two identical-key rows dedup
>   keeps, can resolve differently. They are arbitrary on Spark too.

## Engine and storage compatibility

Each engine reaches each driver through a **stager**. A plan-time gate
refuses impossible pairs **before** any cluster starts. For example, the
DuckDB-only drivers (`postgres`, `mssql`, `gcs`, `databricks`) can't be
paired with `pyspark`. A pipeline that declares `REQUIRES_FAMILY =
"spark"` is refused on DuckDB.

Full settings for DuckDB and Snowpark (memory, threads, staging schemas)
are in [Environment variables](../10-reference/environment.md).
