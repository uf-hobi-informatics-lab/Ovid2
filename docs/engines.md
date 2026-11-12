# Compute engines

Ovid2 runs a pipeline on one of four engines. Choose one per run with
`ovid2 etl --engine <name>`, the **Engine** field on the web Submit page, or
MCP `submit_run(engine=...)`. The default is `pyspark`.

| Engine | Family | Where compute runs | Best for |
|---|---|---|---|
| `pyspark` | spark | local `local[N]` or an isolated per-run Spark cluster | every pipeline; very large batches across many workers |
| `databricks_spark` | spark | a Databricks cluster (Connect) | data already in Databricks |
| `snowpark` | snowpark | inside Snowflake | Snowflake ↔ Snowflake routes |
| `duckdb` | duckdb | in-process, vectorised, one host | batches that fit on one host (fastest start; no JVM / cluster) |

## DuckDB

DuckDB runs inside the ETL driver container. No Spark cluster is started.
It spills to disk past `memory_limit`, so data larger than RAM is fine,
but it uses only one host.

**Settings** (engine JSON `args`, or environment):

| Arg | Env | Default |
|---|---|---|
| `threads` | `OVID2_DUCKDB_THREADS` | every usable CPU |
| `memory_limit` | `OVID2_DUCKDB_MEMORY_LIMIT` | 85% of the container's `--memory` cap (set by `ovid2 etl`); else 75% of `min(host RAM, cgroup limit)` |
| — | `OVID2_DUCKDB_MEMORY_FRACTION` | `0.75` — share of host RAM the driver container may claim (`--memory`) |
| — | `OVID2_DUCKDB_CPUS` | every usable CPU — the container's `--cpus` |
| `temp_directory` | `OVID2_DUCKDB_TEMP_DIR` | per-run dir under the real-disk Spark scratch root |
| `database_path` | — | in-memory |
| `preserve_insertion_order` | — | `false` |
| `stage_sources` | `OVID2_DUCKDB_STAGE_SOURCES` | `true` (parse each text source once into scratch Parquet) |
| `table_concurrency` | `OVID2_DUCKDB_TABLE_CONCURRENCY` | the job's **Workers** (`--workers`), else `min(tables, CPUs)`; `1` = one table at a time |
| — | `OVID2_DUCKDB_MAX_LINE_SIZE` | 8 MiB (longest CSV record) |
| `scratch_codec` | `OVID2_DUCKDB_SCRATCH_CODEC` | `zstd` (scratch Parquet codec) |
| — | `OVID2_DUCKDB_PRESTAGE` | off — stage every source before the parallel phase |

**Container sizing and Workers.** DuckDB is one process, so a DuckDB
run is always **one** driver container — there are no worker containers
to add. That container is sized from the host, not from Spark's driver
heap: `--memory` = 75% of host RAM (`OVID2_DUCKDB_MEMORY_FRACTION`),
`--cpus` = every CPU (`OVID2_DUCKDB_CPUS`), and DuckDB's `memory_limit`
is 85% of that. The job's **Workers** field is the number of tables run
at once inside it (Spark: the number of worker containers). On a
96-core / 958 GB host with Workers = 25 that is a 718 GB container,
610 GB `memory_limit`, 96 threads, 25 concurrent tables. `ovid2 etl`
prints the chosen shape (`duckdb sizing: …`).

**Parallel tables.** A single DuckDB query rarely keeps every core
busy: CSV text writes, `DISTINCT ON` and small tables use only part of
the thread pool. `ovid2 etl` and the scheduler therefore run a DuckDB
batch in two phases:

1. **Reference tables first, one at a time, in CDM order.** These are
   the CDM's `reference_tables` (PCORnet: `demographic`, `provider`,
   `harvest`, `encounter`, `death`; OMOP: `location`, `care_site`,
   `provider`, `person`, `visit_occurrence`). Later tables' cross-table
   fixers (`patid_orphans`, `encounterid_orphans`, `replication`, …) read
   them by name, so they must be finished before anything else starts.
2. **Every other table at the same time, largest source first.** Each
   table runs on its own cursor of the shared database. Tables start in
   descending order of their formatter's source size (`table_sizes`, a
   file `stat` / object listing, never a scan), so the biggest table
   starts first instead of last. Up to `table_concurrency` tables run at
   once; all of them share one thread pool and one `memory_limit`.

Identical sources and checkpoints shared by several tables (the
PCORnet→OMOP `common` fan-in) are still built once. The first table
builds it and the others wait for it. Set `table_concurrency=1` to get
the old sequential loop, e.g. when very wide tables spill too much.
Spark, Snowpark and `legacy_ovid` pipelines always run sequentially in
CDM order. On the 2 GB PCORnet→OMOP batch (18 tables, 96 cores) a run
took 28 s sequentially and 18 s in parallel, with identical row sets.

