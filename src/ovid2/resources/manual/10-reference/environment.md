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
| `OVID2_TRANSFORMS_DIR` | `$XDG_CONFIG_HOME/ovid2/transforms` | user transform library (chains shared across pipelines) |
| `OVID2_PACKS_DIR` | `$XDG_CONFIG_HOME/ovid2/packs` | folder [domain packs](../09-developers/domain-packs.md) (YAML-only) |
| `OVID2_PACKS_ENABLED` | *(unset)* | comma-separated domain-pack ids to enable (`*` = all discovered); adds to `$XDG_CONFIG_HOME/ovid2/packs.yaml` |
| `OVID2_SETTINGS_FILE` | `…/settings.json` | panel and worker settings |
| `OVID2_SCHEDULER_DB` | `$XDG_STATE_HOME/ovid2/scheduler.db` | jobs, audit, users |
| `OVID2_ARCHIVE_DIR` | under the state dir | daily event archive |
| `OVID2_WORK_DIR` | `$XDG_RUNTIME_DIR/ovid2` | per-run work dirs and event logs |
| `OVID2_MATERIAL_DIR` | `$XDG_DATA_HOME/ovid2/material` | OMOP vocabulary, geocoding lists |
| `OVID2_CREDENTIALS_DIR` | `$XDG_DATA_HOME/ovid2/credentials` | host credential directory mounted read-only into run containers |
| `OVID2_DATA_DIR` | `$XDG_DATA_HOME/ovid2/data` | raw batches (legacy layout) |
| `OVID2_PIPELINE_PACKAGES_DIR` | auto-detected `pipelines/` in a checkout | directory holding `ovid2_pipelines/` (CLI and web) |
| `OVID2_PIPELINES_DIR` | XDG `pipelines/` | saved pipeline-config JSONs; also accepted as the packages dir when it contains `ovid2_pipelines/` |
| `OVID2_WORKFLOWS_DIR` | XDG `workflows/` | saved workflow JSONs (chains of pipeline configs) |
| `OVID2_WEB_LOG_DIR` | under the state dir | job console logs |
| `OVID2_WEB_EXPORT_DIR` | `$XDG_STATE_HOME/ovid2/web_exports` | Data Space export files |
| `OVID2_REPORTS_DIR` | `$XDG_STATE_HOME/ovid2/reports` | Report Studio sources (durable, aggregated run datasets) |
| `OVID2_REPORT_EXPORT_DIR` | `$XDG_STATE_HOME/ovid2/report_exports` | Report Studio export files (1 h) |
| `OVID2_VOCAB_CACHE_DIR` | `.ovid2_parquet/` next to the source file, when writable | root of the vocabulary Parquet cache |
| `OVID2_ACCESS_MIRROR_DIR` | the image's scratch dir; outside the image, `<tmp>/ovid2_access_mirror` | writable directory for UCanAccess's HSQLDB mirror of an Access file, so a read-only `.accdb` is never written to |
| `OVID2_DOCKER_CONTEXT` | the checkout containing `docker/Dockerfile` | build context for image builds from the panel |

