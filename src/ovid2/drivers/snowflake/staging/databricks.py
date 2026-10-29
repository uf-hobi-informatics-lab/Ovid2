"""``SnowparkDatabricksStrategy`` — bridge Databricks endpoints into Snowpark via an S3 bounce.

ovid2: allow-sql-fstring — every SQL statement in this module is
constructed from trusted internal values. Instance IDs come from
:mod:`ovid2.core.isolation` (regex-sanitised
``YYYYMMDD-HHMMSS-<6hex>`` shape), stage / directory names are built
with :func:`re.sub` so no operator- or pipeline-supplied identifier
reaches interpolation directly. The Databricks SQL emitted by
:func:`_databricks_unload_sql` / :func:`_databricks_load_sql` uses
:func:`_qualify_table` to build a backtick-quoted three-part
identifier from the driver's ``catalog`` / ``schema`` / (materialise:
``file_name`` / persist: ``table_name``) fields; Databricks
backticks are the counterpart to Snowflake's double quotes for the
same purpose. The strategy is only reachable via
:class:`SnowparkEngineStager` after the compat gate has already
vetted the pairing.

Registered at module import time via
:func:`ovid2.core.engines.stager.register_snowpark_strategy`.
:mod:`ovid2.drivers.snowflake.staging.__init__` triggers the import
so the strategy self-registers whenever the snowflake driver is
loaded (which happens unconditionally during CLI / web startup).

The strategy participates in the Snowpark data plane. It intentionally
avoids importing :mod:`pyspark`, :mod:`boto3`, and
:mod:`databricks.sql` at module scope; Snowpark itself is also
imported lazily (inside method bodies) so the module can be
enumerated by the web tier without dragging in the Snowflake driver
or the Databricks SQL connector.

Data flow — Databricks → Snowpark
---------------------------------

Snowflake cannot create a stage against a Databricks warehouse
directly — there is no ``CREATE STAGE URL='databricks://…'`` idiom.
The strategy therefore uses an S3 **bounce bucket** that both sides
can reach:

1. Compute the bounce URI:
   ``s3://<bounce-bucket>/<bounce-prefix>/<instance_id>/<pipeline>/<batch_id>/<table_stem>/``.
2. On the Databricks side (via ``databricks.sql`` connector), run:

   .. code-block:: sql

      INSERT OVERWRITE DIRECTORY '<bounce_uri>'
      USING csv OPTIONS ('header' = 'true', 'delimiter' = ',')
      SELECT * FROM `<catalog>`.`<schema>`.`<table>`

   The operator's Databricks workspace must have a Unity Catalog
   external location covering the bounce URI (or storage
   credentials on the SQL warehouse) so this write succeeds.
3. From the bounce URI, delegate to
   :func:`ovid2.drivers.snowflake.staging.s3._build_create_stage_sql`
   / :func:`_read_header_columns_from_s3` /
   ``COPY INTO <transient_table>`` — same machinery as
   :class:`SnowparkS3Strategy`.

Data flow — Snowpark → Databricks
---------------------------------

1. Same bounce URI shape as ingest, targeted at the write
   directory.
2. ``df.write.copy_into_location('@<stage>/', …)`` writes CSV to
   the bounce (server-side; no download to the container).
3. On the Databricks side, run:

   .. code-block:: sql

      COPY INTO `<catalog>`.`<schema>`.`<table>`
      FROM '<bounce_uri>'
      FILEFORMAT = CSV
      FORMAT_OPTIONS ('header' = 'true', 'delimiter' = ',')
      COPY_OPTIONS ('mergeSchema' = 'true', 'force' = 'true')

Instance-scoped cleanup
-----------------------

The Snowflake side reuses the SnowparkS3Strategy atexit cleanup for
the temporary stage and transient tables. Bounce objects on the S3
side are dropped when the last Databricks-side ``COPY INTO`` (persist)
or Snowflake-side ``COPY INTO`` (materialise) finishes.
:data:`ovid2.config.settings.SNOWPARK_STAGING_KEEP` suppresses the
bounce cleanup for post-mortem inspection.

Operator requirements
---------------------

* :data:`ovid2.config.settings.SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI` —
  ``s3://<bucket>/<prefix>/``. Required for every invocation.
* Snowflake side: a ``STORAGE INTEGRATION`` with access to the
  bounce (via
  :data:`ovid2.config.settings.SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION`,
  or fall back to
  :data:`~ovid2.config.settings.SNOWPARK_STAGING_S3_INTEGRATION`).
* Databricks side: a Unity Catalog external location (or storage
  credentials on the SQL warehouse) covering the bounce URI so
  ``INSERT OVERWRITE DIRECTORY`` and ``COPY INTO`` succeed.
* Snowpark side has no additional requirements beyond the ones
  already in place for :class:`SnowparkS3Strategy`.
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.stager import (
    SnowparkStagingStrategy,
    register_session_cleanup,
    register_snowpark_strategy,
)

if TYPE_CHECKING:
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.base import Driver

logger = logging.getLogger(__name__)


class SnowparkDatabricksStrategy(SnowparkStagingStrategy):
    """Bridge the ``databricks`` driver into a Snowpark session via an S3 bounce.

    Handles both directions:

    * :meth:`materialise` — issue ``INSERT OVERWRITE DIRECTORY``
      against the Databricks SQL warehouse to spill the source
      table to the bounce URI, then stage the CSV into Snowflake
      via a temporary external stage (same shape as
      :class:`SnowparkS3Strategy`), then return a Snowpark
      DataFrame reading from the transient staging table.
    * :meth:`persist` — write the Snowpark DataFrame to the bounce
      URI via ``copy_into_location``, then issue ``COPY INTO``
      against the Databricks warehouse to load the CSV into the
      target table.

    Cleanup: the Snowflake-side stage + transient table are dropped
    via the SnowparkS3Strategy machinery; the bounce objects on S3
    are removed at the end of each call (unless
    :data:`~ovid2.config.settings.SNOWPARK_STAGING_KEEP` is set).
    """

    driver_type = "databricks"

    #: Per-session record of (bounce_bucket, bounce_prefix) tuples
    #: created during the run — used by the atexit hook to remove
    #: bounce objects. Keyed by ``id(session)``.
    _bounce_prefixes: dict[int, list[tuple[str, str]]] = {}

    #: Per-session atexit-hook registration guard.
    _cleanups_scheduled: dict[int, set[str]] = {}

    # ------------------------------------------------------------------
    # Public data plane
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
        """Spill ``file_name`` from Databricks to the bounce and return a Snowpark DF.

        Args:
            session: The Snowpark session wrapper.
            driver: A Databricks driver (native or legacy adapter).
            pipeline: Pipeline name (source-side lookup key prefix).
            batch_id: Batch identifier.
            file_name: Source-side Databricks table name. Either
                bare (``ENCOUNTER``) or dotted
                (``catalog.schema.table``). When dotted, overrides
                the driver's ``catalog`` / ``schema``.
            delimiter: CSV field separator. When ``None``, defaults
                to :data:`ovid2.config.settings.CSV_DELIMITER`.
            table_name: Ignored on read.

        Returns:
            A :class:`snowflake.snowpark.DataFrame` reading from the
            transient staging table.

        Raises:
            ValueError: When the bounce URI is not configured, or
                the driver has no catalog / schema, or Snowflake has
                no auth path to the bounce.
        """
        del table_name  # unused; databricks reads use file_name
        from ovid2.config.settings import CSV_DELIMITER

        merged = _merged_config(driver)
        delim = delimiter or CSV_DELIMITER
        table_stem = _table_stem(file_name)
        instance_id = _current_instance_id()

        bounce_bucket, bounce_prefix = _resolve_bounce_target(
            instance_id,
            pipeline,
            batch_id,
            table_stem,
            kind="in",
        )
        bounce_uri = _s3_dir_uri(bounce_bucket, bounce_prefix)

        native = _native_session(session)
        self._maybe_schedule_cleanup(session, instance_id)
        self._register_bounce_prefix(session, bounce_bucket, bounce_prefix)

        # -------- Databricks side: spill the source table --------
        qualified = _qualify_table(merged, file_name)
        unload_sql = _databricks_unload_sql(
            source_fqtn=qualified,
            bounce_uri=bounce_uri,
            field_delimiter=delim,
        )
        logger.info(
            "SnowparkDatabricksStrategy: INSERT OVERWRITE DIRECTORY '%s' FROM %s",
            bounce_uri,
            qualified,
        )
        _run_databricks_sql(merged, unload_sql)

        # -------- Snowflake side: delegate to S3 helpers --------
        # Build a "virtual" S3-shaped merged config so we can reuse
        # the SnowparkS3Strategy helpers verbatim. The driver's
        # AWS creds (if any) come from the bounce settings; the
        # S3 integration falls back to the Databricks-specific
        # override then the general one.
        s3_merged = _bounce_s3_merged_config(merged)

        from ovid2.drivers.snowflake.staging.s3 import (
            _build_create_stage_sql,
            _ensure_current_database,
            _ensure_writable_staging_schema,
            _escape_sql,
            _read_header_columns_from_s3,
        )
        from ovid2.drivers.snowflake.staging.s3 import (
            _stage_name as _s3_stage_name,
        )
        from ovid2.drivers.snowflake.staging.s3 import (
            _temp_table_fqtn as _s3_temp_table_fqtn,
        )

        _ensure_current_database(native)
        staging_schema = _ensure_writable_staging_schema(native)

        stage_name = _s3_stage_name(
            instance_id,
            pipeline,
            batch_id,
            table_stem,
            kind="in",
        )
        # Override the "S3" stage prefix with a Databricks-specific
        # one so operator reap-queries can distinguish the two.
        stage_name = _rebrand_stage_name(stage_name)

        create_stage_sql = _build_create_stage_sql(
            stage_name=stage_name,
            url=bounce_uri,
            merged_config=_stage_credentials_config(merged, s3_merged),
        )
        logger.info(
            "SnowparkDatabricksStrategy: CREATE TEMPORARY STAGE %s URL=%s",
            stage_name,
            bounce_uri,
        )
        native.sql(create_stage_sql).collect()

        # The bounce writer emits a single-part CSV under the
        # directory (Databricks' INSERT OVERWRITE DIRECTORY does
        # not preserve a user-chosen file name — it writes
        # ``part-00000-<uuid>.csv``). Discover it via a listing
        # call so the header read + COPY INTO ``FILES`` clause can
        # narrow to the right object.
        header_key = _first_part_key_under(
            bucket=bounce_bucket,
            prefix=bounce_prefix,
            merged_config=s3_merged,
        )
        columns = _read_header_columns_from_s3(
            bucket=bounce_bucket,
            key=header_key,
            delimiter=delim,
            merged_config=s3_merged,
        )

        temp_fqtn = _s3_temp_table_fqtn(
            staging_schema,
            instance_id,
            pipeline,
            batch_id,
            table_stem,
        )
        temp_fqtn = _rebrand_temp_table_fqtn(temp_fqtn)
        col_ddl = ", ".join(f'"{c}" VARCHAR' for c in columns)
        native.sql(f"CREATE OR REPLACE TRANSIENT TABLE {temp_fqtn} ({col_ddl})").collect()

        # COPY INTO — narrow to the specific object under the
        # directory-scoped stage.
        basename = header_key.split("/")[-1]
        copy_sql = (
            f"COPY INTO {temp_fqtn} FROM '@{stage_name}' "
            f"FILES = ('{_escape_sql(basename)}') "
            f"FILE_FORMAT = (TYPE = 'CSV' "
            f"FIELD_DELIMITER = '{_escape_sql(delim)}' "
            f"SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
            f"TRIM_SPACE = TRUE COMPRESSION = 'AUTO') "
            f"ON_ERROR = 'ABORT_STATEMENT' FORCE = TRUE"
        )
        logger.info(
            "SnowparkDatabricksStrategy: COPY INTO %s FROM @%s (file=%s)",
            temp_fqtn,
            stage_name,
            basename,
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
        """Write ``df`` to Databricks via an S3 bounce.

        Args:
            session: The Snowpark session wrapper.
            df: A Snowpark DataFrame.
            driver: The target Databricks driver.
            pipeline: Pipeline name.
            batch_id: Batch identifier.
            table_name: Databricks table name. Either bare
                (``ENCOUNTER``) or dotted
                (``catalog.schema.table``). When dotted, overrides
                the driver's ``catalog`` / ``schema``.
            delimiter: CSV field separator; defaults to
                :data:`~ovid2.config.settings.CSV_DELIMITER`.

        Raises:
            ValueError: When the bounce URI is not configured, or
                the driver has no catalog / schema, or Snowflake
                has no auth path to the bounce.
        """
        from ovid2.config.settings import CSV_DELIMITER

        merged = _merged_config(driver)
        delim = delimiter or CSV_DELIMITER
        instance_id = _current_instance_id()

        bounce_bucket, bounce_prefix = _resolve_bounce_target(
            instance_id,
            pipeline,
            batch_id,
            table_name,
            kind="out",
        )
        bounce_uri = _s3_dir_uri(bounce_bucket, bounce_prefix)

        native = _native_session(session)
        self._maybe_schedule_cleanup(session, instance_id)
        self._register_bounce_prefix(session, bounce_bucket, bounce_prefix)

        # -------- Snowflake side: write to bounce --------
        s3_merged = _bounce_s3_merged_config(merged)

        from ovid2.drivers.snowflake.staging.s3 import (
            _build_create_stage_sql,
            _copy_into_location,
            _ensure_current_database,
        )
        from ovid2.drivers.snowflake.staging.s3 import (
            _stage_name as _s3_stage_name,
        )

        _ensure_current_database(native)

        stage_name = _s3_stage_name(
            instance_id,
            pipeline,
            batch_id,
            table_name,
            kind="out",
        )
        stage_name = _rebrand_stage_name(stage_name)

        create_stage_sql = _build_create_stage_sql(
            stage_name=stage_name,
            url=bounce_uri,
            merged_config=_stage_credentials_config(merged, s3_merged),
        )
        logger.info(
            "SnowparkDatabricksStrategy: CREATE TEMPORARY STAGE %s URL=%s",
            stage_name,
            bounce_uri,
        )
        native.sql(create_stage_sql).collect()

        logger.info(
            "SnowparkDatabricksStrategy: copy_into_location → @%s/ (bounce)",
            stage_name,
        )
        _copy_into_location(
            df,
            stage_target=f"@{stage_name}/",
            field_delimiter=delim,
            single_file=False,
        )

        # -------- Databricks side: COPY INTO --------
        qualified = _qualify_table(merged, table_name)
        load_sql = _databricks_load_sql(
            target_fqtn=qualified,
            bounce_uri=bounce_uri,
            field_delimiter=delim,
        )
        logger.info(
            "SnowparkDatabricksStrategy: COPY INTO %s FROM '%s'",
            qualified,
            bounce_uri,
        )
        _run_databricks_sql(merged, load_sql)

    # ------------------------------------------------------------------
    # Cleanup bookkeeping
    # ------------------------------------------------------------------

    def _maybe_schedule_cleanup(
        self,
        session: EngineSession,
        instance_id: str,
    ) -> None:
        """Register an atexit hook that removes bounce objects.

        Skipped when :data:`~ovid2.config.settings.SNOWPARK_STAGING_KEEP`
        is truthy. Idempotent per ``(session, instance_id)`` pair.
        The Snowflake-side stage + transient table cleanup is
        handled by the SnowparkS3Strategy hook installed indirectly
        through the shared helpers.
        """
        from ovid2.config.settings import SNOWPARK_STAGING_KEEP

        if SNOWPARK_STAGING_KEEP:
            logger.info(
                "SnowparkDatabricksStrategy: SNOWPARK_STAGING_KEEP is set; "
                "skipping bounce cleanup for instance %s",
                instance_id,
            )
            return

        session_key = id(session)
        registered = self._cleanups_scheduled.setdefault(session_key, set())
        if instance_id in registered:
            return
        registered.add(instance_id)

        prefixes_ref = self._bounce_prefixes.setdefault(session_key, [])

        def _cleanup() -> None:
            for bucket, prefix in prefixes_ref:
                try:
                    _remove_bounce_prefix(bucket, prefix)
                except Exception as exc:
                    logger.warning(
                        "SnowparkDatabricksStrategy: bounce cleanup of "
                        "s3://%s/%s failed (%s); clean manually.",
                        bucket,
                        prefix,
                        exc,
                    )

        # Runs over the live session in ``SnowparkEngine.stop_session``
        # (atexit is only the backstop — by then the session is closed).
        register_session_cleanup(_native_session(session), _cleanup)

    def _register_bounce_prefix(
        self,
        session: EngineSession,
        bucket: str,
        prefix: str,
    ) -> None:
        """Record a bounce prefix for atexit cleanup on this session."""
        self._bounce_prefixes.setdefault(id(session), []).append((bucket, prefix))


# ---------------------------------------------------------------------------
# Module-level helpers (pyspark / boto3 / databricks-sql-free at module scope)
# ---------------------------------------------------------------------------


def _nonempty(value: Any) -> str | None:
    """Return ``value`` stripped as a string, or ``None`` when blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _current_instance_id() -> str:
    """Return the process-wide instance id (same source as other strategies)."""
    from ovid2.core.isolation import (
        _generate_instance_id,
        _instance_id_from_env,
    )

    return _instance_id_from_env() or _generate_instance_id()


