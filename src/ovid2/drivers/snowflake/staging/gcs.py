"""``SnowparkGcsStrategy`` — stage GCS objects through a Snowflake external stage.

ovid2: allow-sql-fstring — every SQL statement in this module is
constructed from trusted internal values. Instance IDs come from
:mod:`ovid2.core.isolation` (regex-sanitised
``YYYYMMDD-HHMMSS-<6hex>`` shape), stage/table names are built with
:func:`re.sub` and uppercased so no operator- or pipeline-supplied
identifiers reach the interpolation directly, and inline URL
values pass through :func:`_escape_sql`. The strategy is only
reachable via :class:`SnowparkEngineStager` after the compat gate
has already vetted the pairing.

Registered at module import time via
:func:`ovid2.core.engines.stager.register_snowpark_strategy`.
:mod:`ovid2.drivers.snowflake.staging.__init__` triggers the import
so the strategy self-registers whenever the snowflake driver is
loaded (which happens unconditionally during CLI / web startup).

The strategy participates in the Snowpark data plane. It intentionally
avoids importing :mod:`pyspark` and :mod:`google.cloud.storage` at
module scope. Snowpark itself is also imported lazily (inside method
bodies) so the module can be enumerated by the web tier without
dragging in the Snowflake driver.

Data flow — GCS → Snowpark
--------------------------

1. Resolve the GCS URI (``gcs://<bucket>/<prefix>/<pipeline>/<batch>/<file>``)
   from the driver's location. Snowflake accepts ``gcs://`` as the
   scheme in ``CREATE STAGE URL='…'`` even though the canonical
   Google scheme is ``gs://``.
2. ``CREATE OR REPLACE TEMPORARY STAGE <name> URL='gcs://…/'``
   pointing at the *directory* containing the file, with
   ``STORAGE_INTEGRATION = <name>`` — GCS does **not** accept
   inline credentials on the ``CREATE STAGE`` DDL (unlike S3 and
   Azure Blob). The operator must pre-provision a Snowflake storage
   integration bound to the GCS service account.
3. Read the header line locally via ``google-cloud-storage`` to
   derive an all-string column DDL (matches the disc strategy's
   ``inferSchema=false`` behaviour on read). The service-account
   JSON on the driver's connection secrets authenticates this call.
4. ``CREATE OR REPLACE TRANSIENT TABLE <staging_schema>.OVID2_STG_<...>
   (<col1> VARCHAR, …)``.
5. ``COPY INTO <fqtn> FROM @<stage>/<file>
   FILE_FORMAT = (TYPE = 'CSV' FIELD_DELIMITER = '<delim>'
   SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"' …)``.
6. Return ``session.table(fqtn)``.

Data flow — Snowpark → GCS
--------------------------

1. Same stage creation as ingest, targeted at the write directory
   (``gcs://<bucket>/<prefix>/<pipeline>/<batch>/``).
2. ``df.write.copy_into_location('@<stage>/<table>/',
   file_format_type='csv', format_type_options={...}, header=True,
   overwrite=True, single=<from location.single_file>)`` writes
   the CSV directly into the GCS bucket via the stage.
3. No download step. The bytes land in GCS as
   ``<table>/data_0_0_0.csv[.gz]`` or
   ``<table>/data_0_0_<N>.csv[.gz]``.

Auth path resolution
--------------------

GCS's Snowflake auth path is **integration-only**:

1. ``STORAGE_INTEGRATION = <name>`` when
   :data:`ovid2.config.settings.SNOWPARK_STAGING_GCS_INTEGRATION`
   is set.
2. Otherwise :class:`ValueError` at plan time — GCS external stages
   do not accept inline credentials (a design decision on
   Snowflake's side; the equivalent of AWS's static-credential
   fallback simply doesn't exist). The message names the required
   env var so the operator can provision one.

The local header-reader uses the driver's connection service-account
JSON (``service_account_json`` secret) or falls back to Application
Default Credentials (ADC) when the JSON is absent — matches the
existing :class:`~ovid2.connections.gcs.GcsConnection.test` probe.

Instance-scoped cleanup
-----------------------

All ``TEMPORARY STAGE`` and ``OVID2_STG_%`` transient-table objects
created during the run are dropped on process exit via an
:mod:`atexit` hook registered lazily on the first data-plane call.
When :data:`ovid2.config.settings.SNOWPARK_STAGING_KEEP` is truthy,
the hook is skipped (useful for post-mortem inspection). Temporary
stages are auto-cleaned by Snowflake at session end regardless — the
atexit hook is a belt-and-suspenders that also cleans up before the
session naturally ends.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.stager import (
    SnowparkStagingStrategy,
    register_snowpark_strategy,
)

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)


class SnowparkGcsStrategy(SnowparkStagingStrategy):
    """Bridge the ``gcs`` driver into a Snowpark session via an external stage.

    Handles both directions:

    * :meth:`materialise` — ``CREATE STAGE`` pointing at the source
      URI, ``COPY INTO`` a transient table, return
      ``session.table(fqtn)``.
    * :meth:`persist` — ``CREATE STAGE`` pointing at the destination
      URI, ``df.write.copy_into_location`` directly into GCS (no
      download step).

    Both directions register a session-scoped atexit cleanup on the
    first call so a crashed run leaves at most one instance's worth
    of stages + transient tables behind. Cleanup is skipped when
    :data:`~ovid2.config.settings.SNOWPARK_STAGING_KEEP` is truthy.
    """

    driver_type = "gcs"

    #: Class-level record of the atexit-registered stage names so
    #: :meth:`_maybe_schedule_cleanup` stays idempotent across many
    #: ``materialise`` / ``persist`` calls in one run. Keyed by
    #: ``id(session)`` because a Snowpark session isn't hashable.
    _cleanups_scheduled: dict[int, set[str]] = {}

    #: Per-session list of stage names the strategy created.
    _stages: dict[int, list[str]] = {}

    #: Per-session list of transient tables the strategy created.
    _temp_tables: dict[int, list[str]] = {}

    # ------------------------------------------------------------------
    # Public data plane (invoked by SnowparkEngineStager)
    # ------------------------------------------------------------------

    def materialise(
        self,
        session: EngineSession,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None,
        table_name: str | None,
    ) -> Any:
        """Stage ``file_name`` from GCS into a temporary stage and return a Snowpark DF.

        Args:
            session: The Snowpark session wrapper (real
                :class:`snowflake.snowpark.Session` is on ``.native``).
            driver: A :class:`~ovid2.drivers.legacy_adapter.LegacyDriverAdapter_gcs`
                instance (until a native ``GcsDriver`` lands, the D2
                adapter is the pipeline surface).
            pipeline: Pipeline name (source-side lookup key prefix).
            batch_id: Batch identifier.
            file_name: Source-side GCS object name (basename) or
                Hadoop-style glob. Wildcards are resolved by
                Snowflake's ``COPY INTO`` pattern matching.
            delimiter: CSV field separator. When ``None``, defaults
                to :data:`ovid2.config.settings.CSV_DELIMITER`.
            table_name: Ignored on read; GCS reads always use
                ``file_name`` (matches the disc/s3 conventions).

        Returns:
            A :class:`snowflake.snowpark.DataFrame` reading from the
            transient staging table.

        Raises:
            ValueError: When the driver has no bucket resolved, or
                its ``format`` is not ``csv``, or Snowflake has no
                storage integration configured.
        """
        del table_name  # unused; GCS reads use file_name
        from ovid2.config.settings import CSV_DELIMITER

        merged = _merged_config(driver)
        _require_csv(merged, "materialise")
        bucket = _require_bucket(merged)
        prefix = _nonempty(merged.get("prefix")) or ""
        delim = delimiter or CSV_DELIMITER

        # Instance-scoped stage name. Snowflake identifiers are
        # <= 255 chars and uppercase-normalised; we roll our own
        # so parallel runs stay isolated.
        instance_id = _current_instance_id()
        table_stem = os.path.splitext(os.path.basename(file_name))[0]
        stage_name = _stage_name(instance_id, pipeline, batch_id, table_stem, kind="in")

        # URL points at the *directory* containing the file so
        # multiple files under the same pipeline/batch can share a
        # stage. COPY INTO's FILES clause below narrows to the
        # specific object.
        read_dir_uri = _gcs_dir_uri(bucket, prefix)

        native = _native_session(session)
        self._maybe_schedule_cleanup(session, instance_id)

        # Bind a database + schema so identifier resolution works
        # (Snowpark's plan analyzer fails if either is missing).
        _ensure_current_database(native)
        staging_schema = _ensure_writable_staging_schema(native)

        # CREATE STAGE.
        create_stage_sql = _build_create_stage_sql(
            stage_name=stage_name,
            url=read_dir_uri,
        )
        logger.info(
            "SnowparkGcsStrategy: CREATE TEMPORARY STAGE %s URL=%s",
            stage_name,
            read_dir_uri,
        )
        native.sql(create_stage_sql).collect()
        self._register_stage(session, stage_name)

        # Derive column DDL from the header line via a local
        # google-cloud-storage ranged download (small — one line).
        columns = _read_header_columns_from_gcs(
            bucket=bucket,
            key=_gcs_key(prefix, file_name),
            delimiter=delim,
            merged_config=merged,
        )

        # Create the transient landing table.
        temp_fqtn = _temp_table_fqtn(
            staging_schema,
            instance_id,
            pipeline,
            batch_id,
            table_stem,
        )
        col_ddl = ", ".join(f'"{c}" VARCHAR' for c in columns)
        native.sql(f"CREATE OR REPLACE TRANSIENT TABLE {temp_fqtn} ({col_ddl})").collect()
        self._register_temp_table(session, temp_fqtn)

        # COPY INTO. Snowflake honours GCS auto-compression detection
        # via COMPRESSION='AUTO'. We narrow to the specific file via
        # the FILES clause so a directory-scoped stage still reads
        # only what we asked for.
        copy_sql = (
            f"COPY INTO {temp_fqtn} FROM '@{stage_name}' "
            f"FILES = ('{_escape_sql(os.path.basename(file_name))}') "
            f"FILE_FORMAT = (TYPE = 'CSV' "
            f"FIELD_DELIMITER = '{_escape_sql(delim)}' "
            f"SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
            f"TRIM_SPACE = TRUE COMPRESSION = 'AUTO') "
            f"ON_ERROR = 'ABORT_STATEMENT' FORCE = TRUE"
        )
        logger.info(
            "SnowparkGcsStrategy: COPY INTO %s FROM @%s (file=%s)",
            temp_fqtn,
            stage_name,
            file_name,
        )
        native.sql(copy_sql).collect()

        return native.table(temp_fqtn)

    def persist(
        self,
        session: EngineSession,
        df: Any,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None,
    ) -> None:
        """Write ``df`` to GCS through an external stage.

        No download step — Snowflake writes directly into the bucket
        via the stage's ``URL='gcs://…/'`` and the CSV ends up under
        ``{prefix}/{table_name}/``. When
        ``location.single_file=True``, Snowflake writes one merged
        object; when ``False`` (default), it writes the natural
        part-file prefix downstream consumers expect.

        Args:
            session: The Snowpark session wrapper.
            df: A Snowpark DataFrame.
            driver: The target GCS driver.
            pipeline: Pipeline name.
            batch_id: Batch identifier.
            table_name: File stem — Snowflake writes
                ``<table_name>/data_0_0_0.csv[.gz]``.
            delimiter: CSV field separator; defaults to
                :data:`~ovid2.config.settings.CSV_DELIMITER`.

        Raises:
            ValueError: When the target's ``format`` is not CSV, or
                bucket is unresolved, or no storage integration is
                configured.
        """
        from ovid2.config.settings import CSV_DELIMITER

        merged = _merged_config(driver)
        _require_csv(merged, "persist")
        bucket = _require_bucket(merged)
        prefix = _nonempty(merged.get("prefix")) or ""
        delim = delimiter or CSV_DELIMITER
        single_file = bool(merged.get("single_file", False))

        instance_id = _current_instance_id()
        stage_name = _stage_name(instance_id, pipeline, batch_id, table_name, kind="out")
        write_dir_uri = _gcs_dir_uri(bucket, prefix)

        native = _native_session(session)
        self._maybe_schedule_cleanup(session, instance_id)
        _ensure_current_database(native)

        create_stage_sql = _build_create_stage_sql(
            stage_name=stage_name,
            url=write_dir_uri,
        )
        logger.info(
            "SnowparkGcsStrategy: CREATE TEMPORARY STAGE %s URL=%s",
            stage_name,
            write_dir_uri,
        )
        native.sql(create_stage_sql).collect()
        self._register_stage(session, stage_name)

        # Snowpark's copy_into_location writes CSV via COPY INTO
        # LOCATION under the hood. ``single=True`` requests one
        # merged file (Snowflake enforces a size cap; large frames
        # split anyway).
        stage_target = f"@{stage_name}/{table_name}/"
        logger.info(
            "SnowparkGcsStrategy: copy_into_location → %s (single=%s)",
            stage_target,
            single_file,
        )
        _copy_into_location(
            df,
            stage_target=stage_target,
            field_delimiter=delim,
            single_file=single_file,
        )

    # ------------------------------------------------------------------
    # Internal cleanup bookkeeping
    # ------------------------------------------------------------------

    def _maybe_schedule_cleanup(
        self,
        session: EngineSession,
        instance_id: str,
    ) -> None:
        """Register an atexit hook that drops the stages + transient tables.

        Skipped when :data:`~ovid2.config.settings.SNOWPARK_STAGING_KEEP`
        is truthy. Idempotent per ``(session, instance_id)`` pair.
        """
        from ovid2.config.settings import SNOWPARK_STAGING_KEEP

        if SNOWPARK_STAGING_KEEP:
            logger.info(
                "SnowparkGcsStrategy: SNOWPARK_STAGING_KEEP is set; "
                "skipping atexit cleanup for instance %s",
                instance_id,
            )
            return

        session_key = id(session)
        registered = self._cleanups_scheduled.setdefault(session_key, set())
        if instance_id in registered:
            return
        registered.add(instance_id)

        native = _native_session(session)
        stages_ref = self._stages.setdefault(session_key, [])
        temp_tables_ref = self._temp_tables.setdefault(session_key, [])

        def _cleanup() -> None:
            for stage in stages_ref:
                try:
                    native.sql(f"DROP STAGE IF EXISTS {stage}").collect()
                except Exception as exc:
                    logger.warning(
                        "SnowparkGcsStrategy: DROP STAGE %s failed (%s); "
                        "the stage is temporary and Snowflake will "
                        "auto-clean at session end.",
                        stage,
                        exc,
                    )
            for fqtn in temp_tables_ref:
                try:
                    native.sql(f"DROP TABLE IF EXISTS {fqtn}").collect()
                except Exception as exc:
                    logger.warning(
                        "SnowparkGcsStrategy: DROP TABLE %s failed (%s); clean manually.",
                        fqtn,
                        exc,
                    )

        atexit.register(_cleanup)

    def _register_stage(self, session: EngineSession, stage_name: str) -> None:
        """Record ``stage_name`` for atexit cleanup on this session."""
        self._stages.setdefault(id(session), []).append(stage_name)

    def _register_temp_table(self, session: EngineSession, fqtn: str) -> None:
        """Record ``fqtn`` for atexit cleanup on this session."""
        self._temp_tables.setdefault(id(session), []).append(fqtn)


# ---------------------------------------------------------------------------
# Module-level helpers (pyspark- and google-cloud-storage-free at module scope)
# ---------------------------------------------------------------------------


def _nonempty(value: Any) -> str | None:
    """Return ``value`` stripped as a string, or ``None`` when blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _current_instance_id() -> str:
    """Return the process-wide instance id (same source as disc strategy)."""
    from ovid2.core.isolation import (
        _generate_instance_id,
        _instance_id_from_env,
    )

    return _instance_id_from_env() or _generate_instance_id()