**Performance notes.**

- **CSV reads** try DuckDB's parallel scanner in `strict_mode` first. It
  accepts quoted line breaks, which the lenient (`null_padding`) parallel
  scanner refuses. Files with ragged rows fall back to the lenient parse,
  in parallel if possible, otherwise single-threaded, and the result is
  identical either way. The rung that worked is remembered per file for
  the whole run. Parallel tables reading the same file probe it once
  (per-file lock); the others reuse the recorded rung.
- **Spark CSV parity.** `header=false` reads name columns `_c0.._cN`;
  headers like `"a", "b"` (space before the quote) are unquoted like
  Spark's `ignoreLeadingWhiteSpace`. An absent `optional: true` primary
  source yields an empty CDM-shaped table plus a warning, as on Spark.
- **Dedup** keeps incomplete-key rows in one hash pass (`DISTINCT ON`
  with a unique tag for NULL / empty keys) instead of filter + `UNION`,
  which made DuckDB run the formatter plan twice.
- **CSV writes** take DuckDB's own header (one write of the body, no
  re-copy) and only wrap string columns for the delimiter / line-break
  check; typed columns are cast straight to text. Doubles in Java's
  plain range use `CAST(x AS VARCHAR)` (identical to `Double.toString`);
  the regex rewrite runs only for scientific notation, zero, NaN, ±Inf.
- **Date parsing** of canonical, zero-padded values (`yyyy-MM-dd`,
  `yyyy-MM-dd HH:mm:ss`, …) uses the native `try_strptime`; every other
  shape falls back to the LEGACY `SimpleDateFormat` emulation. Both paths
  are fuzz-tested against each other.
- **Scratch Parquet** is `zstd`: same parse and scan time as `lz4_raw`
  on ACME, ~37% fewer bytes on a shared disk.
- On ACME (24 tables, 96 cores, ~280 GB in) these took a run from 636 s
  to 415 s with identical outputs, except tables whose dedup ties are
  arbitrary (same key set, different surviving row) and the
  timestamp-derived `GEOCODEID`.
- **Reference tables** (`demographic`, `encounter`, `provider`, `harvest`,
  `death`) are materialised once into scratch Parquet. Spark gets the same
  effect from `persist()`. Cross-table fixers therefore scan them instead
  of recomputing their plans.
- **Snowflake writes** produce a directory of ~200 MB Parquet parts and
  upload it with a single parallel `PUT` plus one `COPY INTO`, so
  Snowflake loads the parts in parallel.

**Storage support** (`drivers/<type>/duckdb.py`):

| Driver | Read | Write |
|---|---|---|
| disc | ✓ CSV / Parquet / JSON | ✓ same layout as Spark (`{table}.csv`) |
| s3, azure_blob, gcs | ✓ | ✓ `{prefix}/{table}.{fmt}` |
| snowflake | ✓ (Arrow) | ✓ CDM-typed table (`VARCHAR(N)`, `DATE`, `NUMBER`) |
| postgres | ✓ (DuckDB `postgres` extension) | ✓ |
| mssql, databricks | ✓ | ✓ (row batches; for moderate tables) |
| access | ✓ | — (read-only, as on Spark) |

**Semantics.** DuckDB reproduces the Spark production SQL semantics:
ANSI off, `timeParserPolicy=LEGACY` date parsing, lenient casts, and the
disc CSV read/write contract. A given table produces the same rows on both
engines, with two exceptions:

- **Surrogate ids** from `stable_hash` (OMOP `*_id` columns built from a
  hash) differ between engines. Do not switch an existing route's engine
  without re-deriving downstream ids.
- **Ties** that are arbitrary on Spark too can resolve differently, for
  example which duplicate row a dedup keeps, or which of several
  equally-dated `Maps to` targets is chosen.

**Pipelines that need Spark.** Pipelines whose custom formatters use
Spark-only APIs declare `REQUIRES_FAMILY = "spark"` in
`pipeline_settings.py` and are refused on DuckDB at plan time. A single
formatter can declare `requires_family = "spark"` instead, which fails
only that table with a clear message. A surrogate built from
`monotonically_increasing_id` is unique on both engines, but the values
differ between them.

**Writing portable code.** Import column functions from
`ovid2.core.engines.dsl` (`from ovid2.core.engines.dsl import col, when`;
`Window` comes from the same module),
or use `context.functions`, not `pyspark.sql.functions`. Use
`engine.union_by_name` / `drop_duplicates` / `checkpoint` for
DataFrame-shape helpers.
