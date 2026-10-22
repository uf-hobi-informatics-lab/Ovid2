"""Default configuration settings for Ovid2.

All hardcoded paths from the legacy Ovid ``/app`` / ``/data`` layout are
routed through **environment-first, XDG-compliant** resolvers so the
package works both inside the production Docker image (where the legacy
paths still exist as bind mounts) and on a developer laptop (where those
paths do not).

Precedence for every path constant is:

1. Explicit environment variable (``$OVID2_MATERIAL_DIR``, ``$OVID2_DATA_DIR``, …).
2. Legacy in-container path (``/app/material``, ``/data``) when running inside
   Docker (``/.dockerenv`` present).
3. XDG default relative to the developer's ``$XDG_DATA_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Runtime environment detection
# ---------------------------------------------------------------------------

INSIDE_DOCKER: bool = Path("/.dockerenv").exists()

# ---------------------------------------------------------------------------
# Path resolvers
# ---------------------------------------------------------------------------


def _xdg_data_home() -> Path:
    """Return the effective ``$XDG_DATA_HOME`` (defaults to ``~/.local/share``)."""
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def _xdg_config_home() -> Path:
    """Return the effective ``$XDG_CONFIG_HOME`` (defaults to ``~/.config``)."""
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def _xdg_runtime_dir() -> Path:
    """Return the effective ``$XDG_RUNTIME_DIR`` (defaults to ``/tmp/ovid2-<uid>``).

    Unlike other XDG dirs, ``XDG_RUNTIME_DIR`` has no home-based default; the
    spec says applications must handle its absence. We fall back to a
    per-uid ``/tmp`` directory.
    """
    override = os.environ.get("XDG_RUNTIME_DIR")
    if override:
        return Path(override)
    return Path(f"/tmp/ovid2-{os.getuid()}")  # noqa: S108 — deliberate uid-scoped fallback


def _resolve_path(env_var: str, legacy_docker: str, xdg_relative: str) -> Path:
    """Resolve a path via env-var, then legacy docker mount, then XDG default.

    Args:
        env_var: Environment variable that overrides everything else.
        legacy_docker: Absolute path used inside the production container.
        xdg_relative: Relative path appended to ``$XDG_DATA_HOME``.

    Returns:
        The resolved :class:`~pathlib.Path` (not required to exist).
    """
    override = os.environ.get(env_var)
    if override:
        return Path(override)
    if INSIDE_DOCKER and Path(legacy_docker).exists():
        return Path(legacy_docker)
    return _xdg_data_home() / "ovid2" / xdg_relative


# ---------------------------------------------------------------------------
# Application paths
# ---------------------------------------------------------------------------

MATERIAL_DIR: Path = _resolve_path("OVID2_MATERIAL_DIR", "/app/material", "material")  # noqa: OVID2002
DATA_DIR: Path = _resolve_path("OVID2_DATA_DIR", "/data", "data")  # noqa: OVID2002
LEGACY_CREDENTIALS_DIR: Path = _resolve_path(
    "OVID2_CREDENTIALS_DIR",
    "/ovid_credentials",  # noqa: OVID2002
    "credentials",
)

OMOP_CDM_DIR: Path = MATERIAL_DIR / "omop_cdm" / "v5.3"
COMMON_LISTS_DIR: Path = MATERIAL_DIR / "lists"
GEOCODING_TABLE_PATH: Path = COMMON_LISTS_DIR / "FULL_GEOCODING_202603_16.csv"
RUCA_TRACT_XLSX_PATH: Path = COMMON_LISTS_DIR / "RUCA-codes-2020-tract.xlsx"
RUCA_ZIPCODE_XLSX_PATH: Path = COMMON_LISTS_DIR / "RUCA-codes-2020-zipcode.xlsx"
PCORNET_CDM_DIR: Path = MATERIAL_DIR / "cdm" / "pcornet_7_0"
PCORNET_CDM_CSV_PATH: Path = PCORNET_CDM_DIR / "pcornet_cdm_v70.csv"

# Log4j config for Spark — packaged with ovid2 (resources/log4j-quiet.properties).
LOG4J_CONFIG_URI: str = "file:" + str(
    Path(__file__).resolve().parent.parent / "resources" / "log4j-quiet.properties"
)

# Work directory for docker-compose files, spark tmp, event logs.
WORK_DIR: Path = Path(os.environ.get("OVID2_WORK_DIR") or (_xdg_runtime_dir() / "ovid2"))

# ---------------------------------------------------------------------------
# Harmonized field sizes (VARCHAR widths for hashed ID columns).
#
# DEPRECATED (P10): this mapping now lives on each CDM's identifier
# columns — read via ``cdm.id_columns[role].width``. The dict below is
# retained as a backwards-compatible re-export until P11 removes it.
# ---------------------------------------------------------------------------
HARMONIZED_FIELD_SIZES: dict[str, int] = {
    "PATID": 64,
    "PATID_1": 64,
    "PATID_2": 64,
    "PERSON_ID": 64,
    "ENCOUNTERID": 128,
    "PROVIDERID": 64,
    "VX_PROVIDERID": 64,
    "MEDADMIN_PROVIDERID": 64,
    "OBSGEN_PROVIDERID": 64,
    "OBSCLIN_PROVIDERID": 64,
    "RX_PROVIDERID": 64,
    "PROCEDURESID": 128,
    "PRESCRIBINGID": 128,
    "ADDRESSID": 64,
    "GEOCODEID": 64,
}

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
DOCKER_IMAGE: str = os.environ.get("OVID2_DOCKER_IMAGE", "ovid2:latest")

# ---------------------------------------------------------------------------
# Spark
# ---------------------------------------------------------------------------
SPARK_MODE: str = os.environ.get("OVID2_SPARK_MODE", "cluster")
SPARK_CORES: str = os.environ.get("OVID2_SPARK_CORES", "15")
SPARK_MASTER_URL_DEFAULT: str = "spark://spark-master:7077"

SPARK_CONFIG: dict[str, str] = {
    # Driver
    "spark.driver.memory": "32g",
    "spark.driver.memoryOverhead": "4g",
    "spark.driver.maxResultSize": "8g",
    "spark.driver.extraJavaOptions": f"-Dlog4j.configuration={LOG4J_CONFIG_URI}",
    # Executors — 8 cores / 48g each
    "spark.executor.memory": "48g",
    "spark.executor.memoryOverhead": "8g",
    "spark.executor.cores": "8",
    # AQE
    "spark.sql.adaptive.enabled": "true",
    "spark.sql.adaptive.coalescePartitions.enabled": "true",
    "spark.sql.adaptive.skewJoin.enabled": "true",
    "spark.sql.adaptive.localShuffleReader.enabled": "true",
    # Auto-broadcast reference tables (see notes in refactor commit)
    "spark.sql.autoBroadcastJoinThreshold": "200m",
    # Datetime parser leniency for legacy source data
    "spark.sql.legacy.timeParserPolicy": "LEGACY",
    # Parallelism
    "spark.sql.shuffle.partitions": "200",
    "spark.default.parallelism": "200",
    # Memory
    "spark.memory.fraction": "0.8",
    "spark.memory.storageFraction": "0.3",
    # Shuffle
    "spark.shuffle.spill.compress": "true",
    "spark.shuffle.compress": "true",
    "spark.shuffle.file.buffer": "1m",
    "spark.reducer.maxSizeInFlight": "96m",
    # File / partition sizing
    "spark.sql.files.maxPartitionBytes": "268435456",  # 256 MB
    "spark.sql.files.openCostInBytes": "8388608",  # 8 MB
    # Serialization
    "spark.serializer": "org.apache.spark.serializer.KryoSerializer",
    "spark.kryoserializer.buffer.max": "1g",
    # Network
    "spark.network.timeout": "600s",
    "spark.rpc.askTimeout": "600s",
}

# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
CSV_DELIMITER: str = "\t"

# ---------------------------------------------------------------------------
# CSV single-file finalize (disc / s3 / azure_blob)
#
# Historically each of the three CSV-producing drivers hard-coded
# ``coalesce(15)`` before ``df.write.csv(...)``. On a 25-worker cluster
# that pins the last write stage to 15 executors and leaves the rest
# idle — the exact bottleneck ``coalesce(1)`` produces, just less
# extreme. The single-file contract is now decoupled from the write
# parallelism: Spark writes with its natural partitioning, and the
# driver stitches the parts into one artefact via a *server-side*
# primitive (``cat`` on disc, ``UploadPartCopy`` on S3, ``PutBlockFromURL``
# on Azure). Zero bytes traverse the driver.
#
# The knob below is disc-only. It exists as an escape hatch for
# operators whose local filesystem thrashes under high concurrent PUT
# — set ``OVID2_DISC_WRITE_PARTITIONS`` to a positive int and the disc
# driver will ``coalesce()`` to that number before writing. ``None``
# (the default) lets Spark's natural partitioning drive the write,
# maximising cluster utilisation.
#
# Object stores (S3, Azure Blob) never coalesce — object-store PUT
# throughput scales with parallel connections, and the server-side
# finalize means "many small parts" is no longer a downstream concern.
# ---------------------------------------------------------------------------
def _resolve_disc_write_partitions() -> int | None:
    """Read ``$OVID2_DISC_WRITE_PARTITIONS``; ``None`` when unset/invalid."""
    raw = os.environ.get("OVID2_DISC_WRITE_PARTITIONS")
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError:
        return None
    return n if n > 0 else None


DISC_WRITE_PARTITIONS: int | None = _resolve_disc_write_partitions()

# ---------------------------------------------------------------------------
# Snowpark staging (Phase C + D + E — ADR:
# the adr-engine-storage-orthogonality design note)
#
# When ``--engine snowpark`` is paired with a non-Snowflake peripheral,
# the :class:`ovid2.core.engines.stager.SnowparkEngineStager` needs to
# stage the source data into Snowflake first (via ``PUT`` for disc,
# external stages for object stores). These settings control the
# Snowflake user-stage (``@~``) paths the staging strategies use:
#
# * ``OVID2_SNOWPARK_STAGING_INGEST_PREFIX`` — subdirectory under
#   ``@~`` for source→Snowflake staging. Default ``ovid_ingest``.
# * ``OVID2_SNOWPARK_STAGING_EGEST_PREFIX`` — subdirectory under
#   ``@~`` for Snowflake→target staging. Default ``ovid_egest``.
# * ``OVID2_SNOWPARK_STAGING_KEEP`` — when truthy (``1`` / ``true`` /
#   ``yes``) skip the atexit ``REMOVE @~/<prefix>/<instance_id>/``
#   cleanup. For debugging only; leaves stage files behind and grows
#   the operator's ``@~`` over time.
#
# Phase D adds external-stage settings for object-store peripherals:
#
# * ``OVID2_SNOWPARK_STAGING_S3_INTEGRATION`` — name of a Snowflake
#   ``STORAGE INTEGRATION`` object with access to the S3 bucket. When
#   set, :class:`SnowparkS3Strategy` creates its per-invocation
#   ``TEMPORARY STAGE`` with ``STORAGE_INTEGRATION = <name>``; when
#   unset, the strategy falls back to inline credentials sourced from
#   the address's ``aws_access_key_id`` / ``aws_secret_access_key``
#   / ``aws_session_token``.
# * ``OVID2_SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION`` — same idea for
#   Azure Blob. Fallback prefers ``sas_token`` (single-line, no
#   secret-material exposure in the plan cache), then ``account_key``.
# * ``OVID2_SNOWPARK_STAGING_GCS_INTEGRATION`` — same idea for GCS.
#   **Mandatory** — unlike S3 and Azure Blob, Snowflake external
#   stages do not accept inline credentials for GCS. When unset,
#   :class:`SnowparkGcsStrategy` raises ``ValueError`` at plan time.
#
# Storage integration is the recommended production path — Snowflake's
# side of the trust chain lives in the integration object, and no
# credential material is passed through Snowpark plans. Inline
# credentials work well for one-off `ovid2 tools copy` invocations
# and testing.
#
# Every stage path is instance-scoped (``@~/<prefix>/<instance_id>/…``
# for the user-stage cases; ``<bucket|container>/<pipeline>/<batch>/…``
# for the external-stage cases) so parallel runs never collide.
# Cleanup is idempotent and best-effort (warnings on failure, never
# raises).
# ---------------------------------------------------------------------------
SNOWPARK_STAGING_INGEST_PREFIX: str = os.environ.get(
    "OVID2_SNOWPARK_STAGING_INGEST_PREFIX", "ovid_ingest"
).strip() or "ovid_ingest"

SNOWPARK_STAGING_EGEST_PREFIX: str = os.environ.get(
    "OVID2_SNOWPARK_STAGING_EGEST_PREFIX", "ovid_egest"
).strip() or "ovid_egest"


def _resolve_snowpark_staging_keep() -> bool:
    """Read ``$OVID2_SNOWPARK_STAGING_KEEP``; ``False`` when unset/other."""
    raw = os.environ.get("OVID2_SNOWPARK_STAGING_KEEP", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


SNOWPARK_STAGING_KEEP: bool = _resolve_snowpark_staging_keep()


def _resolve_optional_str(env_var: str) -> str | None:
    """Return a stripped env value or ``None`` when unset/blank."""
    raw = os.environ.get(env_var, "").strip()
    return raw or None


# ---------------------------------------------------------------------------
# Snowpark session parameters
#
# The Snowpark session builder receives a set of Snowflake *session
# parameters* that harden long-running statements against the Python
# connector's default retry-on-timeout behaviour. Without these, a
# client-side socket/network timeout during a multi-hour ``CREATE OR
# REPLACE TABLE ... AS SELECT`` (e.g. the ``pcornet_to_omop`` measurement
# CTAS, which scans multiple terabytes) causes the connector to *resubmit*
# the statement while the original keeps running server-side — spawning
# two, three, or more identical CTAS executions that compete for the same
# warehouse and slow every copy down.
#
# The two levers that stop this:
#
# * ``ABORT_DETACHED_QUERY = TRUE`` — Snowflake aborts a query whose
#   client connection has detached, so a resubmit cannot leave an
#   orphaned duplicate running. This is the decisive fix.
# * ``CLIENT_SESSION_KEEP_ALIVE = TRUE`` + a large ``network_timeout`` —
#   keep the long CTAS's client connection alive so the timeout that
#   triggers the resubmit never fires in the first place.
#
# Operators can override or extend the defaults with
# ``OVID2_SNOWPARK_SESSION_PARAMETERS`` (a JSON object of parameter →
# value), or per-engine via the engine JSON's ``session_parameters``
# arg. Operator-supplied values win over these defaults.
# ---------------------------------------------------------------------------

#: Snowflake session parameters applied to every Snowpark session by
#: default. Keys are passed straight through to the Snowpark
#: ``Session.builder.configs`` mapping (the connector forwards unknown
#: keys as session parameters). ``ABORT_DETACHED_QUERY`` is the reason
#: this exists — see the module comment above.
SNOWPARK_SESSION_PARAMETERS_DEFAULT: dict[str, object] = {
    "abort_detached_query": True,
    "client_session_keep_alive": True,
}


def _resolve_snowpark_session_parameters() -> dict[str, object]:
    """Return the default session parameters merged with any env override.

    Reads ``$OVID2_SNOWPARK_SESSION_PARAMETERS`` as a JSON object and
    merges it over :data:`SNOWPARK_SESSION_PARAMETERS_DEFAULT` (operator
    values win). Malformed JSON is ignored (defaults used) so a typo
    never blocks a run.
    """
    import json

    params = dict(SNOWPARK_SESSION_PARAMETERS_DEFAULT)
    raw = os.environ.get("OVID2_SNOWPARK_SESSION_PARAMETERS", "").strip()
    if raw:
        try:
            override = json.loads(raw)
        except ValueError:
            override = None
        if isinstance(override, dict):
            params.update(override)
    return params


SNOWPARK_SESSION_PARAMETERS: dict[str, object] = _resolve_snowpark_session_parameters()


#: Name of the Snowflake ``STORAGE INTEGRATION`` used by
#: :class:`SnowparkS3Strategy`. ``None`` triggers the inline-credentials
#: fallback path.
SNOWPARK_STAGING_S3_INTEGRATION: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_S3_INTEGRATION"
)

#: Name of the Snowflake ``STORAGE INTEGRATION`` used by
#: :class:`SnowparkAzureBlobStrategy`. ``None`` triggers the SAS /
#: account-key fallback path.
SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION"
)

#: Name of the Snowflake ``STORAGE INTEGRATION`` used by
#: :class:`SnowparkGcsStrategy`. ``None`` triggers ``ValueError`` at
#: plan time — GCS external stages do not accept inline credentials
#: (unlike S3 and Azure Blob), so a pre-provisioned storage
#: integration is mandatory. Provision one on the Snowflake side with
#: ``CREATE STORAGE INTEGRATION <name> TYPE = EXTERNAL_STAGE
#: STORAGE_PROVIDER = 'GCS' ...`` and bind the resulting service
#: account to the target bucket's IAM.
SNOWPARK_STAGING_GCS_INTEGRATION: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_GCS_INTEGRATION"
)

# ---------------------------------------------------------------------------
# Snowpark staging — Phase E (SnowparkDatabricksStrategy)
#
# Databricks endpoints route through an S3 bounce because Snowflake
# cannot create a stage against a Databricks warehouse directly.
# The bounce URI must be reachable by *both* sides:
#
# * Snowflake reads/writes via a ``TEMPORARY STAGE`` — authenticated
#   through :data:`SNOWPARK_STAGING_S3_INTEGRATION` /
#   :data:`SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION` (or the
#   inline AWS credentials below).
# * Databricks reads/writes via ``INSERT OVERWRITE DIRECTORY`` /
#   ``COPY INTO`` — authenticated through Unity Catalog external
#   locations (or storage credentials on the SQL warehouse). This
#   side is provisioned entirely on the Databricks workspace; the
#   Ovid2 process never sees these credentials.
#
# Operators who want to isolate the bounce's AWS credentials from
# any address config (typical when the S3 bucket used for bouncing
# is different from any pipeline-facing S3 address) can set the four
# ``AWS_...`` env vars below.
# ---------------------------------------------------------------------------

#: ``s3://<bucket>/<optional-prefix>/`` bounce URI for
#: :class:`SnowparkDatabricksStrategy`. Required. When ``None``, any
#: ``--engine snowpark`` + databricks pairing raises ``ValueError``
#: at plan time.
SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI"
)

#: Optional per-strategy override of :data:`SNOWPARK_STAGING_S3_INTEGRATION`.
#: When set, ``SnowparkDatabricksStrategy`` uses this integration name
#: for the bounce stage instead of the general S3 integration. Falls
#: back to :data:`SNOWPARK_STAGING_S3_INTEGRATION` when unset.
SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION"
)

#: Inline AWS access key for the bounce. ``None`` triggers ``boto3``'s
#: default provider chain (EC2 metadata / ``AWS_ACCESS_KEY_ID`` env /
#: shared credentials file / …).
SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID"
)

#: Inline AWS secret key for the bounce.
SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY"
)

#: Inline AWS STS session token for the bounce (optional even when
#: the access key + secret are set).
SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SESSION_TOKEN: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SESSION_TOKEN"
)

#: Optional AWS region for the bounce bucket. When ``None``, ``boto3``
#: uses its default resolution (region from the env / shared config /
#: instance metadata).
SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION: str | None = _resolve_optional_str(
    "OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION"
)

# ---------------------------------------------------------------------------
# Live engine metrics
#
# The pipeline emits ``engine.metrics`` events on a fixed cadence while a
# run is in flight. Every N milliseconds the sampler calls
# ``engine.get_metrics()`` (cheap in-JVM StatusTracker fields for Spark;
# a bare "alive" sample for Snowpark in phase 1) and appends the sample
# to the run's JSONL event log. The web tier tails those samples over
# an SSE stream so the run detail page can show live pulse + tiles.
#
# Rate discipline:
#
# * Spark: 500 ms while any task is active, 5 s while idle (adaptive
#   backoff triggered after 5 consecutive zero-active samples).
# * Snowpark: 5 s uniformly — even a "cheap" query is billable, so
#   phase 1 returns family-only samples and never round-trips.
#
# Setting ``OVID2_ENGINE_METRICS_ENABLED=0`` disables the sampler
# entirely (no thread started, no events emitted).
# ---------------------------------------------------------------------------
def _resolve_bool_env(name: str, default: bool) -> bool:
    """Read ``$<name>`` as a boolean with ``default`` when unset."""
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "on")


def _resolve_int_env(name: str, default: int) -> int:
    """Read ``$<name>`` as an int, falling back to ``default`` on error."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