def _merged_config(driver: Any) -> dict[str, Any]:
    """Return the driver's merged connection+secrets+location config."""
    if hasattr(driver, "as_legacy_src_args"):
        with contextlib.suppress(Exception):
            _, merged = driver.as_legacy_src_args()
            return dict(merged)
    location = getattr(driver, "location", None) or {}
    return dict(location)


def _table_stem(file_name: str) -> str:
    """Return the table-name stem for a bare or dotted table reference.

    ``ENCOUNTER`` → ``ENCOUNTER``. ``mycat.myschema.ENCOUNTER`` →
    ``ENCOUNTER``. Used only for stage / bounce-prefix naming; the
    actual Databricks-side identifier resolution happens in
    :func:`_qualify_table`.
    """
    if not file_name:
        return ""
    return file_name.rsplit(".", 1)[-1]


def _qualify_table(merged: dict[str, Any], reference: str) -> str:
    """Return a backtick-quoted three-part Databricks identifier.

    Resolution order:

    * When ``reference`` already looks dotted (two or three parts),
      use it verbatim (still backtick-quoting each component).
    * Otherwise, prepend ``catalog`` and ``schema`` from the merged
      driver config.

    Raises:
        ValueError: When the reference is bare and either ``catalog``
            or ``schema`` is unset.
    """
    parts = [p for p in reference.split(".") if p]
    if len(parts) == 3:
        catalog, schema, table = parts
    elif len(parts) == 2:
        catalog_fallback = _nonempty(merged.get("catalog"))
        if not catalog_fallback:
            raise ValueError(
                f"SnowparkDatabricksStrategy: reference '{reference}' has "
                "no catalog and the driver's connection has no catalog. "
                "Set 'catalog' on the connection or use "
                "'catalog.schema.table'."
            )
        catalog = catalog_fallback
        schema, table = parts
    elif len(parts) == 1:
        catalog_fallback = _nonempty(merged.get("catalog"))
        schema_fallback = _nonempty(merged.get("schema"))
        if not (catalog_fallback and schema_fallback):
            raise ValueError(
                f"SnowparkDatabricksStrategy: reference '{reference}' is "
                "bare but the driver has no catalog + schema. Provide "
                "'catalog.schema.table' or set catalog + schema on the "
                "connection / address."
            )
        catalog = catalog_fallback
        schema = schema_fallback
        table = parts[0]
    else:
        raise ValueError(f"SnowparkDatabricksStrategy: empty table reference ({reference!r}).")
    return f"`{_escape_backtick(catalog)}`.`{_escape_backtick(schema)}`.`{_escape_backtick(table)}`"


