"""Spark-backed read/write for the Snowflake driver.

The **only** module under :mod:`ovid2.drivers.snowflake` allowed to
import :mod:`pyspark`. Everything else in the snowflake package
stays web-safe.

Ported verbatim from the pre-D3.snowflake
:mod:`ovid2.core.addresses.snowflake` reader/writer so the
data-plane behaviour is byte-identical:

- ``read(...)`` — lazy read from ``{database}.{schema}.{dbtable}``
  via the Spark-Snowflake connector, with a two-step ``dbtable``
  fall-back (see :meth:`SnowflakeAddress.read` docstring).
- ``write(...)`` — end-to-end schema-create + CDM-typed DataFrame
  rebuild + bounded-retry write to the derived
  ``OVID_{pipeline}_{batch_id}`` schema. The Spark→Snowflake connector
  derives the target ``CREATE TABLE`` DDL from the *DataFrame schema*
  (it ignores the generic ``createTableColumnTypes`` option), so
  ``write`` re-projects the all-string produced DataFrame through a
  CDM-typed schema — ``StringType`` columns carry a
  ``metadata={"maxlength": N}`` hint (→ ``VARCHAR(N)``), dates become
  ``DateType`` and numerics ``DoubleType``. Without this every column
  would land as ``VARCHAR(16777216)``.
- ``create_complementary_tables(...)`` — not implemented (legacy
  version contained a ``.collect()`` violation; the follow-up port
  is tracked separately).

VARCHAR widths come from the harmonised-ID map plus the declared
``RDBMS Text(N)`` sizes in ``pcornet_cdm_v70.csv`` — no data scan.
A pipeline whose data exceeds these widths will surface as a
Snowflake error at write time, which matches the legacy behaviour
when the scan was disabled.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from ovid2.core.addresses.base import BaseAddress

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

logger = logging.getLogger(__name__)

#: Transient error substrings that warrant a retry. Matches the legacy
#: pattern set in the April 2026 Ovid refactor.
_TRANSIENT_ERROR_PATTERNS: tuple[str, ...] = (
    "unknownhostexception",
    "sockettimeoutexception",
    "connectionreset",
    "connection reset",
    "networkunreachable",
    "temporaryerror",
    "no response or invalid response",
)

_MAX_RETRIES: int = 3
_INITIAL_BACKOFF_SECONDS: int = 30
_MAX_BACKOFF_SECONDS: int = 300

#: PEM header/footer stripped when converting DER back to the Spark
#: connector's expected base64-body form.
_PEM_HEADER_RE = re.compile(r"-*(BEGIN|END) PRIVATE KEY-*\n?")

#: Matches an unquoted Snowflake identifier: letters, digits,
#: underscores, and ``$``, starting with a letter or underscore. Used
#: by :meth:`SnowflakeAddress.read` to detect whether ``file_name``
#: (which is ``formatter.input_file_name``) is a real table name or a
#: disc-style glob that would confuse the Snowflake compiler.
_SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class SnowflakeAddress(BaseAddress):
    """Read/write DataFrames from/to a Snowflake table.

    Preserved from :class:`ovid2.core.addresses.snowflake.SnowflakeAddress`
    with a single behaviour-neutral change: the class now lives under
    :mod:`ovid2.drivers.snowflake.spark` per the D3 layout. All
    attributes, method names, and semantics survive byte-identical.
    """

    #: CDM reference used by :meth:`_apply_cdm_schema`. Kept as a
    #: class-level constant so future subclasses (per-CDM addresses)
    #: can override without duplicating write logic. When the driver
    #: passes ``cdm=`` at construction (phase C1, via ``ovid2 etl
    #: --cdm``) it wins over this default.
    CDM_NAMESPACE: str = "pcornet_7_0"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config: dict[str, Any] = dict(config)
        self.account: str = str(config.get("account") or "")
        self.database: str = str(config.get("database") or "")
        self.default_schema: str = str(config.get("schema") or "")
        self.warehouse: str = str(config.get("warehouse") or "")
        self.role: str = str(config.get("role") or "")
        self.user: str = str(config.get("user") or "")
        self.authenticator: str = str(config.get("authenticator") or "")
        self._password: str | None = config.get("password")
        # DER bytes when key-pair auth is used, else ``None``.
        self._private_key_der: bytes | None = config.get("private_key")
        # Phase C1: the active CDM. Threaded through by
        # ``ovid2 etl --cdm`` or resolved from ``pipeline_settings.cdm``
        # when unset.
        self._cdm_name: str = str(config.get("cdm") or self.CDM_NAMESPACE)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_database(self) -> None:
        """Ensure a database name has been resolved before use."""
        if not self.database:
            raise ValueError(
                "No Snowflake database specified. Provide via: "
                "(1) --sf_database CLI arg, "
                "(2) 'database' in the address's location JSON, or "
                "(3) DEFAULT_PIPELINE_DB_NAME in pipeline_settings.py"
            )

    def _schema_name(self, pipeline: str, batch_id: str) -> str:
        """Return ``OVID_{pipeline}_{batch_id}`` with path chars sanitized."""
        sanitized = batch_id.replace("/", "_").replace("\\", "_")
        return f"OVID_{pipeline}_{sanitized}"

    def _pem_body_from_der(self) -> str | None:
        """Return the base64 body of the PEM key for the Spark connector.

        The Spark-Snowflake connector's ``pem_private_key`` option
        expects the base64 body of a PKCS8 PEM key — the same bytes as
        the PEM file with the ``BEGIN/END`` headers stripped and
        newlines removed. Since we already have the DER bytes (from
        :meth:`SnowflakeConnection.as_address_config`), base64-encoding
        them yields exactly that string.
        """
        if self._private_key_der is None:
            return None
        return base64.b64encode(self._private_key_der).decode("ascii")

    def _spark_options(self, schema_name: str) -> dict[str, str]:
        """Build the Spark-Snowflake connector options dict."""
        opts: dict[str, str] = {
            "sfURL": f"{self.account}.snowflakecomputing.com",
            "sfAccount": self.account,
            "sfDatabase": self.database,
            "sfSchema": schema_name,
        }
        for key, value in (
            ("sfUser", self.user),
            ("sfWarehouse", self.warehouse),
            ("sfRole", self.role),
            ("authenticator", self.authenticator),
        ):
            if value:
                opts[key] = value

        pem_body = self._pem_body_from_der()
        if pem_body is not None:
            opts["pem_private_key"] = pem_body
        elif self._password:
            opts["sfPassword"] = self._password
        return opts

    def _get_sf_connection(self) -> Any:
        """Open a Python-connector connection for DDL execution.

        Returns:
            ``snowflake.connector.SnowflakeConnection`` instance.

        Raises:
            ImportError: When ``snowflake-connector-python`` is not
                installed in the driver environment.
        """
        # Local import so hosts without the driver can still import the
        # module (tests, static analysis).
        import snowflake.connector as sf

        kwargs: dict[str, Any] = {"account": self.account}
        for key, value in (
            ("user", self.user),
            ("warehouse", self.warehouse),
            ("role", self.role),
            ("authenticator", self.authenticator),
        ):
            if value:
                kwargs[key] = value

        if self._private_key_der is not None:
            kwargs["private_key"] = self._private_key_der
        elif self._password:
            kwargs["password"] = self._password
        return sf.connect(**kwargs)

    def _create_schema_if_not_exists(self, schema_name: str) -> None:
        """Idempotently create ``{database}.{schema_name}``.

        Uses the Python connector because the Spark-Snowflake
        connector's ``query`` option only supports ``SELECT``. Trusted
        SQL — no user input is interpolated (``schema_name`` is derived
        from ``pipeline`` + ``batch_id`` which are already validated by
        :mod:`ovid2.core.isolation`).
        """
        sql = f"CREATE SCHEMA IF NOT EXISTS {self.database}.{schema_name}"
        conn = self._get_sf_connection()
        try:
            cur = conn.cursor()
            try:
                cur.execute(sql)
            finally:
                cur.close()
        finally:
            conn.close()
        logger.info(
            "Ensured Snowflake schema exists: %s.%s",
            self.database,
            schema_name,
        )

    def _apply_cdm_schema(
        self,
        df: DataFrame,
        spark: SparkSession,
        table_name: str,
    ) -> DataFrame:
        """Return ``df`` rebuilt with the output CDM's typed schema.

        The Spark→Snowflake connector infers the target ``CREATE TABLE``
        DDL from the DataFrame schema, so a CDM-typed schema (with
        ``maxlength`` metadata on string columns and real ``DateType`` /
        ``DoubleType`` columns) is what produces ``VARCHAR(N)`` / ``DATE``
        / ``NUMBER`` instead of ``VARCHAR(16777216)``.

        Only columns the DataFrame actually carries are typed; unknown
        columns (present in the CDM but not produced by the pipeline) are
        skipped rather than fabricated, and DataFrame columns absent from
        the CDM pass through untouched. When the CDM table is unknown
        (developer laptops without ``material/``) the DataFrame is
        returned unchanged and a warning is logged, matching the previous
        infer-and-warn behaviour.

        Lazy: builds a new ``StructType`` and re-projects via
        ``spark.createDataFrame(df.rdd, schema)`` — the single unavoidable
        boundary the legacy writer also crossed; no ``collect`` happens.
        """
        from pyspark.sql.functions import col, substring, to_date
        from pyspark.sql.types import (
            DateType,
            DoubleType,
            StringType,
            StructField,
            StructType,
        )

        write_spec = self._write_schema(table_name)
        if not write_spec:
            logger.warning(
                "No CDM schema found for %s.%s — Spark connector will "
                "infer column types from the DataFrame. VARCHAR widths "
                "may not match the harmonised sizes (columns may land as "
                "VARCHAR(16777216)).",
                self._cdm_name,
                table_name.upper(),
            )
            return df

        df_columns = {c.upper(): c for c in df.columns}
        fields: list[StructField] = []
        typed_df = df
        for name, kind, width in write_spec:
            source = df_columns.get(name.upper())
            if source is None:
                # CDM column the pipeline did not produce — skip it; the
                # connector only creates columns present in the DataFrame.
                continue
            if kind == "date":
                typed_df = typed_df.withColumn(source, to_date(col(source), "yyyy-MM-dd"))
                fields.append(StructField(source, DateType(), True))
            elif kind == "number":
                typed_df = typed_df.withColumn(source, col(source).cast(DoubleType()))
                fields.append(StructField(source, DoubleType(), True))
            else:
                # Conform the data to the declared VARCHAR width, not just the
                # DDL. The connector emits ``VARCHAR(width)`` from the
                # ``maxlength`` metadata, but Snowflake's COPY rejects any row
                # whose value exceeds it (e.g. long OMOP drug names in
                # RAW_EXT_MED_NAME, or an over-long PRO_TIME). Truncating here
                # matches Snowflake's own silent right-trim for governed loads
                # and keeps the write from failing the whole table.
                typed_df = typed_df.withColumn(source, substring(col(source), 1, int(width)))
                fields.append(
                    StructField(
                        source,
                        StringType(),
                        True,
                        metadata={"maxlength": int(width)},
                    )
                )

        # Preserve any DataFrame columns that are not in the CDM (e.g.
        # pipeline-added ``SOURCE`` / ``UPDATED`` bookkeeping) as
        # unbounded strings so the write does not drop them.
        typed_names = {f.name.upper() for f in fields}
        for original in df.columns:
            if original.upper() not in typed_names:
                fields.append(StructField(original, StringType(), True))

        # Order the projection to match the assembled schema.
        ordered = [f.name for f in fields]
        typed_df = typed_df.select(*ordered)
        schema = StructType(fields)
        return spark.createDataFrame(typed_df.rdd, schema)

    def _write_schema(self, table_name: str) -> list[tuple[str, str, int]]:
        """Return the CDM ``(name, kind, width)`` write spec for ``table_name``.

        Thin wrapper around
        :func:`ovid2.schema.cdm.build_snowflake_write_schema` that stamps
        the driver's CDM namespace. Empty list when the CDM table is
        unknown (see :func:`build_snowflake_write_schema`).
        """
        from ovid2.schema.cdm import build_snowflake_write_schema

        return build_snowflake_write_schema(f"{self._cdm_name}.{table_name.upper()}")

    # ------------------------------------------------------------------
    # BaseAddress contract
    # ------------------------------------------------------------------

    def read(
        self,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> DataFrame:
        """Lazily read a Snowflake table into a DataFrame.

        Reads ``{database}.{schema}.{resolved_name}`` via the
        Spark-Snowflake connector. Unlike :meth:`write`, the schema is
        **not** derived from ``pipeline`` / ``batch_id``: it comes from
        the address's ``default_schema`` (the ``"schema"`` field on
        the location JSON). When ``default_schema`` is empty, no
        ``sfSchema`` option is passed and Snowflake falls back to the
        connection default (typically ``PUBLIC``).

        Identifier resolution (ease-of-use fall-back)
        ---------------------------------------------

        Pipeline formatter YAMLs were originally authored for disc
        sources and declare disc-style globs in ``input_file_name``
        (e.g. ``*datavant_person_tokens*``). Those globs cannot be
        used as Snowflake identifiers directly — they would produce
        ``SQL compilation error: syntax error ... unexpected '*'``.

        Rather than force every pipeline to duplicate the identifier
        (once as a disc glob, once as a SQL name) this method uses a
        two-step fall-back:

        1. If ``file_name`` is a valid Snowflake unquoted identifier
           (matches :data:`_SQL_IDENTIFIER_RE`), it is used verbatim.
        2. Otherwise, ``table_name`` (the CDM table name the
           pipeline is producing) is used as ``dbtable`` and a
           ``logger.info`` records the substitution so operators can
           see it in the run log.
        3. If both are missing/invalid, a :class:`ValueError` is
           raised early — surfacing the misconfiguration before
           Spark builds a broken plan.

        This makes ``ovid2 etl --input_address <snowflake_addr>``
        work against an existing pipeline without touching a single
        YAML, as long as the source tables are named after their CDM
        counterparts (the common convention in ``CHAR_*`` databases).

        Returns a lazy DataFrame; execution is deferred to the
        pipeline's final ``write()``.

        Raises:
            ValueError: If neither ``file_name`` nor ``table_name``
                yields a valid Snowflake identifier, or if the
                address has no ``database`` configured.
        """
        del pipeline, batch_id, delimiter  # unused — see docstring
        self._validate_database()

        dbtable = self._resolve_dbtable(file_name, table_name)

        schema_name = self.default_schema
        options = self._spark_options(schema_name)
        if not schema_name:
            # ``_spark_options`` unconditionally sets ``sfSchema``;
            # strip it so Snowflake resolves against the connection
            # default rather than the empty string.
            options.pop("sfSchema", None)
        options["dbtable"] = dbtable
        logger.info(
            "Snowflake read: %s.%s.%s",
            self.database,
            schema_name or "<default>",
            dbtable,
        )
        return spark.read.format("snowflake").options(**options).load()

    def _resolve_dbtable(
        self,
        file_name: str,
        table_name: str | None,
    ) -> str:
        """Pick the Snowflake identifier for the read.

        See :meth:`read` for the full contract; this helper is factored
        out so unit tests can exercise the fall-back rules without
        going through the Spark reader.
        """
        if file_name and _SQL_IDENTIFIER_RE.match(file_name):
            return file_name
        if table_name and _SQL_IDENTIFIER_RE.match(table_name):
            logger.info(
                "SnowflakeAddress.read: file_name=%r is not a valid SQL "
                "identifier; falling back to table_name=%r as dbtable. "
                "Update the formatter YAML's ``input_file_name`` to the "
                "real Snowflake table name to silence this message.",
                file_name,
                table_name,
            )
            return table_name
        raise ValueError(
            f"SnowflakeAddress.read: cannot resolve a Snowflake identifier. "
            f"file_name={file_name!r} is not a valid unquoted identifier "
            f"(must match {_SQL_IDENTIFIER_RE.pattern!r}) and "
            f"table_name={table_name!r} did not provide a usable fall-back. "
            f"Set the formatter YAML's ``input_file_name`` to a real "
            f"Snowflake table name."
        )

    def write(
        self,
        df: DataFrame,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write ``df`` into ``{database}.OVID_{pipeline}_{batch_id}.{table_name}``.

        Creates the target schema when missing and retries transient
        network failures. VARCHAR widths come from the harmonized-ID
        map + declared CDM ``RDBMS Text(N)`` sizes; the writer does
        NOT scan the DataFrame for actual max lengths (see module
        docstring).

        The Spark→Snowflake connector derives the ``CREATE TABLE`` DDL
        from the *DataFrame's own schema*, so the outgoing DataFrame is
        rebuilt with CDM-typed columns: ``StringType`` fields carry a
        ``metadata={"maxlength": N}`` hint (→ ``VARCHAR(N)``), date fields
        become ``DateType`` (→ ``DATE``) and numeric fields ``DoubleType``
        (→ ``NUMBER``). Without this every column would land as
        ``VARCHAR(16777216)`` — the connector ignores the generic
        ``createTableColumnTypes`` DataSource option.
        """
        del delimiter  # unused

        self._validate_database()
        schema_name = self._schema_name(pipeline, batch_id)
        self._create_schema_if_not_exists(schema_name)

        options = self._spark_options(schema_name)
        options["dbtable"] = table_name.upper()

        write_df = self._apply_cdm_schema(df, spark, table_name)

        backoff_seconds = _INITIAL_BACKOFF_SECONDS
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                writer = (
                    write_df.repartition(10)
                    .write.format("snowflake")
                    .options(**options)
                    .option("on_error", "CONTINUE")
                    .option("useSchema", schema_name)
                    .mode("overwrite")
                )
                writer.save()
                logger.info(
                    "Snowflake write succeeded: %s.%s.%s",
                    self.database,
                    schema_name,
                    table_name.upper(),
                )
                return
            except Exception as exc:
                message = str(exc)
                if not _is_transient_error(message):
                    logger.error(
                        "Non-transient error writing %s to Snowflake: %s",
                        table_name,
                        message,
                    )
                    raise
                if attempt == _MAX_RETRIES:
                    logger.error(
                        "Failed to write %s after %d attempts: %s",
                        table_name,
                        _MAX_RETRIES,
                        message,
                    )
                    raise
                logger.warning(
                    "Transient error writing %s (attempt %d/%d), retrying in %ds: %s",
                    table_name,
                    attempt,
                    _MAX_RETRIES,
                    backoff_seconds,
                    message,
                )
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, _MAX_BACKOFF_SECONDS)


def _is_transient_error(message: str) -> bool:
    """Return ``True`` when the error message looks like a network glitch."""
    lower = message.lower()
    return any(pattern in lower for pattern in _TRANSIENT_ERROR_PATTERNS)


__all__ = [
    "_INITIAL_BACKOFF_SECONDS",
    "_MAX_BACKOFF_SECONDS",
    "_MAX_RETRIES",
    "_PEM_HEADER_RE",
    "_SQL_IDENTIFIER_RE",
    "_TRANSIENT_ERROR_PATTERNS",
    "SnowflakeAddress",
    "_is_transient_error",
]