ENGINE_METRICS_ENABLED: bool = _resolve_bool_env("OVID2_ENGINE_METRICS_ENABLED", True)
ENGINE_METRICS_SPARK_MS: int = _resolve_int_env("OVID2_ENGINE_METRICS_SPARK_MS", 500)
ENGINE_METRICS_SNOWPARK_MS: int = _resolve_int_env(
    "OVID2_ENGINE_METRICS_SNOWPARK_MS", 5000
)
ENGINE_METRICS_IDLE_MS: int = _resolve_int_env("OVID2_ENGINE_METRICS_IDLE_MS", 5000)
#: How many consecutive zero-active-task samples trigger the switch to
#: the idle rate. Keeps the log from ballooning while the cluster spins
#: up or between tables.
ENGINE_METRICS_IDLE_AFTER: int = _resolve_int_env(
    "OVID2_ENGINE_METRICS_IDLE_AFTER", 5
)


# ---------------------------------------------------------------------------
# Timezone
# ---------------------------------------------------------------------------
TIMEZONE: str = os.environ.get("OVID2_TIMEZONE", "America/New_York")

# ---------------------------------------------------------------------------
# Snowflake defaults
# ---------------------------------------------------------------------------
#: Fallback Snowflake account for legacy address configs that omit one.
#: Override per deployment with ``OVID2_DEFAULT_SF_ACCOUNT``; an empty
#: value disables the fallback.
DEFAULT_SF_ACCOUNT: str = os.environ.get("OVID2_DEFAULT_SF_ACCOUNT", "")

# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------
SEED_FILE_NAME: str = os.environ.get("OVID2_SEED_FILE_NAME", "xor_seed.txt")

# ---------------------------------------------------------------------------
# Reference tables — cached after their per-table pipeline completes so
# downstream fixers hit the cache instead of re-executing the plan.
#
# DEPRECATED (P10): this set now lives on the CDM handle — read via
# ``cdm.reference_tables``. The constant below is retained as a
# backwards-compatible re-export until P11 removes it. New code MUST use
# the handle so non-PCORnet CDMs work correctly.
# ---------------------------------------------------------------------------
REFERENCE_TABLES: frozenset[str] = frozenset(
    {"demographic", "encounter", "provider", "harvest", "death"}
)

# ---------------------------------------------------------------------------
# Deduplication skew relief
# ---------------------------------------------------------------------------
DEDUP_SALT_BUCKETS: int = int(os.environ.get("OVID2_DEDUP_SALT_BUCKETS", "32"))
DEDUP_NULL_KEY_PASSTHROUGH: bool = (
    os.environ.get("OVID2_DEDUP_NULL_KEY_PASSTHROUGH", "true").lower() != "false"
)