def _escape_backtick(value: str) -> str:
    """Escape a backtick-quoted Databricks identifier component."""
    return value.replace("`", "``")


def _escape_sql(value: str) -> str:
    """Escape a single-quoted SQL string literal.

    Doubles single quotes and backslashes. Both dialects (Snowflake
    and Databricks) accept the same escaping under standard SQL
    string-literal rules.
    """
    return value.replace("\\", "\\\\").replace("'", "''")


def _native_session(session: EngineSession) -> Any:
    """Return the real ``snowflake.snowpark.Session`` from an adapter."""
    native = getattr(session, "native", None)
    if native is None:
        return session
    return native


def _resolve_bounce_target(
    instance_id: str,
    pipeline: str,
    batch_id: str,
    table_stem: str,
    *,
    kind: str,
) -> tuple[str, str]:
    """Return ``(bounce_bucket, bounce_key_prefix)`` for this call.

    Reads :data:`ovid2.config.settings.SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI`,
    parses the ``s3://<bucket>/<prefix>/`` URL, and appends the
    per-invocation subpath ``<instance_id>/<pipeline>/<batch_id>/<table>/<kind>/``.

    Raises:
        ValueError: When the bounce URI is unset or malformed.
    """
    from ovid2.config.settings import (
        SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI,
    )

    uri = _nonempty(SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI)
    if not uri:
        raise ValueError(
            "SnowparkDatabricksStrategy: no bounce bucket configured. "
            "Set OVID2_SNOWPARK_STAGING_DATABRICKS_BOUNCE_URI to an "
            "s3://bucket/prefix/ URI reachable by both Snowflake and "
            "Databricks (via Unity Catalog external location on the "
            "Databricks side, and STORAGE INTEGRATION or inline "
            "credentials on the Snowflake side)."
        )
    bucket, base_prefix = _parse_s3_uri(uri)

    safe = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        f"{instance_id}_{pipeline}_{batch_id}_{table_stem}_{kind}",
    )
    # Instance-scoped, human-readable per-invocation subdir.
    sub = f"{instance_id}/{pipeline}/{batch_id}/{safe}"
    prefix_parts = [p for p in (base_prefix, sub) if p]
    prefix = "/".join(prefix_parts)
    return bucket, prefix


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Return ``(bucket, prefix)`` for an ``s3://bucket/prefix/`` URL.

    A trailing slash is stripped from the returned prefix so
    :func:`_s3_dir_uri` can reassemble it deterministically.
    """
    if not uri.startswith("s3://"):
        raise ValueError(
            f"SnowparkDatabricksStrategy: bounce URI '{uri}' is not "
            "an s3:// URL. Only S3 bounces are supported today."
        )
    remainder = uri[5:]
    if "/" in remainder:
        bucket, prefix = remainder.split("/", 1)
    else:
        bucket, prefix = remainder, ""
    return bucket, prefix.rstrip("/")


def _s3_dir_uri(bucket: str, prefix: str) -> str:
    """Assemble the ``s3://<bucket>/<prefix>/`` directory URI."""
    if prefix:
        return f"s3://{bucket}/{prefix}/"
    return f"s3://{bucket}/"