def _merged_config(driver: Any) -> dict[str, Any]:
    """Return the driver's merged connection+secrets+location config.

    Works for the D2 legacy adapter (which implements
    :meth:`as_legacy_src_args`). Falls back to ``driver.location``
    when the method is absent (defensive — the gcs driver has no
    native counterpart yet, but a future ``GcsDriver`` will keep the
    same shape).
    """
    if hasattr(driver, "as_legacy_src_args"):
        with contextlib.suppress(Exception):
            _, merged = driver.as_legacy_src_args()
            return dict(merged)
    location = getattr(driver, "location", None) or {}
    return dict(location)


def _require_csv(merged: dict[str, Any], op: str) -> None:
    """Raise ``ValueError`` when the driver's format is not CSV.

    Snowflake ``COPY INTO`` supports parquet and JSON too, but the
    Ovid2 pipeline's on-disk contract is CSV; we mirror the S3
    strategy's fmt='csv' restriction for consistency until a pipeline
    ships a parquet target.
    """
    fmt = str(merged.get("format", "csv") or "csv").lower()
    if fmt != "csv":
        raise ValueError(
            f"SnowparkGcsStrategy.{op}: only supports fmt='csv' "
            f"(got {fmt!r}). Non-CSV GCS feeds must run under "
            "--engine pyspark."
        )