## Execution

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_DOCKER_IMAGE` | `ovid2:latest` | image jobs run in |
| `OVID2_EXECUTION_MODE` | (container) | `inprocess` = same as `ovid2 etl --local` |
| `OVID2_SPARK_MODE` | `cluster` | `local` uses `local[N]` instead of a per-run cluster |
| `OVID2_SPARK_CORES` | every usable CPU | `local[N]` core count |
| `OVID2_SPARK_MEMORY_FRACTION` | `0.75` | share of host RAM the Spark containers may use |
| `OVID2_SPARK_DRIVER_MEMORY` | derived | Spark driver heap override |
| `OVID2_SPARK_LOCAL_DRIVER_MEMORY_GB` | half the host's Spark budget, clamped | `local[N]` driver heap in GiB |
| `OVID2_SPARK_ALLOW_OVERSUBSCRIBE` | off | `1` lets `--workers` exceed what the host fits (otherwise capped) |
| `OVID2_SPARK_LOCAL_DIR` | real-disk scratch | Spark spill and scratch root |
| `OVID2_LEGACY_SCRATCH_DIR` | `$XDG_CACHE_HOME/ovid2/legacy-scratch` | real-disk root for `legacy_ovid` stage files (mapper / fixer outputs, round-trip dirs) |
| `OVID2_HOST_MEMORY_GB` / `OVID2_HOST_CPUS` | detected | override host sizing |
| `OVID2_DEDUP_SALT_BUCKETS` | `32` | salted-dedup partitions |
| `OVID2_DEDUP_NULL_KEY_PASSTHROUGH` | `true` | keep null-key rows out of dedup |
| `OVID2_DISC_WRITE_PARTITIONS` | engine default | output partitions for disc writes |
| `OVID2_SNOWFLAKE_WRITE_PARTITIONS` | unset (keep the DataFrame's own partitioning) | Spark partitions fed to the Snowflake connector on write; non-positive or non-integer values are ignored |
| `OVID2_VOCAB_PARQUET_CACHE` | on | `0` disables the one-time Parquet cache of large vocabulary files (OMOP `CONCEPT`, `CONCEPT_RELATIONSHIP`) shared by Spark and DuckDB |
| `OVID2_COPY_ORDER` | largest first | `as_given` keeps the listed order |
| `OVID2_TIMEZONE` | `America/New_York` | timezone for executor timestamps (e.g. `UPDATED` columns) |
| `OVID2_NETWORK_ID` | the template's `NETWORKID`, else `NET` | default network id (`HARVEST.NETWORKID`, prefix of `DATAMARTID`) offered when a new pipeline is scaffolded |
| `OVID2_ENGINE_METRICS_ENABLED` | `true` | `0` turns off the live engine-metrics sampler (no `engine.metrics` events) |
| `OVID2_ENGINE_METRICS_SPARK_MS` | `500` | Spark sample interval while tasks are active (ms) |
| `OVID2_ENGINE_METRICS_IDLE_MS` | `5000` | sample interval once idle (ms) |
| `OVID2_ENGINE_METRICS_IDLE_AFTER` | `5` | consecutive idle samples before switching to the idle interval |
| `OVID2_ENGINE_METRICS_SNOWPARK_MS` | `5000` | Snowpark sample interval (ms) |

### DuckDB

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_DUCKDB_THREADS` | every usable CPU | DuckDB threads |
| `OVID2_DUCKDB_MEMORY_LIMIT` | 85% of the container cap | DuckDB `memory_limit` |
| `OVID2_DUCKDB_MEMORY_FRACTION` | `0.75` | share of host RAM for the driver container |
| `OVID2_DUCKDB_CPUS` | every usable CPU | the container's `--cpus` |
| `OVID2_DUCKDB_TEMP_DIR` | per-run scratch | spill directory |
| `OVID2_DUCKDB_STAGE_SOURCES` | `true` | parse each text source once into scratch Parquet |
| `OVID2_DUCKDB_MAX_LINE_SIZE` | 8 MiB | longest CSV record |
| `OVID2_DUCKDB_SCRATCH_CODEC` | `zstd` | scratch Parquet codec (`zstd` / `lz4_raw` / `snappy` / `uncompressed`) |
| `OVID2_DUCKDB_EXTENSION_DIR` | DuckDB's own | directory of pre-installed DuckDB extensions (no download at run time) |
| `OVID2_GCS_ENDPOINT` | — | point DuckDB's GCS client at an emulator (anonymous credentials) |

