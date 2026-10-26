"""``SnowparkAzureBlobStrategy`` — stage Azure Blob objects via a Snowflake external stage.

ovid2: allow-sql-fstring — every SQL statement in this module is
constructed from trusted internal values. Instance IDs come from
:mod:`ovid2.core.isolation`, stage/table names are built with
:func:`re.sub` and uppercased, and inline credential values pass
through :func:`_escape_sql`. The strategy is only reachable via
:class:`SnowparkEngineStager` after the compat gate has already
vetted the pairing.

Registered at module import time via
:func:`ovid2.core.engines.stager.register_snowpark_strategy`.
:mod:`ovid2.drivers.snowflake.staging.__init__` triggers the import
so the strategy self-registers whenever the snowflake driver is
loaded (which happens unconditionally during CLI / web startup).

Data flow — Azure Blob → Snowpark
---------------------------------

1. Resolve the ``azure://<container>@<account>.blob.<suffix>/<prefix>/<pipeline>/<batch>/<file>``
   URL from the driver's location. Snowflake's external stages use
   the ``azure://`` scheme (NOT ``abfss://`` — that's the Hadoop
   driver's scheme).
2. ``CREATE OR REPLACE TEMPORARY STAGE <name>
   URL='azure://<account>.blob.<suffix>/<container>/<prefix>/'``.
   Auth path is one of:

   * ``STORAGE_INTEGRATION = <name>`` when
     :data:`ovid2.config.settings.SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION`
     is set (preferred).
   * ``CREDENTIALS = (AZURE_SAS_TOKEN='...')`` when the driver has
     a ``sas_token``.
   * ``CREDENTIALS = (AZURE_SAS_TOKEN='...')`` derived from the
     ``account_key`` via a container-scoped generated SAS. Requires
     ``azure-storage-blob`` (already in ``install_requires``).
   * Otherwise raise :class:`ValueError` — Snowflake cannot reach
     Azure Blob blind; the account's default identity has no
     integration path. Managed-identity + service-principal
     support requires storage-integration setup.
3. Read header line locally via ``azure-storage-blob``.
4. Create transient landing table, ``COPY INTO`` it, return
   ``session.table(fqtn)``.

Data flow — Snowpark → Azure Blob
---------------------------------

1. Same stage creation targeted at the write directory.
2. ``df.write.copy_into_location('@<stage>/<table>/',
   file_format_type='csv', …, single=<from location.single_file>)``
   writes the CSV directly into Azure Blob. No GET step.

Instance-scoped cleanup
-----------------------

Stages and transient tables are dropped on process exit via an
:mod:`atexit` hook. Temporary stages auto-clean at session end
regardless — the atexit hook is a belt-and-suspenders that also
cleans before the session naturally ends. Skipped when
:data:`ovid2.config.settings.SNOWPARK_STAGING_KEEP` is truthy.
"""

from __future__ import annotations

import atexit
import contextlib
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