def _require_bucket(merged: dict[str, Any]) -> str:
    """Return the resolved bucket or raise a descriptive ``ValueError``."""
    bucket = _nonempty(merged.get("bucket"))
    if not bucket:
        raise ValueError(
            "SnowparkGcsStrategy: no bucket resolved. Set "
            "``location.bucket`` on the address or "
            "``bucket_default`` on the connection."
        )
    return bucket


def _gcs_key(prefix: str, file_name: str) -> str:
    """Assemble the bucket-relative object name ``{prefix}/{file}`` (address = exact location)."""
    parts = [p for p in (prefix, file_name) if p]
    return "/".join(parts)


def _gcs_dir_uri(bucket: str, prefix: str) -> str:
    """Assemble the ``gcs://<bucket>/<key-prefix>/`` directory URI.

    Snowflake accepts ``gcs://`` in ``CREATE STAGE URL='…'`` even
    though the canonical Google scheme is ``gs://``. We use ``gcs://``
    verbatim here so the DDL round-trips against a Snowflake
    execution plan without translation.
    """
    parts = [p for p in (prefix,) if p]
    key = "/".join(parts)
    if key:
        return f"gcs://{bucket}/{key}/"
    return f"gcs://{bucket}/"


def _stage_name(
    instance_id: str,
    pipeline: str,
    batch_id: str,
    table: str,
    *,
    kind: str,
) -> str:
    """Return a stage name safe for interpolation into DDL.

    Format: ``OVID2_STG_GCS_<KIND>_<INSTANCE>_<PIPELINE>_<BATCH>_<TABLE>``.
    Uppercased and ``[A-Z0-9_]``-only so Snowflake identifier rules
    are satisfied. Instance scoping keeps parallel runs isolated;
    the ``OVID2_STG_GCS_`` prefix lets operators reap orphans with a
    single ``SHOW STAGES LIKE 'OVID2_STG_GCS_%'`` if the atexit hook
    ever fails.
    """
    safe = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        f"{kind}_{instance_id}_{pipeline}_{batch_id}_{table}",
    )
    return f"OVID2_STG_GCS_{safe}".upper()


