---
title: Environment variables
summary: Every environment variable Ovid2 reads, grouped by purpose, with its default.
audience: [admin, developer]
---

Set these in the shell, in `/etc/ovid2/web.env` / `mcp.env` for
services, or with `eval "$(ovid2 dev env)"` for dev instances. Variables
not listed here are internal; don't rely on them.

## Paths

| Variable | Default | Meaning |
| --- | --- | --- |
| `XDG_CONFIG_HOME` | `~/.config` | parent of `ovid2/` configuration |
| `XDG_STATE_HOME` | `~/.local/state` | parent of `ovid2/` state (`scheduler.db`, archive) |
| `XDG_CACHE_HOME` | `~/.cache` | caches |
| `XDG_RUNTIME_DIR` | `/run/user/<uid>`, else `/tmp/ovid2-<uid>` | event logs, per-run dirs |
| `OVID2_CONFIGS_DIR` | `$XDG_CONFIG_HOME/ovid2/configs` | addresses |
| `OVID2_CONNECTIONS_DIR` | `…/connections` | storage accounts |
| `OVID2_ENGINES_DIR` | `…/engines` | engines |
| `OVID2_LLMS_DIR` | `…/llms` | assistant model connections |
| `OVID2_SETTINGS_FILE` | `…/settings.json` | panel and worker settings |
| `OVID2_SCHEDULER_DB` | `$XDG_STATE_HOME/ovid2/scheduler.db` | jobs, audit, users |
| `OVID2_ARCHIVE_DIR` | under the state dir | daily event archive |
| `OVID2_WORK_DIR` | `$XDG_RUNTIME_DIR/ovid2` | per-run work dirs and event logs |
| `OVID2_MATERIAL_DIR` | `$XDG_DATA_HOME/ovid2/material` | OMOP vocabulary, geocoding lists |
| `OVID2_DATA_DIR` | `$XDG_DATA_HOME/ovid2/data` | raw batches (legacy layout) |
| `OVID2_PIPELINE_PACKAGES_DIR` | auto-detected `pipelines/` in a checkout | directory holding `ovid2_pipelines/` (CLI and web) |
| `OVID2_PIPELINES_DIR` | XDG `pipelines/` | saved pipeline-config JSONs; also accepted as the packages dir when it contains `ovid2_pipelines/` |
| `OVID2_WEB_LOG_DIR` | under the state dir | job console logs |
| `OVID2_WEB_EXPORT_DIR` | `$XDG_STATE_HOME/ovid2/web_exports` | Data Space export files |
| `OVID2_REPORTS_DIR` | `$XDG_STATE_HOME/ovid2/reports` | Report Studio sources (durable, aggregated run datasets) |
| `OVID2_REPORT_EXPORT_DIR` | `$XDG_STATE_HOME/ovid2/report_exports` | Report Studio export files (1 h) |