class SnowparkAzureBlobStrategy(SnowparkStagingStrategy):
    """Bridge the ``azure_blob`` driver into a Snowpark session.

    Handles both directions:

    * :meth:`materialise` — ``CREATE STAGE`` pointing at the source
      URL, ``COPY INTO`` a transient table, return
      ``session.table(fqtn)``.
    * :meth:`persist` — ``CREATE STAGE`` pointing at the destination
      URL, ``df.write.copy_into_location`` directly into ADLS. No
      download step.

    Both directions register a session-scoped atexit cleanup on the
    first call. Skipped when
    :data:`~ovid2.config.settings.SNOWPARK_STAGING_KEEP` is truthy.
    """

    driver_type = "azure_blob"

    #: Class-level record of atexit-registered instance ids.
    _cleanups_scheduled: dict[int, set[str]] = {}

    #: Per-session list of stage names.
    _stages: dict[int, list[str]] = {}

    #: Per-session list of transient tables.
    _temp_tables: dict[int, list[str]] = {}

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
        """Stage ``file_name`` from Azure Blob and return a Snowpark DF."""
        del table_name  # unused; Azure reads use file_name
        from ovid2.config.settings import CSV_DELIMITER

        merged = _merged_config(driver)
        _require_csv(merged, "materialise")
        account = _require_account(merged)
        endpoint_suffix = _endpoint_suffix(merged)
        container = _require_container(merged)
        prefix = _nonempty(merged.get("prefix")) or ""
        delim = delimiter or CSV_DELIMITER

        instance_id = _current_instance_id()
        table_stem = os.path.splitext(os.path.basename(file_name))[0]
        stage_name = _stage_name(instance_id, pipeline, batch_id, table_stem, kind="in")
        read_dir_url = _azure_dir_url(
            account,
            endpoint_suffix,
            container,
            prefix,
        )

        native = _native_session(session)
        self._maybe_schedule_cleanup(session, instance_id)
        _ensure_current_database(native)
        staging_schema = _ensure_writable_staging_schema(native)

        create_stage_sql = _build_create_stage_sql(
            stage_name=stage_name,
            url=read_dir_url,
            merged_config=merged,
            container=container,
        )
        logger.info(
            "SnowparkAzureBlobStrategy: CREATE TEMPORARY STAGE %s URL=%s",
            stage_name,
            read_dir_url,
        )
        native.sql(create_stage_sql).collect()
        self._register_stage(session, stage_name)

        columns = _read_header_columns_from_azure(
            account=account,
            endpoint_suffix=endpoint_suffix,
            container=container,
            blob=_azure_blob_name(prefix, file_name),
            delimiter=delim,
            merged_config=merged,
        )

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
            "SnowparkAzureBlobStrategy: COPY INTO %s FROM @%s (file=%s)",
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
        """Write ``df`` to Azure Blob via an external stage. No GET step."""
        from ovid2.config.settings import CSV_DELIMITER

        merged = _merged_config(driver)
        _require_csv(merged, "persist")
        account = _require_account(merged)
        endpoint_suffix = _endpoint_suffix(merged)
        container = _require_container(merged)
        prefix = _nonempty(merged.get("prefix")) or ""
        delim = delimiter or CSV_DELIMITER
        single_file = bool(merged.get("single_file", False))

        instance_id = _current_instance_id()
        stage_name = _stage_name(
            instance_id,
            pipeline,
            batch_id,
            table_name,
            kind="out",
        )
        write_dir_url = _azure_dir_url(
            account,
            endpoint_suffix,
            container,
            prefix,
        )

        native = _native_session(session)
        self._maybe_schedule_cleanup(session, instance_id)
        _ensure_current_database(native)

        create_stage_sql = _build_create_stage_sql(
            stage_name=stage_name,
            url=write_dir_url,
            merged_config=merged,
            container=container,
        )
        logger.info(
            "SnowparkAzureBlobStrategy: CREATE TEMPORARY STAGE %s URL=%s",
            stage_name,
            write_dir_url,
        )
        native.sql(create_stage_sql).collect()
        self._register_stage(session, stage_name)

        stage_target = f"@{stage_name}/{table_name}/"
        logger.info(
            "SnowparkAzureBlobStrategy: copy_into_location → %s (single=%s)",
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
    # Cleanup bookkeeping (identical shape to SnowparkS3Strategy)
    # ------------------------------------------------------------------

    def _maybe_schedule_cleanup(
        self,
        session: EngineSession,
        instance_id: str,
    ) -> None:
        """Register an atexit hook that drops the stages + transient tables."""
        from ovid2.config.settings import SNOWPARK_STAGING_KEEP

        if SNOWPARK_STAGING_KEEP:
            logger.info(
                "SnowparkAzureBlobStrategy: SNOWPARK_STAGING_KEEP is set; "
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
                        "SnowparkAzureBlobStrategy: DROP STAGE %s failed "
                        "(%s); the stage is temporary and Snowflake will "
                        "auto-clean at session end.",
                        stage,
                        exc,
                    )
            for fqtn in temp_tables_ref:
                try:
                    native.sql(f"DROP TABLE IF EXISTS {fqtn}").collect()
                except Exception as exc:
                    logger.warning(
                        "SnowparkAzureBlobStrategy: DROP TABLE %s failed (%s); clean manually.",
                        fqtn,
                        exc,
                    )

        atexit.register(_cleanup)

    def _register_stage(self, session: EngineSession, stage_name: str) -> None:
        self._stages.setdefault(id(session), []).append(stage_name)

    def _register_temp_table(self, session: EngineSession, fqtn: str) -> None:
        self._temp_tables.setdefault(id(session), []).append(fqtn)


# ---------------------------------------------------------------------------
# Module-level helpers
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

    Works for both native
    :class:`~ovid2.drivers.azure_blob.driver.AzureBlobDriver` and the
    D2 legacy adapter. Falls back to ``driver.location`` when
    neither :meth:`as_legacy_src_args` is present nor callable.
    """
    if hasattr(driver, "as_legacy_src_args"):
        with contextlib.suppress(Exception):
            _, merged = driver.as_legacy_src_args()
            return dict(merged)
    location = getattr(driver, "location", None) or {}
    return dict(location)


def _require_csv(merged: dict[str, Any], op: str) -> None:
    """Raise ``ValueError`` when the driver's format is not CSV."""
    fmt = str(merged.get("format", "csv") or "csv").lower()
    if fmt != "csv":
        raise ValueError(
            f"SnowparkAzureBlobStrategy.{op}: only supports fmt='csv' "
            f"(got {fmt!r}). Non-CSV Azure Blob feeds must run under "
            "--engine pyspark."
        )


def _require_account(merged: dict[str, Any]) -> str:
    """Return the storage account name or raise a descriptive ``ValueError``.

    When the config carries a ``connection_string`` we parse it and
    extract ``AccountName`` — many operators only register the
    portal string and never set ``account_name`` explicitly.
    """
    account = _nonempty(merged.get("account_name"))
    if account:
        return account
    conn_str = _nonempty(merged.get("connection_string"))
    if conn_str:
        parsed = _parse_connection_string(conn_str)
        account = _nonempty(parsed.get("AccountName"))
        if account:
            return account
    raise ValueError(
        "SnowparkAzureBlobStrategy: no storage account resolved. "
        "Set ``account_name`` on the connection or include an "
        "``AccountName=…`` clause in a ``connection_string`` secret."
    )


def _require_container(merged: dict[str, Any]) -> str:
    """Return the container or raise a descriptive ``ValueError``."""
    container = _nonempty(merged.get("container"))
    if not container:
        raise ValueError(
            "SnowparkAzureBlobStrategy: no container resolved. Set "
            "``location.container`` on the address or "
            "``container_default`` on the connection."
        )
    return container


def _endpoint_suffix(merged: dict[str, Any]) -> str:
    """Return the endpoint suffix; defaults to ``core.windows.net``."""
    return _nonempty(merged.get("endpoint_suffix")) or "core.windows.net"


def _parse_connection_string(conn_str: str) -> dict[str, str]:
    """Split a portal-style connection string into k=v pairs.

    Wraps :func:`ovid2.drivers.azure_blob.metadata._parse_connection_string`
    so this module doesn't have to import the metadata module at
    module scope (keeps the boto-free / azure-free import contract).
    """
    from ovid2.drivers.azure_blob.metadata import _parse_connection_string as _p

    return _p(conn_str)


def _azure_blob_name(prefix: str, file_name: str) -> str:
    """Assemble the container-relative blob name ``{prefix}/{file}`` (address = exact location)."""
    parts = [p for p in (prefix, file_name) if p]
    return "/".join(parts)


def _azure_dir_url(
    account: str,
    endpoint_suffix: str,
    container: str,
    prefix: str,
) -> str:
    """Assemble the Snowflake-flavoured ``azure://…/`` directory URL.

    Snowflake expects the URL in the form
    ``azure://<account>.blob.<suffix>/<container>/<key-prefix>/``.
    The ``.blob.`` subdomain is required — the ``.dfs.`` (ADLS Gen2)
    endpoint is not supported by external stages.
    """
    parts = [p for p in (prefix,) if p]
    key = "/".join(parts)
    base = f"azure://{account}.blob.{endpoint_suffix}/{container}/"
    if key:
        return f"{base}{key}/"
    return base


def _stage_name(
    instance_id: str,
    pipeline: str,
    batch_id: str,
    table: str,
    *,
    kind: str,
) -> str:
    """Return a stage name safe for DDL interpolation."""
    safe = re.sub(
        r"[^A-Za-z0-9]+",
        "_",
        f"{kind}_{instance_id}_{pipeline}_{batch_id}_{table}",
    )
    return f"OVID2_STG_AZ_{safe}".upper()


def _temp_table_fqtn(
    staging_schema: str,
    instance_id: str,
    pipeline: str,
    batch_id: str,
    stem: str,
) -> str:
    """Return the fully-qualified transient-table name."""
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{instance_id}_{pipeline}_{batch_id}_{stem}")
    return f"{staging_schema}.OVID2_STG_AZ_{safe}".upper()


def _escape_sql(value: str) -> str:
    """Escape a string literal for inline SQL (double single quotes, backslashes)."""
    return value.replace("\\", "\\\\").replace("'", "''")


def _native_session(session: EngineSession) -> Any:
    """Return the real Snowpark session from an engine adapter."""
    native = getattr(session, "native", None)
    if native is None:
        return session
    return native


_DEFAULT_STAGE_DATABASE: str = "SNOWFLAKE"
_DEFAULT_STAGE_SCHEMA: str = "INFORMATION_SCHEMA"


def _ensure_current_database(native: Any) -> None:
    """Bind a current database + schema when either is missing."""
    try:
        current_db = native.get_current_database()
    except Exception:
        current_db = None
    if not current_db:
        try:
            native.sql(f"USE DATABASE {_DEFAULT_STAGE_DATABASE}").collect()
        except Exception as exc:
            logger.warning(
                "SnowparkAzureBlobStrategy: could not USE DATABASE %s (%s)",
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
            "SnowparkAzureBlobStrategy: could not USE SCHEMA %s (%s)",
            _DEFAULT_STAGE_SCHEMA,
            exc,
        )


def _ensure_writable_staging_schema(native: Any) -> str:
    """Return a fully-qualified writable ``<db>.<schema>`` for transient tables."""
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
            "SnowparkAzureBlobStrategy: cannot resolve a writable "
            "staging schema — the session has no current database. "
            "Set the engine's default database, or export "
            "OVID2_SNOWPARK_STAGING_SCHEMA=<db>.<schema>."
        )
    current_db = str(current_db).strip('"')
    schema_fqn = f"{current_db}.OVID2_SNOWPARK_STAGING"
    try:
        native.sql(f"CREATE SCHEMA IF NOT EXISTS {schema_fqn}").collect()
        return schema_fqn
    except Exception as exc:
        raise RuntimeError(
            f"SnowparkAzureBlobStrategy: cannot create staging schema "
            f"{schema_fqn} ({exc}). Grant CREATE SCHEMA on {current_db} "
            "or export OVID2_SNOWPARK_STAGING_SCHEMA=<existing_writable_schema>."
        ) from exc