def _bounce_s3_merged_config(merged: dict[str, Any]) -> dict[str, Any]:
    """Return an S3-shaped merged config for the bounce bucket.

    The bounce credentials are entirely separate from the
    Databricks warehouse credentials; the Databricks driver carries
    only ``host`` / ``http_path`` / ``token`` (and catalog / schema).
    We read the bounce's AWS keys from dedicated env vars so
    operators can rotate them independently of any address config.

    Falls back to an empty dict when no AWS keys are configured
    (default ``boto3`` provider chain — EC2 metadata / ``AWS_*``
    env / …).

    Note:
        Passed to :func:`ovid2.drivers.snowflake.staging.s3._read_header_columns_from_s3`
        and :func:`_build_create_stage_sql`, both of which honour
        the same key names as :class:`~ovid2.drivers.s3.driver.S3Driver`.
    """
    from ovid2.config.settings import (
        SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID,
        SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY,
        SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SESSION_TOKEN,
        SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION,
    )

    del merged  # currently no per-driver override; may become one later
    out: dict[str, Any] = {"format": "csv"}
    aki = _nonempty(SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_ACCESS_KEY_ID)
    ask = _nonempty(SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SECRET_ACCESS_KEY)
    ast_ = _nonempty(SNOWPARK_STAGING_DATABRICKS_BOUNCE_AWS_SESSION_TOKEN)
    region = _nonempty(SNOWPARK_STAGING_DATABRICKS_BOUNCE_REGION)
    if aki and ask:
        out["aws_access_key_id"] = aki
        out["aws_secret_access_key"] = ask
        if ast_:
            out["aws_session_token"] = ast_
    if region:
        out["region"] = region
    return out