## Execution

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_DOCKER_IMAGE` | `ovid2:latest` | image jobs run in |
| `OVID2_EXECUTION_MODE` | (container) | `inprocess` = same as `ovid2 etl --local` |
| `OVID2_SPARK_MODE` | `cluster` | `local` uses `local[N]` instead of a per-run cluster |
| `OVID2_SPARK_CORES` | every usable CPU | `local[N]` core count |
| `OVID2_SPARK_MEMORY_FRACTION` | `0.75` | share of host RAM the Spark containers may use |
| `OVID2_SPARK_DRIVER_MEMORY` | derived | Spark driver heap override |
| `OVID2_SPARK_LOCAL_DIR` | real-disk scratch | Spark spill and scratch root |
| `OVID2_HOST_MEMORY_GB` / `OVID2_HOST_CPUS` | detected | override host sizing |
| `OVID2_DEDUP_SALT_BUCKETS` | `32` | salted-dedup partitions |
| `OVID2_DEDUP_NULL_KEY_PASSTHROUGH` | `true` | keep null-key rows out of dedup |
| `OVID2_DISC_WRITE_PARTITIONS` | engine default | output partitions for disc writes |
| `OVID2_COPY_TABLE_CONCURRENCY` | `8` | tables copied at once |
| `OVID2_COPY_ORDER` | largest first | `as_given` keeps the listed order |
| `OVID2_TIMEZONE` | `America/New_York` | timezone for executor timestamps (e.g. `UPDATED` columns) |

### DuckDB

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_DUCKDB_THREADS` | every usable CPU | DuckDB threads |
| `OVID2_DUCKDB_MEMORY_LIMIT` | 85% of the container cap | DuckDB `memory_limit` |
| `OVID2_DUCKDB_MEMORY_FRACTION` | `0.75` | share of host RAM for the driver container |
| `OVID2_DUCKDB_CPUS` | every usable CPU | the container's `--cpus` |
| `OVID2_DUCKDB_TEMP_DIR` | per-run scratch | spill directory |
| `OVID2_DUCKDB_STAGE_SOURCES` | `true` | parse each text source once into scratch Parquet |
| `OVID2_DUCKDB_TABLE_CONCURRENCY` | Workers, else `min(tables, CPUs)` | tables at once (`1` = sequential) |
| `OVID2_DUCKDB_MAX_LINE_SIZE` | 8 MiB | longest CSV record |
| `OVID2_DUCKDB_SCRATCH_CODEC` | `zstd` | scratch Parquet codec (`zstd` / `lz4_raw` / `snappy` / `uncompressed`) |
| `OVID2_DUCKDB_PRESTAGE` | off | stage every source before the parallel phase (opt-in) |

### Snowpark

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_SNOWPARK_STAGING_SCHEMA` | `<db>.OVID2_SNOWPARK_STAGING` | scratch schema for temp views and staging |
| `OVID2_SNOWPARK_STAGING_KEEP` | off | keep staging objects for debugging |
| `OVID2_SNOWPARK_STAGING_{S3,AZURE_BLOB,GCS}_INTEGRATION` | — | storage integration names for external stages |

## Web, worker and MCP

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_WEB_BIND` | `127.0.0.1:8090` | panel bind address |
| `OVID2_WEB_URL` | `http://$OVID2_WEB_BIND` | URL executors and MCP use to reach the API |
| `OVID2_WEB_MAX_PARALLEL_JOBS` | `1` | worker concurrency |
| `OVID2_WEB_WORKER_POLL_SECONDS` | `2.0` | worker poll interval |
| `OVID2_MCP_BIND` | `127.0.0.1:8091` | MCP HTTP bind |
| `OVID2_MCP_API_TOKEN` | — | MCP service-principal bearer token |
| `OVID2_LLM_TOOLS_API_URL` | the server's own socket | where assistant tool calls reach the API |
| `OVID2_MANUAL_RELOAD` | off | `1` re-reads manual sources on every request (authoring) |

## Data Space and exports

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_EXPORT_MAX_ROWS` | `100000` | immediate CSV / Excel download cap; larger exports run in a container |
| `OVID2_EXPORT_DOC_MAX_ROWS` | `5000` | PDF / Word cap |
| `OVID2_BROWSE_DEADLINE_SECONDS` | built-in | storage browse timeout |
| `OVID2_UCANACCESS_JARS` | `$SPARK_HOME/jars` | UCanAccess jars for host-side Access browsing |

## Secrets and security

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_SECRETS_SCOPE_MODE` | `warn` | `enforce` makes executor secret scopes exclusive |
| `OVID2_SECRETS_DIR` | set by Ovid2 | the scoped secret directory of an executor. Don't set it yourself. |
| `OVID2_SECRETS_FILE` | — | single-file override for CI. Never forwarded; ignored under `enforce`. |
| `OVID2_WORKER_ENV_ALLOWLIST` | built-in list | extra variables forwarded to executor subprocesses |

## Dev instances

`OVID2_DEV_HOST`, `OVID2_DEV_PORT_RANGE`, `OVID2_DEV_HOME`,
`OVID2_DEV_REGISTRY`, `OVID2_DEV_BASE_IMAGE`. See
[Dev instances](../01-getting-started/dev-instances.md).