def _temp_table_fqtn(
    staging_schema: str,
    instance_id: str,
    pipeline: str,
    batch_id: str,
    stem: str,
) -> str:
    """Return the fully-qualified transient-table name.

    Matches the S3 strategy's convention (``OVID2_STG_<...>``) so
    an operator's cleanup query (``SHOW TABLES LIKE 'OVID2_STG_%'``)
    catches every strategy's orphans.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{instance_id}_{pipeline}_{batch_id}_{stem}")
    return f"{staging_schema}.OVID2_STG_GCS_{safe}".upper()


def _escape_sql(value: str) -> str:
    """Escape a string literal for inline SQL.

    Doubles single quotes and backslashes. Used to interpolate
    delimiters and file basenames. Not a general-purpose SQL
    injection guard — the callers pass validated internal values,
    not user input.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def _native_session(session: EngineSession) -> Any:
    """Return the real ``snowflake.snowpark.Session`` from an adapter."""
    native = getattr(session, "native", None)
    if native is None:
        return session
    return native


#: Default database used when a Snowpark session has no current
#: database set. ``SNOWFLAKE`` is Snowflake's built-in read-only
#: information-schema database — every account has it and every
#: role can ``USE`` it.
_DEFAULT_STAGE_DATABASE: str = "SNOWFLAKE"