def _stage_credentials_config(
    merged: dict[str, Any],
    s3_merged: dict[str, Any],
) -> dict[str, Any]:
    """Return the merged-config dict passed to :func:`s3._build_create_stage_sql`.

    Auth path resolution consulted by that function (in order):

    1. :data:`ovid2.config.settings.SNOWPARK_STAGING_S3_INTEGRATION` —
       consulted directly by the S3 helper. The Databricks-specific
       override (:data:`~ovid2.config.settings.SNOWPARK_STAGING_DATABRICKS_BOUNCE_S3_INTEGRATION`),
       if set, is copied into the general setting for the duration
       of the call via a monkey-patched attribute — see
       :func:`_build_create_stage_sql`'s use of ``s3.SNOWPARK_STAGING_S3_INTEGRATION``.
    2. Inline AWS credentials from the bounce settings.

    The function returns the credentials dict; :func:`_build_create_stage_sql`
    picks the auth path based on the module-level integration setting.
    """
    del merged  # currently no per-driver AWS creds; separated for future use
    return dict(s3_merged)


def _rebrand_stage_name(s3_stage_name: str) -> str:
    """Swap the ``OVID2_STG_S3_`` prefix for ``OVID2_STG_DBX_``.

    Keeps operator reap queries able to distinguish databricks-strategy
    stages from plain-S3-strategy stages.
    """
    return s3_stage_name.replace("OVID2_STG_S3_", "OVID2_STG_DBX_", 1)


