---
title: Engines
summary: The four compute engines, what each is good at, how every engine reaches every storage driver, and the rules that keep results identical across them.
audience: [user, developer]
---

An **engine** is what actually computes a job. Choose it per run with
`--engine <name>` (CLI), the **Engine** chip on the Submit page, or
`submit_run(engine=…)` (MCP). The default is `pyspark`. **Every engine
runs every pipeline on every storage driver** — the choice is about speed
and where compute happens, never about what will run.

| Engine | Family | Where compute runs | Best for |
| --- | --- | --- | --- |
| `pyspark` | spark | an isolated per-run Spark cluster (or `local[N]`) | very large batches across many workers |
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
- **Very large data?** Use `pyspark`.
- **Source and destination both in Snowflake?** Use `snowpark`. Data never
  leaves the warehouse.
- **Source and destination in Databricks?** Use `databricks_spark`.

## One table at a time, on every engine

Every engine (`pyspark`, `databricks_spark`, `snowpark`, `duckdb`) runs a
pipeline's tables **one at a time**, in CDM order. The same applies to
`copy`, `move`, legacy ports and the scheduler. The running table gets the
engine's whole capacity:

- on DuckDB, every thread and the whole `memory_limit`;
- on Spark, the whole cluster;
- on Snowpark, the whole warehouse.

Because of this, the run page shows the same order and the same per-table
clocks whatever engine you use.

On every engine, `ovid2 etl` records a failed table and carries on with the
others. The scheduler stops after the first failure.

## What *Workers* means

| Engine | `--workers N` / *Workers* field |
| --- | --- |
| `pyspark` / `databricks_spark` | number of Spark worker containers in the run's cluster |
| `duckdb` | not used. The run is one host-sized container |
| `snowpark` | not used. Compute runs in the Snowflake warehouse |

The submit and pipeline forms only show *Workers* for Spark engines. The run
page does not show it.

## How a DuckDB run is sized

DuckDB is one process, so there are no worker containers to add. Before it
starts, `ovid2 etl` sizes the single driver container from the host:

| Container setting | Value | Override |
| --- | --- | --- |
| `--memory` | 75% of host RAM (at least 4 GB) | `OVID2_DUCKDB_MEMORY_FRACTION` |
| `--cpus` and DuckDB `threads` | every usable CPU | `OVID2_DUCKDB_CPUS` (narrows); `OVID2_DUCKDB_THREADS` |
| DuckDB `memory_limit` | 85% of the container cap; the rest is left for Python and Arrow | `OVID2_DUCKDB_MEMORY_LIMIT` |

It prints the chosen shape, for example:

```text
[ovid2]   duckdb sizing: 96 CPUs, 718 GB container (610 GB engine), one table at a time.
```

That example is a 96-core host with 958 GB of RAM.

The CDM's [reference tables](etl-stages.md#reference-tables-go-first) come
first in CDM order, so cross-table fixers in later tables
(`patid_orphans`, `encounterid_orphans`, `replication`, …) always find them.
They are materialised once into scratch Parquet, so later fixers scan them
instead of recomputing their plans. A source or checkpoint that several
tables share is built only once.

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
| `scratch_codec` | `OVID2_DUCKDB_SCRATCH_CODEC` | `zstd` | scratch Parquet codec: `zstd`, `lz4_raw`, `snappy` or `uncompressed` |
| — | `OVID2_DUCKDB_MAX_LINE_SIZE` | 8 MiB | longest CSV record accepted |
| `extension_directory` | `OVID2_DUCKDB_EXTENSION_DIR` | DuckDB's own | directory of pre-installed extensions, so a run never downloads one |

**Scratch codec.** Staged sources and checkpoints are written as scratch
Parquet. `zstd` costs the same parse and scan time as `lz4_raw` and
writes about a third fewer bytes, which matters on a shared disk.

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
| `access` | ✓ | — (read-only on every engine) |

## Same answer on every engine

DuckDB reproduces Spark's production semantics: ANSI mode off, LEGACY date
parsing, lenient casts, and the same CSV read/write contract. A table
produces the same rows on every engine, with these deliberate exceptions:

> [!WARNING]
> - **Hash-built surrogate ids** (OMOP `*_id` columns made with
>   `stable_hash`) differ between engines. Don't switch an existing
>   route's engine without re-deriving downstream ids.
> - **Arbitrary ties**, such as which of two identical-key rows dedup
>   keeps ("first in file order"), or which of several equally valid
>   `Maps to` concepts a one-to-many OMOP mapping picks, can resolve
>   differently. They are arbitrary on Spark too.
> - **`monotonically_increasing_id`** surrogates are unique on every
>   engine, but the values differ.

On Snowpark, Python `udf` / `pandas_udf` code runs as temporary Snowflake
Python UDFs, which is slower than on Spark or DuckDB. Literal lookup maps
with more than 512 entries also become UDF lookups there.

## Engine and storage compatibility

There are no engine-family gates: no pipeline, formatter or transform
declares a required engine, and generated `legacy_ovid` ports run on every
engine too. Each engine reaches each driver through a **stager**, either
natively or through the **Arrow bridge**:

| Driver | `pyspark` | `databricks_spark` | `duckdb` | `snowpark` |
| --- | --- | --- | --- | --- |
| `disc` | native | bridge | native | native read, bridge write |
| `s3`, `azure_blob` | native¹ | native¹ | native | native |
| `snowflake` | native¹ | native¹ | native | native |
| `gcs`, `databricks` | bridge | bridge | native | native |
| `postgres`, `mssql` | bridge | bridge | native | bridge |
| `access` | read native¹ | read bridge | read native | read bridge |

¹ When the connector's Java class is on the Spark classpath (it is in the
Ovid2 image). Otherwise that driver is bridged.

- **Access is read-only.** Every engine may read it; every engine refuses
  it as an output, at plan time.
- **The bridge.** A private DuckDB sidecar reads or writes the storage with
  the driver's DuckDB strategy and hands rows to the engine as Arrow
  batches. The bytes written are the same whichever engine produced them.
  It is slower than a native path, so a native path wins when one exists.
- **Remote engines.** `databricks_spark` and `snowpark` can't see the
  host's files, so `disc` and `access` always go through the bridge.

The plan-time storage check only refuses a read-only output, or a driver
added without a DuckDB strategy.

## Capabilities

Every function and step works on every engine, but a few behaviours differ.
Before a run starts (`ovid2 etl`) and before a job is queued (the web /
MCP submit), Ovid2 checks that the engine supports every capability the
run's steps need, and refuses the run with a message naming the step and the
missing capability.

| Capability | pyspark | databricks_spark | duckdb | snowpark |
| --- | :-: | :-: | :-: | :-: |
| Reproducible seeded random sampling (`rand_seeded`) | ✓ | ✓ | — | — |
| Sampling with replacement | ✓ | ✓ | — | ✓ |
| SHA-2 other than 256 bits | ✓ | ✓ | — | ✓ |
| Maps built from non-literal columns | ✓ | ✓ | — | — |
| Real per-row source file names | ✓ | — | ✓ | — |

Windows, regular expressions, checkpoints and broadcast joins work on every
engine. For example, a `random_sample` transform with a `seed` is refused on
DuckDB and Snowpark (drop the seed, or pick a Spark engine); without a seed it
runs everywhere.

Full settings for DuckDB and Snowpark (memory, threads, staging schemas)
are in [Environment variables](../10-reference/environment.md). How
Snowpark picks its scratch schema is explained in
[Health & EDC checks](../04-running-jobs/health-checks.md#any-engine).