def _build_create_stage_sql(
    *,
    stage_name: str,
    url: str,
    merged_config: dict[str, Any],
    container: str,
) -> str:
    """Build the ``CREATE OR REPLACE TEMPORARY STAGE`` DDL for an Azure URL.

    Auth path resolution:

    1. ``STORAGE_INTEGRATION = <name>`` when
       :data:`ovid2.config.settings.SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION`
       is set.
    2. ``CREDENTIALS = (AZURE_SAS_TOKEN='...')`` when the driver has
       a ``sas_token`` secret.
    3. ``CREDENTIALS = (AZURE_SAS_TOKEN='...')`` generated on the fly
       from an ``account_key`` via
       :func:`azure.storage.blob.generate_container_sas` (12-hour
       validity — long enough for a batch, short enough to auto-
       expire before it becomes a durable secret).
    4. Otherwise raise :class:`ValueError`. Service-principal and
       managed-identity require pre-provisioned storage integration
       on the Snowflake side.

    Args:
        stage_name: Sanitised stage identifier.
        url: Full ``azure://…/`` directory URL.
        merged_config: Merged driver config.
        container: Container name (used for the on-the-fly SAS scope).

    Returns:
        A single-statement DDL string.

    Raises:
        ValueError: When no auth path is available.
    """
    from ovid2.config.settings import SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION

    integration = SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION
    sas = _nonempty(merged_config.get("sas_token"))
    account_key = _nonempty(merged_config.get("account_key"))
    connection_string = _nonempty(merged_config.get("connection_string"))
    if not account_key and connection_string:
        parsed = _parse_connection_string(connection_string)
        account_key = _nonempty(parsed.get("AccountKey"))

    if integration:
        auth_clause = f"STORAGE_INTEGRATION = {integration}"
    elif sas:
        # Snowflake wants the SAS token without the leading '?'.
        sas_clean = sas.lstrip("?")
        auth_clause = f"CREDENTIALS = (AZURE_SAS_TOKEN = '{_escape_sql(sas_clean)}')"
    elif account_key:
        sas_clean = _generate_container_sas(
            merged_config=merged_config,
            account_key=account_key,
            container=container,
        )
        auth_clause = f"CREDENTIALS = (AZURE_SAS_TOKEN = '{_escape_sql(sas_clean)}')"
    else:
        raise ValueError(
            "SnowparkAzureBlobStrategy: no Snowflake→Azure auth path "
            "available. Set OVID2_SNOWPARK_STAGING_AZURE_BLOB_INTEGRATION "
            "to a pre-provisioned STORAGE INTEGRATION name, or attach "
            "a sas_token / account_key / connection_string secret to "
            "the Azure Blob connection. Service-principal and managed-"
            "identity auth require the storage-integration path."
        )

    return (
        f"CREATE OR REPLACE TEMPORARY STAGE {stage_name} URL = '{_escape_sql(url)}' {auth_clause}"
    )