def _rebrand_temp_table_fqtn(s3_fqtn: str) -> str:
    """Swap the ``OVID2_STG_S3_`` prefix for ``OVID2_STG_DBX_``."""
    return s3_fqtn.replace("OVID2_STG_S3_", "OVID2_STG_DBX_", 1)


# ---------------------------------------------------------------------------
# Databricks-side SQL
# ---------------------------------------------------------------------------


def _databricks_unload_sql(
    *,
    source_fqtn: str,
    bounce_uri: str,
    field_delimiter: str,
) -> str:
    """Build ``INSERT OVERWRITE DIRECTORY '…' USING csv … SELECT * FROM …``.

    Uses the Databricks SQL CSV writer with header + operator's
    field delimiter. Overwrite semantics ensure a rerun of the same
    ``(instance_id, pipeline, batch_id, table)`` tuple produces a
    clean directory.
    """
    return (
        f"INSERT OVERWRITE DIRECTORY '{_escape_sql(bounce_uri)}' "
        f"USING csv "
        f"OPTIONS ('header' = 'true', "
        f"'delimiter' = '{_escape_sql(field_delimiter)}') "
        f"SELECT * FROM {source_fqtn}"
    )


def _databricks_load_sql(
    *,
    target_fqtn: str,
    bounce_uri: str,
    field_delimiter: str,
) -> str:
    """Build ``COPY INTO <target> FROM '<uri>' FILEFORMAT = CSV …``.

    Uses ``mergeSchema=true`` so a CSV whose columns don't exactly
    match the target's DDL still loads (Databricks widens the
    target when the SQL warehouse has the ``CREATE TABLE`` privilege;
    otherwise it errors out cleanly). ``force=true`` disables the
    default "COPY INTO is idempotent on filename" behaviour so the
    same bounce URI can be reused across reruns of the same
    ``(pipeline, batch_id)`` — matches the operator's expectation
    that a rerun replaces the target.
    """
    return (
        f"COPY INTO {target_fqtn} "
        f"FROM '{_escape_sql(bounce_uri)}' "
        f"FILEFORMAT = CSV "
        f"FORMAT_OPTIONS ('header' = 'true', "
        f"'delimiter' = '{_escape_sql(field_delimiter)}') "
        f"COPY_OPTIONS ('mergeSchema' = 'true', 'force' = 'true')"
    )