See [Engines → DuckDB tuning](../02-concepts/engines.md#duckdb-tuning).

### Snowpark

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_SNOWPARK_STAGING_SCHEMA` | `<db>.OVID2_SNOWPARK_STAGING` | scratch schema for temp views and staging: `<db>.<schema>`, or a bare `<schema>` in the session's database ([resolution order](../04-running-jobs/health-checks.md#snowpark-scratch-schema)) |
| `OVID2_SNOWPARK_STAGING_KEEP` | off | keep staging objects for debugging |
| `OVID2_SNOWPARK_STAGING_INGEST_PREFIX` | `ovid_ingest` | `@~` sub-directory for source → Snowflake staging |
| `OVID2_SNOWPARK_STAGING_EGEST_PREFIX` | `ovid_egest` | `@~` sub-directory for Snowflake → target staging |
| `OVID2_SNOWPARK_STAGING_{S3,AZURE_BLOB,GCS}_INTEGRATION` | — | storage integration names for external stages (required for GCS) |
| `OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI` | — | `s3://<bucket>/<prefix>/` used to bounce Databricks data into Snowflake; required for Snowpark + Databricks |
| `OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION` | the S3 integration | storage integration for the bounce stage |
| `OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_{ACCESS_KEY_ID,SECRET_ACCESS_KEY,SESSION_TOKEN}` | boto3 default chain | inline AWS credentials for the bounce bucket |
| `OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION` | boto3 default | AWS region of the bounce bucket |
| `OVID2_SNOWPARK_SESSION_PARAMETERS` | — | JSON object of Snowflake session parameters merged over the defaults (`abort_detached_query`, `client_session_keep_alive`) |
| `OVID2_DEFAULT_SF_ACCOUNT` | deployment default | Snowflake account for address configs that omit one; empty disables the fallback |

## Web, worker and MCP

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_WEB_BIND` | `127.0.0.1:8090` | panel bind address |
| `OVID2_WEB_URL` | `http://$OVID2_WEB_BIND` | URL executors and MCP use to reach the API |
| `OVID2_WEB_MAX_PARALLEL_JOBS` | `1` | worker concurrency |
| `OVID2_WEB_WORKER_POLL_SECONDS` | `2.0` | worker poll interval |
| `OVID2_WEB_DASHBOARD_POLL_MS` | `3000` | dashboard refresh interval |
| `OVID2_WEB_RUN_EVENTS_POLL_MS` | `2000` | run-page event poll interval |
| `OVID2_WEB_RUN_EVENTS_PAGE_SIZE` | `500` | events returned per `/api/v1/runs/{id}/events` call |
| `OVID2_WEB_LIVE_SSE_KEEPALIVE_S` | `15` | keep-alive interval on the live-metrics stream |
| `OVID2_WEB_LIVE_SSE_MAX_SECONDS` | `3600` | maximum lifetime of one live-metrics stream |
| `OVID2_WEB_LIVE_SSE_MAX_RECONNECT` | `5` | reconnect attempts before the browser falls back to polling |
| `OVID2_MCP_BIND` | `127.0.0.1:8091` | MCP HTTP bind |
| `OVID2_MCP_API_TOKEN` | — | MCP service-principal bearer token |
| `OVID2_MCP_HTTP_TIMEOUT` | `30.0` | timeout (s) for each MCP → API request |
| `OVID2_MCP_ACTOR` | `mcp` | `submitted_by` the MCP server records on jobs it submits |
| `OVID2_LLM_TOOLS_API_URL` | the server's own socket | where assistant tool calls reach the API |
| `OVID2_MANUAL_RELOAD` | off | `1` re-reads manual sources on every request (authoring) |

## Data Space and exports

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_EXPORT_MAX_ROWS` | `100000` | immediate CSV / Excel download cap; larger exports run in a container |
| `OVID2_EXPORT_DOC_MAX_ROWS` | `5000` | PDF / Word cap |
| `OVID2_BROWSE_DEADLINE_SECONDS` | built-in | storage browse timeout |
| `OVID2_COUNT_DEADLINE_SECONDS` | `900` | timeout for an operator-triggered exact row count |
| `OVID2_EXPORT_DEADLINE_SECONDS` | `900` | timeout for an immediate table export |
| `OVID2_UCANACCESS_JARS` | `$SPARK_HOME/jars` | UCanAccess jars for host-side Access browsing |

## Secrets and security

| Variable | Default | Meaning |
| --- | --- | --- |
| `OVID2_DEPLOYMENT` | `production` | `dev` (set by `ovid2 dev`) turns off the secure defaults: sign-in off unless configured, secret scopes `warn`. Unset or any other value means production. |
| `OVID2_SECRETS_SCOPE_MODE` | `enforce` (`warn` in dev instances) | `enforce` makes executor secret scopes exclusive; `warn` only logs out-of-scope reads |
| `OVID2_BREAKGLASS_PASSWORD_FILE` | — | file whose first line `ovid2 init` uses as the break-glass admin password (unattended installs) |
| `OVID2_SECRETS_DIR` | set by Ovid2 | the scoped secret directory of an executor. Don't set it yourself (see [below](#set-by-ovid2-do-not-set)). |
| `OVID2_SECRETS_FILE` | — | single-file override for CI. Never forwarded; ignored under `enforce`. |
| `OVID2_WORKER_ENV_ALLOWLIST` | built-in list | extra variables forwarded to executor subprocesses |
| `OVID2_SEED_FILE` | searched in `secrets.d/` | explicit path to the seed (key) file for identifier encryption (`encrypt_id`) |
| `OVID2_SEED_FILE_NAME` | `xor_seed.txt` | seed file name searched for in `secrets.d/` |

## Set by Ovid2 (do not set)

Ovid2 sets these for its own child processes and containers. They are
listed so you recognise them in a process environment.

| Variable | Set by | Meaning |
| --- | --- | --- |
| `OVID2_INSTANCE_ID` | the worker | pre-assigned run id (`YYYYMMDD-HHMMSS-<6hex>`) so the run page can follow the event log from the start |
| `OVID2_SECRETS_DIR` | the worker / web tier | the executor's scoped secret directory |
| `OVID2_DUCKDB_SCRATCH_ROOT` | `ovid2 etl` | the real-disk scratch mount inside the run container |
| `OVID2_LLM_API_URL` / `OVID2_LLM_API_TOKEN` | the assistant | API root and per-turn read-only token for assistant tool calls |
| `OVID2_DEV_INSTANCE` / `OVID2_DEV_ROOT` | `ovid2 dev env` | the dev instance's name and root directory |

## Dev instances

`OVID2_DEV_HOST`, `OVID2_DEV_PORT_RANGE`, `OVID2_DEV_HOME`,
`OVID2_DEV_REGISTRY`, `OVID2_DEV_BASE_IMAGE`. See
[Dev instances](../01-getting-started/dev-instances.md).