#: Default schema under :data:`_DEFAULT_STAGE_DATABASE`.
_DEFAULT_STAGE_SCHEMA: str = "INFORMATION_SCHEMA"


def _ensure_current_database(native: Any) -> None:
    """Bind a current database + schema when either is missing.

    Snowpark's plan analyzer needs both when identifiers land in
    stage or COPY INTO statements. Idempotent — subsequent calls
    are cheap SQL round-trips.
    """
    try:
        current_db = native.get_current_database()
    except Exception:
        current_db = None
    if not current_db:
        try:
            native.sql(f"USE DATABASE {_DEFAULT_STAGE_DATABASE}").collect()
        except Exception as exc:
            logger.warning(
                "SnowparkGcsStrategy: could not USE DATABASE %s (%s)",
                _DEFAULT_STAGE_DATABASE,
                exc,
            )
            return

    try:
        current_schema = native.get_current_schema()
    except Exception:
        current_schema = None
    if current_schema:
        return
    try:
        native.sql(f"USE SCHEMA {_DEFAULT_STAGE_SCHEMA}").collect()
    except Exception as exc:
        logger.warning(
            "SnowparkGcsStrategy: could not USE SCHEMA %s (%s)",
            _DEFAULT_STAGE_SCHEMA,
            exc,
        )