def _run_databricks_sql(merged: dict[str, Any], sql: str) -> None:
    """Execute ``sql`` against the Databricks SQL warehouse.

    Reads ``host`` / ``http_path`` / ``token`` from the driver's
    merged config. All three are required. Uses the
    ``databricks-sql-connector`` package, imported lazily inside
    this function body so the module stays SDK-free at import time.

    Raises:
        ValueError: When any of the three credentials is missing.
        RuntimeError: When the connector package is not installed
            on the host running the Snowpark job.
    """
    host = _nonempty(merged.get("host"))
    http_path = _nonempty(merged.get("http_path"))
    token = _nonempty(merged.get("token"))
    if not (host and http_path and token):
        raise ValueError(
            "SnowparkDatabricksStrategy: driver's connection is "
            "missing one of host / http_path / token. All three "
            "are required to talk to the Databricks SQL warehouse."
        )

    try:
        from databricks import sql as dbx_sql  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "SnowparkDatabricksStrategy: databricks-sql-connector is "
            "not installed. Install it into the environment running "
            "the Snowpark job (pip install databricks-sql-connector)."
        ) from exc

    hostname = host.replace("https://", "").replace("http://", "").rstrip("/")
    conn = dbx_sql.connect(
        server_hostname=hostname,
        http_path=http_path,
        access_token=token,
    )
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
        finally:
            cursor.close()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bounce-object listing / cleanup
