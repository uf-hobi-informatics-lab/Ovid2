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

## How a DuckDB run is scheduled

DuckDB is one process, so there are no worker containers to add. A
single query rarely keeps every core busy, though: CSV text writes,
`DISTINCT ON` and small tables use only part of the thread pool. Ovid2
therefore runs several tables **side by side** inside the one container.
`ovid2 etl` and the scheduler both do this in two phases.

**The container.** Before it starts, `ovid2 etl` sizes the driver
container from the host:

| Container setting | Value | Override |
| --- | --- | --- |
| `--memory` | 75% of host RAM (at least 4 GB) | `OVID2_DUCKDB_MEMORY_FRACTION` |
| `--cpus` and DuckDB `threads` | every usable CPU | `OVID2_DUCKDB_CPUS` (narrows); `OVID2_DUCKDB_THREADS` |
| DuckDB `memory_limit` | 85% of the container cap; the rest is left for Python and Arrow | `OVID2_DUCKDB_MEMORY_LIMIT` |
| tables at once | the job's *Workers* (`--workers`); unset means one per CPU | `OVID2_DUCKDB_TABLE_CONCURRENCY` |

It prints the chosen shape, for example:

```text
[ovid2]   duckdb sizing: 96 CPUs, 718 GB container (610 GB engine), up to 25 concurrent tables (Workers).
```

That is a 96-core host with 958 GB of RAM and *Workers* = 25.

**Phase A: reference tables first, one at a time.** The CDM's
[reference tables](etl-stages.md#reference-tables-go-first) run first,
in CDM order, each with the whole thread pool. Cross-table fixers in later
tables (`patid_orphans`, `encounterid_orphans`, `replication`, …) read
them by name, so they have to be finished before anything else starts.
They are also materialised once into scratch Parquet, so later fixers
scan them instead of recomputing their plans.

**Phase B: every other table at once, largest first.** Each remaining
table runs on its own cursor of the shared DuckDB database. Tables start
in descending order of their formatter's source size. The size comes from
storage metadata only (a file `stat` or an object listing), never from a
scan. Starting the largest table first means it never sits at the back of
the queue while small tables finish. Up to *Workers* tables run at once.
They all share one thread pool and one `memory_limit`.

A source or checkpoint that several tables share is still built only
once. The first table builds it and the others wait for it.

> [!NOTE]
> Spark and Snowpark always run tables one at a time, in CDM order. On
> every engine, `ovid2 etl` records a failed table and carries on with
> the others. The scheduler stops starting new tables after the first
> failure, and tables already running finish.

> [!TIP]
> Set *Workers* to `1` (or `OVID2_DUCKDB_TABLE_CONCURRENCY=1`) to get the
> sequential loop back, for example when very wide tables spill too much.

## DuckDB tuning

The defaults suit most batches. Set these as engine JSON `args` (a named
engine under **Configure → Warehouses → Engines**) or as environment
variables on the host that runs `ovid2 etl`.

| Arg | Environment variable | Default | Effect |
| --- | --- | --- | --- |
| `threads` | `OVID2_DUCKDB_THREADS` | every usable CPU | DuckDB thread pool |
| `memory_limit` | `OVID2_DUCKDB_MEMORY_LIMIT` | 85% of the container's `--memory`; outside a container, 75% of `min(host RAM, cgroup limit)` | DuckDB spills to disk beyond this |
| — | `OVID2_DUCKDB_MEMORY_FRACTION` | `0.75` | share of host RAM the driver container may claim |
| — | `OVID2_DUCKDB_CPUS` | every usable CPU | the container's `--cpus` |
| `temp_directory` | `OVID2_DUCKDB_TEMP_DIR` | a per-run directory on the real-disk scratch root | spill directory |
| `database_path` | — | in-memory | on-disk database file |
| `preserve_insertion_order` | — | `false` | keep input row order (slower) |
| `stage_sources` | `OVID2_DUCKDB_STAGE_SOURCES` | `true` | parse each text source once into scratch Parquet |
| `table_concurrency` | `OVID2_DUCKDB_TABLE_CONCURRENCY` | *Workers*, else `min(tables, CPUs)` | tables at once (`1` = sequential) |
| `scratch_codec` | `OVID2_DUCKDB_SCRATCH_CODEC` | `zstd` | scratch Parquet codec: `zstd`, `lz4_raw`, `snappy` or `uncompressed` |
| — | `OVID2_DUCKDB_PRESTAGE` | off | stage every source before phase B |
| — | `OVID2_DUCKDB_MAX_LINE_SIZE` | 8 MiB | longest CSV record accepted |
| `extension_directory` | `OVID2_DUCKDB_EXTENSION_DIR` | DuckDB's own | directory of pre-installed extensions, so a run never downloads one |

**Scratch codec.** Staged sources and checkpoints are written as scratch
Parquet. `zstd` costs the same parse and scan time as `lz4_raw` and
writes about a third fewer bytes, which matters on a shared disk.

**Prestage.** With `OVID2_DUCKDB_PRESTAGE=1`, every source of the phase-B
tables is parsed into scratch, in parallel and largest first, before any
of those tables starts. The tables then read from the stage cache. It is
off by default because one slow, serial-parse source then holds every
table back. Letting tables stage on demand was faster in measurements.
Turn it on for feeds where many tables share a few large sources.

**CSV scanner ladder.** Each CSV source is read with the first scanner
that works:

1. the **parallel strict** scanner, which accepts quoted line breaks;
2. the **parallel lenient** scanner (pads ragged rows);
3. the **single-threaded lenient** scanner.

Strict mode fails on any row a lenient parse would repair, so whichever
rung succeeds, the rows are the same. The rung that worked is remembered
per file for the whole run. When parallel tables read the same file, one
of them probes it and the others reuse the result. Files that aren't
valid UTF-8 are transcoded with replacement characters, as Spark does, so
no row is lost.

**Snowflake writes.** A table written to Snowflake is executed once into
a directory of ~200 MB Parquet parts. It is uploaded with a single
parallel `PUT` and loaded with one `COPY INTO`, so Snowflake loads the
parts in parallel. The target is a CDM-typed table.

**Memory.** DuckDB measures memory from `/proc/meminfo`, which inside a
container reports the *host's* RAM. That is why `ovid2 etl` always passes
an explicit `memory_limit` below the container's cap. Without it the
container would be killed for running out of memory before DuckDB ever
spilled. If you raise `OVID2_DUCKDB_MEMORY_LIMIT`, keep it below the
container's `--memory`.

### Storage DuckDB can stage

Each driver ships a DuckDB staging strategy (`drivers/<type>/duckdb.py`):

| Driver | Read | Write |
| --- | --- | --- |
| `disc` | ✓ CSV / Parquet / JSON | ✓ same layout as Spark (`<table>.csv`) |
| `s3`, `azure_blob`, `gcs` | ✓ | ✓ `<prefix>/<table>.<fmt>` |
| `snowflake` | ✓ (Arrow) | ✓ CDM-typed table (`VARCHAR(n)`, `DATE`, `NUMBER`) |
| `postgres` | ✓ (DuckDB `postgres` extension) | ✓ |
| `mssql`, `databricks` | ✓ | ✓ (row batches, best for moderate tables) |
| `access` | ✓ | — (read-only, as on Spark) |

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
are in [Environment variables](../10-reference/environment.md). How
Snowpark picks its scratch schema is explained in
[Health & EDC checks](../04-running-jobs/health-checks.md#any-engine).