def _ensure_writable_staging_schema(native: Any) -> str:
    """Return a fully-qualified ``<db>.<schema>`` we can CREATE TABLE in.

    Resolution order mirrors the S3 / disc strategies:

    1. ``$OVID2_SNOWPARK_STAGING_SCHEMA`` — operator override.
    2. ``<current_database>.OVID2_SNOWPARK_STAGING`` — created on
       demand.

    Args:
        native: The real Snowpark session.

    Returns:
        A fully-qualified ``<db>.<schema>`` string safe to use
        as the prefix of a ``CREATE TRANSIENT TABLE`` statement.

    Raises:
        RuntimeError: When no writable schema can be resolved.
    """
    override = os.environ.get("OVID2_SNOWPARK_STAGING_SCHEMA", "").strip()
    if override:
        if "." in override:
            return override
        try:
            current_db = native.get_current_database()
        except Exception:
            current_db = None
        if not current_db:
            raise RuntimeError(
                "OVID2_SNOWPARK_STAGING_SCHEMA is set to a bare schema "
                f"({override!r}) but the session has no current database. "
                "Set the env var to '<db>.<schema>' or configure the "
                "engine's default database."
            )
        return f"{str(current_db).strip(chr(34))}.{override}"

    try:
        current_db = native.get_current_database()
    except Exception:
        current_db = None
    if not current_db:
        raise RuntimeError(
            "SnowparkGcsStrategy: cannot resolve a writable staging "
            "schema — the session has no current database. Set the "
            "engine's default database in the Snowpark connection, "
            "or export OVID2_SNOWPARK_STAGING_SCHEMA=<db>.<schema>."
        )
    current_db = str(current_db).strip('"')
    schema_fqn = f"{current_db}.OVID2_SNOWPARK_STAGING"
    try:
        native.sql(f"CREATE SCHEMA IF NOT EXISTS {schema_fqn}").collect()
        return schema_fqn
    except Exception as exc:
        raise RuntimeError(
            f"SnowparkGcsStrategy: cannot create staging schema "
            f"{schema_fqn} ({exc}). Grant CREATE SCHEMA on {current_db}, "
            "or export OVID2_SNOWPARK_STAGING_SCHEMA=<existing_writable_schema>."
        ) from exc


def _build_create_stage_sql(
    *,
    stage_name: str,
    url: str,
) -> str:
    """Build the ``CREATE OR REPLACE TEMPORARY STAGE`` DDL for a GCS URL.

    Auth path resolution — integration-only:

    1. When
       :data:`ovid2.config.settings.SNOWPARK_STAGING_GCS_INTEGRATION`
       is set, emit ``STORAGE_INTEGRATION = <name>``.
    2. Otherwise raise :class:`ValueError` — Snowflake's ``CREATE
       STAGE`` DDL does not accept inline credentials for GCS (a
       design decision on Snowflake's side; the equivalent of
       AWS's ``CREDENTIALS = (AWS_KEY_ID = …)`` fallback simply
       doesn't exist for GCS). The message names the required env
       var so the operator can provision one.

    Args:
        stage_name: The stage identifier (already sanitised).
        url: Full ``gcs://bucket/prefix/`` directory URL.

    Returns:
        A single-statement DDL string.

    Raises:
        ValueError: When no storage integration is configured.
    """
    from ovid2.config.settings import SNOWPARK_STAGING_GCS_INTEGRATION

    integration = SNOWPARK_STAGING_GCS_INTEGRATION
    if not integration:
        raise ValueError(
            "SnowparkGcsStrategy: no Snowflake→GCS auth path available. "
            "Set OVID2_SNOWPARK_STAGING_GCS_INTEGRATION to a "
            "pre-provisioned STORAGE INTEGRATION name. Unlike S3 and "
            "Azure Blob, Snowflake external stages do not accept "
            "inline credentials for GCS — the storage integration is "
            "mandatory."
        )

    auth_clause = f"STORAGE_INTEGRATION = {integration}"
    return (
        f"CREATE OR REPLACE TEMPORARY STAGE {stage_name} URL = '{_escape_sql(url)}' {auth_clause}"
    )