def _generate_container_sas(
    *,
    merged_config: dict[str, Any],
    account_key: str,
    container: str,
) -> str:
    """Generate a 12-hour read+write SAS token for ``container``.

    Uses :func:`azure.storage.blob.generate_container_sas`. Import is
    local so the module stays azure-free at module scope.

    Args:
        merged_config: Driver config; provides ``account_name`` (or
            parses one out of ``connection_string``).
        account_key: The storage account key.
        container: Container to scope the SAS to.

    Returns:
        The SAS token as a query-string fragment (no leading '?').

    Raises:
        ValueError: When the account name cannot be resolved.
    """
    from datetime import datetime, timedelta, timezone

    from azure.storage.blob import (
        ContainerSasPermissions,
        generate_container_sas,
    )

    account = _nonempty(merged_config.get("account_name"))
    if not account:
        conn_str = _nonempty(merged_config.get("connection_string"))
        if conn_str:
            parsed = _parse_connection_string(conn_str)
            account = _nonempty(parsed.get("AccountName"))
    if not account:
        raise ValueError("SnowparkAzureBlobStrategy: cannot generate SAS without an account name.")

    expiry = datetime.now(timezone.utc) + timedelta(hours=12)
    return generate_container_sas(
        account_name=account,
        container_name=container,
        account_key=account_key,
        permission=ContainerSasPermissions(
            read=True,
            write=True,
            delete=True,
            list=True,
            add=True,
            create=True,
        ),
        expiry=expiry,
    )