# ---------------------------------------------------------------------------


def _first_part_key_under(
    *,
    bucket: str,
    prefix: str,
    merged_config: dict[str, Any],
) -> str:
    """Return the key of the first ``part-*.csv`` under ``s3://<bucket>/<prefix>/``.

    Databricks' ``INSERT OVERWRITE DIRECTORY`` writes one or more
    ``part-<n>-<uuid>.csv`` files under the target directory (plus
    a ``_SUCCESS`` marker). We narrow to the first ``part-*``
    object; if there are multiple parts we still only need one for
    the header line — the ``COPY INTO`` step reads the whole
    directory via the stage.

    Note: for small tables, ``INSERT OVERWRITE`` typically emits
    exactly one part. For larger tables the current strategy still
    only reads one part's header — that's fine because Databricks
    writes each part with the same schema.

    Raises:
        ValueError: When no ``part-*`` file is found under the
            prefix.
    """
    import boto3  # local import per module docstring

    kwargs: dict[str, Any] = {"service_name": "s3"}
    region = _nonempty(merged_config.get("region"))
    if region:
        kwargs["region_name"] = region
    aki = _nonempty(merged_config.get("aws_access_key_id"))
    ask = _nonempty(merged_config.get("aws_secret_access_key"))
    ast_ = _nonempty(merged_config.get("aws_session_token"))
    if aki and ask:
        kwargs["aws_access_key_id"] = aki
        kwargs["aws_secret_access_key"] = ask
        if ast_:
            kwargs["aws_session_token"] = ast_

    client = boto3.client(**kwargs)
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix + "/")
    for entry in resp.get("Contents", []) or []:
        key = entry.get("Key", "") or ""
        basename = key.rsplit("/", 1)[-1]
        if basename.startswith("part-") and basename.endswith(".csv"):
            return key
    raise ValueError(
        f"SnowparkDatabricksStrategy: no part-*.csv found under "
        f"s3://{bucket}/{prefix}/. Did the Databricks INSERT "
        "OVERWRITE DIRECTORY step actually run? Check the SQL "
        "warehouse logs."
    )


def _remove_bounce_prefix(bucket: str, prefix: str) -> None:
    """Best-effort ``DELETE`` of every object under ``s3://<bucket>/<prefix>/``.

    Called from the atexit hook. Any per-object failure is logged
    at debug level so the hook can never crash the interpreter.
    """
    import boto3

    client = boto3.client("s3")
    paginator = client.get_paginator("list_objects_v2")
    to_delete: list[ObjectIdentifierTypeDef] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix + "/"):
        for entry in page.get("Contents", []) or []:
            key = entry.get("Key")
            if key:
                to_delete.append({"Key": key})
    if not to_delete:
        return
    # delete_objects tops out at 1000 keys per call.
    for chunk_start in range(0, len(to_delete), 1000):
        chunk = to_delete[chunk_start : chunk_start + 1000]
        try:
            client.delete_objects(Bucket=bucket, Delete={"Objects": chunk})
        except Exception as exc:
            logger.debug(
                "SnowparkDatabricksStrategy: bounce delete_objects "
                "chunk failed (%s); some objects may remain under "
                "s3://%s/%s/",
                exc,
                bucket,
                prefix,
            )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# Self-register at module import. The snowflake driver's package
# ``__init__.py`` imports the ``staging`` subpackage, whose
# ``__init__`` imports this module. Idempotent — re-registering the
# same driver_type overwrites the previous entry.
register_snowpark_strategy(SnowparkDatabricksStrategy)


__all__ = ["SnowparkDatabricksStrategy"]