def _read_header_columns_from_gcs(
    *,
    bucket: str,
    key: str,
    delimiter: str,
    merged_config: dict[str, Any],
) -> list[str]:
    """Read the header line of ``gs://bucket/key`` and return its columns.

    Uses ``google-cloud-storage`` with a ranged download (first
    64 KB) to avoid pulling the whole file. Wildcards in ``key``
    are rejected — the caller must resolve them upstream via a
    ``client.list_blobs`` pass.

    Auth path: the same credentials feeding the connection's own
    probe (:meth:`GcsConnection.test`):

    1. ``service_account_json`` on the connection secrets →
       :meth:`~google.oauth2.service_account.Credentials.from_service_account_info`.
    2. Otherwise Application Default Credentials — walks
       ``$GOOGLE_APPLICATION_CREDENTIALS``, gcloud CLI, GCE
       metadata service, …

    Raises:
        ValueError: When the key contains wildcards, or the object
            is empty, or the header line has no columns.
    """
    if any(ch in key for ch in "*?["):
        raise ValueError(
            f"SnowparkGcsStrategy: cannot read header from a glob "
            f"({key!r}). The caller must expand wildcards to a "
            "concrete object name before staging."
        )

    # Local imports so the module stays google-cloud-free at module
    # scope.
    from google.cloud import storage

    project_id = _nonempty(merged_config.get("project_id"))
    sa_json = merged_config.get("service_account_json")

    client_kwargs: dict[str, Any] = {}
    if project_id:
        client_kwargs["project"] = project_id
    if sa_json:
        from google.oauth2 import service_account

        info = json.loads(sa_json) if isinstance(sa_json, str) else dict(sa_json)
        client_kwargs["credentials"] = service_account.Credentials.from_service_account_info(info)

    client = storage.Client(**client_kwargs)
    blob = client.bucket(bucket).blob(key)
    body = blob.download_as_bytes(start=0, end=65535)
    if not body:
        raise ValueError(f"SnowparkGcsStrategy: gs://{bucket}/{key} is empty; nothing to stage.")
    # Decode leniently — CSV headers occasionally carry a BOM.
    text = body.decode("utf-8", errors="replace")
    # First line only.
    first = text.split("\n", 1)[0]
    columns: list[str] = []
    for raw in first.rstrip("\r").split(delimiter):
        col = raw.strip().lstrip("\ufeff").strip('"')
        columns.append(col or f"_c{len(columns)}")
    if not columns:
        raise ValueError(
            f"SnowparkGcsStrategy: could not extract columns from "
            f"gs://{bucket}/{key} (delimiter={delimiter!r})."
        )
    return columns


def _copy_into_location(
    df: Any,
    *,
    stage_target: str,
    field_delimiter: str,
    single_file: bool,
) -> None:
    """Run ``df.write.copy_into_location`` with the CSV options.

    Isolated so unit tests can stub the call. Snowpark's
    :meth:`DataFrameWriter.copy_into_location` accepts a
    ``format_type_options`` dict for CSV-specific knobs
    (``FIELD_DELIMITER``, ``COMPRESSION``, ``NULL_IF``, …).
    """
    df.write.copy_into_location(
        stage_target,
        file_format_type="csv",
        format_type_options={
            "FIELD_DELIMITER": field_delimiter,
            "COMPRESSION": "NONE",
            "FIELD_OPTIONALLY_ENCLOSED_BY": '"',
            "NULL_IF": ("",),
        },
        header=True,
        overwrite=True,
        single=single_file,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# Self-register at module import. The snowflake driver's package
# ``__init__.py`` imports the ``staging`` subpackage, whose
# ``__init__`` imports this module. Idempotent — re-registering the
# same driver_type overwrites the previous entry.
register_snowpark_strategy(SnowparkGcsStrategy)


__all__ = ["SnowparkGcsStrategy"]