def _read_header_columns_from_azure(
    *,
    account: str,
    endpoint_suffix: str,
    container: str,
    blob: str,
    delimiter: str,
    merged_config: dict[str, Any],
) -> list[str]:
    """Read the header line of an Azure blob and return its columns.

    Uses ``azure-storage-blob`` with a range download (first 64 KB).
    Auth path mirrors the driver's own probe:

    1. ``connection_string`` — passed to
       :meth:`BlobServiceClient.from_connection_string`.
    2. ``account_key`` — used as-is on the ``BlobServiceClient``
       constructor.
    3. ``sas_token`` — appended to the account URL as a query.
    4. Otherwise :class:`azure.identity.DefaultAzureCredential` —
       walks env / managed identity / az CLI.

    Raises:
        ValueError: When ``blob`` contains wildcards, or the object
            is empty, or the header line has no columns.
    """
    if any(ch in blob for ch in "*?["):
        raise ValueError(
            f"SnowparkAzureBlobStrategy: cannot read header from a "
            f"glob ({blob!r}). Expand wildcards upstream."
        )

    # Local imports so module stays azure-free at module scope.
    from azure.storage.blob import BlobServiceClient

    account_url = f"https://{account}.blob.{endpoint_suffix}"
    conn_str = _nonempty(merged_config.get("connection_string"))
    account_key = _nonempty(merged_config.get("account_key"))
    sas = _nonempty(merged_config.get("sas_token"))

    if conn_str:
        client = BlobServiceClient.from_connection_string(conn_str)
    elif account_key:
        client = BlobServiceClient(
            account_url=account_url,
            credential=account_key,
        )
    elif sas:
        sas_q = sas.lstrip("?")
        client = BlobServiceClient(
            account_url=f"{account_url}?{sas_q}",
        )
    else:
        from azure.identity import DefaultAzureCredential

        client = BlobServiceClient(
            account_url=account_url,
            credential=DefaultAzureCredential(),
        )

    blob_client = client.get_blob_client(container=container, blob=blob)
    stream = blob_client.download_blob(offset=0, length=65536)
    body = stream.readall()
    if not body:
        raise ValueError(
            f"SnowparkAzureBlobStrategy: {container}/{blob} is empty; nothing to stage."
        )
    text = body.decode("utf-8", errors="replace")
    first = text.split("\n", 1)[0]
    columns: list[str] = []
    for raw in first.rstrip("\r").split(delimiter):
        col = raw.strip().lstrip("\ufeff").strip('"')
        columns.append(col or f"_c{len(columns)}")
    if not columns:
        raise ValueError(
            f"SnowparkAzureBlobStrategy: could not extract columns "
            f"from {container}/{blob} (delimiter={delimiter!r})."
        )
    return columns


def _copy_into_location(
    df: Any,
    *,
    stage_target: str,
    field_delimiter: str,
    single_file: bool,
) -> None:
    """Run ``df.write.copy_into_location`` with the CSV options."""
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

register_snowpark_strategy(SnowparkAzureBlobStrategy)


__all__ = ["SnowparkAzureBlobStrategy"]
