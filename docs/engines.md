# Compute engines

Ovid2 runs a pipeline on one of four engines. Choose one per run with
`ovid2 etl --engine <name>`, the **Engine** field on the web Submit page, or
MCP `submit_run(engine=...)`. The default is `pyspark`. Every engine runs
every pipeline on every storage driver — see
[Engine universality](#engine-universality).

| Engine | Family | Where compute runs | Best for |
|---|---|---|---|
| `pyspark` | spark | local `local[N]` or an isolated per-run Spark cluster | very large batches across many workers |
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
| — | `OVID2_DUCKDB_MAX_LINE_SIZE` | 8 MiB (longest CSV record) |
| `scratch_codec` | `OVID2_DUCKDB_SCRATCH_CODEC` | `zstd` (scratch Parquet codec) |

**Container sizing.** DuckDB is one process, so a DuckDB run is always
**one** driver container — there are no worker containers and no
*Workers* knob (the submit form hides it). The container is sized from the
host, not from Spark's driver heap: `--memory` = 75% of host RAM
(`OVID2_DUCKDB_MEMORY_FRACTION`), `--cpus` = every CPU
(`OVID2_DUCKDB_CPUS`), and DuckDB's `memory_limit` is 85% of that. On a
96-core / 958 GB host that is a 718 GB container, 610 GB `memory_limit`,
96 threads. `ovid2 etl` prints the chosen shape (`duckdb sizing: …`).

**One table at a time, on every engine (2026-11-24).** Every engine —
`pyspark`, `databricks_spark`, `snowpark`, `duckdb` — runs a pipeline's
tables sequentially in CDM order (reference tables first by construction).
So do `copy`, `move`, legacy ports and the scheduler. The running table
gets the engine's whole capacity: on DuckDB every thread and the whole
`memory_limit`, on Spark the whole cluster, on Snowpark the whole
warehouse. Engines therefore report the same order and the same per-table
timings on the run page. `core/table_runner.TABLE_CONCURRENCY` is `1`,
pinned by `tests/contract/test_tables_run_serially.py`. *Workers* sizes
only a Spark cluster (`config.engines.engine_uses_workers`). Identical
sources and checkpoints shared by several tables (the PCORnet→OMOP
`common` fan-in) are still built once.

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
| access | ✓ | — (read-only on every engine) |

**Semantics.** DuckDB reproduces the Spark production SQL semantics:
ANSI off, `timeParserPolicy=LEGACY` date parsing, lenient casts, and the
disc CSV read/write contract. A given table produces the same rows on every
engine, with two exceptions:

- **Surrogate ids** from `stable_hash` (OMOP `*_id` columns built from a
  hash) differ between engines. Do not switch an existing route's engine
  without re-deriving downstream ids.
- **Ties** that are arbitrary on Spark too can resolve differently, for
  example which duplicate row a dedup keeps, or which of several
  equally-dated `Maps to` targets is chosen.

**Residual differences.** A surrogate built from
`monotonically_increasing_id` is unique on every engine, but the values
differ between them. See [Engine universality](#engine-universality) for
the full list.

**Writing portable code.** Import column functions from
`ovid2.core.engines.dsl` (`from ovid2.core.engines.dsl import col, when`;
`Window`, `udf` and `pandas_udf` come from the same module),
or use `context.functions`, not `pyspark.sql.functions`. Use
`engine.union_by_name` / `drop_duplicates` / `checkpoint` for
DataFrame-shape helpers.

## Engine universality

**Every engine runs every pipeline on every storage driver.** That covers
YAML and custom formatters, transforms, mappers, fixers, health checks and
the generated `legacy_ovid` byte-parity ports. There are no engine-family
gates: pipelines, formatters and transforms do not declare a required
family, and no run is refused because of its engine
(`tests/contract/test_no_engine_family_gates.py` forbids reintroducing
`REQUIRES_FAMILY` / `requires_family`). The one storage rule left is that
**Access is read-only**: it is accepted as an input on every engine and
refused as an output on every engine (`Driver.read_only = True`).

This works because all shared and pipeline code is written against one
PySpark-shaped surface, `src/ovid2/core/engines/surface.py` (column
`FUNCTIONS` plus the DataFrame / Column / GroupedData / Window / Session /
Reader methods). Shared code imports it from `ovid2.core.engines.dsl`;
pipeline packages never import `pyspark`. Generated legacy ports import
`ovid2.core.engines.sparkapi`, a portable `pyspark` stand-in (the legacy
migrator rewrites the imports). Every engine implements the whole surface
(`tests/contract/test_engine_surface_complete.py`).

### Engine × storage

| Driver | `pyspark` | `databricks_spark` | `duckdb` | `snowpark` |
|---|---|---|---|---|
| disc | native | bridge | native | native read, bridge write |
| s3 | native¹ | native¹ | native | native |
| azure_blob | native¹ | native¹ | native | native |
| gcs | bridge | bridge | native | native |
| snowflake | native¹ | native¹ | native | native |
| databricks | bridge | bridge | native | native |
| postgres | bridge | bridge | native | bridge |
| mssql | bridge | bridge | native | bridge |
| access | native¹ read; read-only output | bridge read; read-only output | native read; read-only output | bridge read; read-only output |

¹ Native only when the connector's JVM class is on the session classpath
(hadoop-aws, hadoop-azure, spark-snowflake, UCanAccess — all baked into the
Ovid2 image). A bare `pip install pyspark` host without the jars bridges
that driver instead of failing with `ClassNotFoundException`.

*Native* means the engine's own data plane: for Spark the driver's
`spark.py` (`spark_capable` drivers); for DuckDB the driver's
`duckdb.py`; for Snowpark a server-side staging strategy under
`drivers/snowflake/staging/` (disc, s3, azure_blob, databricks, gcs) or
the Snowflake driver itself. `databricks_spark` and `snowpark` set
`supports_local_files = False` — a remote session cannot see this host's
files — so disc and access go through the bridge. Snowpark disc *writes*
always take the bridge, which writes the same bytes as Spark's CSV writer.

`tests/contract/test_engine_storage_matrix_complete.py` checks that every
engine × driver pair is accepted by the stagers and by the plan-time
compatibility check (named engine configs resolve to their type).

### The Arrow bridge

`src/ovid2/core/engines/bridge.py` fills every gap in the native matrix.
It attaches a private DuckDB *sidecar* engine to the running engine and
uses the driver's DuckDB strategy (`drivers/<type>/duckdb.py`):

- **Read** — the DuckDB strategy materialises the source with the usual
  read contract (all `VARCHAR`, trimmed, empty → NULL) and streams it as
  Arrow record batches into the engine (`createDataFrame` on Spark, a
  transient table on Snowpark).
- **Write** — the engine's frame is exported as Arrow into a DuckDB frame
  and persisted by the DuckDB strategy, so the output bytes / typed DDL
  are the same whichever engine produced them.

The bridge moves data through the driver container's memory, so a native
path is preferred whenever one exists; it is a reach mechanism, not a
fast path.

### Residual differences between engines

Engines agree row-for-row except for choices that are arbitrary on Spark
too:

- **Dedup ties** — "first in file order" between key-identical rows can
  keep a different row.
- **Surrogate ids** — `stable_hash` and `monotonically_increasing_id`
  values differ between engines. Do not switch an existing route's engine
  without re-deriving downstream ids.
- **One-to-many `Maps to`** — PCORnet→OMOP `condition_occurrence` breaks
  ties between equally valid concepts differently (~30 of 1599 rows in the
  reference batch).
- **Python UDFs on DuckDB** — a `udf` / `pandas_udf` holds Python's GIL
  for every batch, so a UDF-heavy query uses one core whatever
  `OVID2_DUCKDB_THREADS` says. Hot shared helpers have native SQL
  equivalents instead: the legacy id encryption
  (`utils.common.encrypt_id_udf`) runs as `_DuckFunctions.xor_base64`
  (byte-identical; non-string inputs keep the UDF).
- **Snowpark UDFs** — Python `udf` / `pandas_udf` run as temporary
  Snowflake Python UDFs (Ovid2 modules are pickled by value), with lower
  throughput than on Spark or DuckDB. Literal `create_map` lookups with
  more than 512 entries also become UDF lookups on Snowpark.
- **Bridged object stores on bare Spark hosts** — without the connector
  jars, s3 / azure_blob are bridged (correct, but slower).

### Capabilities

Every algorithm declares the engine capabilities it needs (`requires`), and
every engine declares the capabilities it has (`capabilities()`). The run is
checked **at plan time** — `ovid2 etl` before any cluster starts (exit code
`2`) and `jobs_svc.submit()` before the job is queued (HTTP 400) — so a
runtime `EngineOperationUnsupported` is a bug, not an expected failure.

The vocabulary is the closed enum `Capability`
(`ovid2.config.capabilities`, re-exported by `ovid2.core.engines.protocols`):

- **Functions** — `fn:<name>`, one per column function in
  `core/engines/surface.py::FUNCTIONS` plus the Ovid2 extensions
  (`stable_hash`, `map_from_dict`, `to_double`). Every engine implements all
  of them, so they never refuse a plan; they make each algorithm's
  declaration exact.
- **Features** — behaviours whose support really differs:

| Capability | pyspark | databricks_spark | duckdb | snowpark | Meaning |
| --- | :-: | :-: | :-: | :-: | --- |
| `window` | ✓ | ✓ | ✓ | ✓ | Window functions (`.over(...)`) |
| `regex` | ✓ | ✓ | ✓ | ✓ | `regexp_*` / `rlike` |
| `checkpoint` | ✓ | ✓ | ✓ | ✓ | `engine.checkpoint(df, name)` |
| `broadcast_join` | ✓ | ✓ | ✓ | ✓ | `broadcast(df)` join hint (no-op where the planner decides) |
| `rand_seeded` | ✓ | ✓ | — | — | A seed gives reproducible random draws / samples |
| `sample_with_replacement` | ✓ | ✓ | — | ✓ | `sample(withReplacement=True)` |
| `sha2_all_bits` | ✓ | ✓ | — | ✓ | `sha2` with 224 / 384 / 512 bits (SHA-256 is everywhere) |
| `map_from_columns` | ✓ | ✓ | — | — | `create_map` over non-literal columns (literal maps are everywhere) |
| `input_file_name` | ✓ | — | ✓ | — | A real per-row source file path |

The per-engine sets live in `ENGINE_CAPABILITIES` (web-safe) and
`BaseEngine.capabilities()`; contracts keep them equal
(`test_algorithm_requires_declared.py`).

**Adding a capability.** Add a member to `Capability` (features) — or a name
to `surface.FUNCTIONS` (functions; the `fn:` member is generated and pinned
by contract) — add it to `ENGINE_CAPABILITIES` for the engines that have it,
teach `ovid2.algorithms.requirements` to detect its use if it is
syntactically visible, and add a row to the table above. An algorithm that
needs it declares it in `REQUIRES` (or `requires_for(params)` when only some
parameters need it) and in the web-safe mirror
`config/algorithm_requirements.py`.

### Adding an engine or a driver

- **New driver:** ship at least a DuckDB staging strategy
  (`drivers/<type>/duckdb.py`, mapped in
  `DuckDBEngineStager._STRATEGY_MODULES`). With it, every engine reaches
  the driver through the bridge; a `spark.py` or a Snowpark staging
  strategy is an optional native fast path.
- **New engine:** implement all of `surface.py`, a stager, Arrow import /
  export in `bridge.py` (`import_arrow` / `export_arrow`), declare its
  `ENGINE_CAPABILITIES` entry (every `fn:*` plus the features it supports),
  and pass
  `test_engine_surface_complete.py` and
  `test_engine_storage_matrix_complete.py`.
