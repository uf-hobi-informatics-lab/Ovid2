"""Engine-scoped data-plane bridge (``EngineStager``).

ovid2: allow-sql-fstring — the ``_NativeSnowparkStrategy`` inline
SQL statements interpolate driver-location fields (``database`` /
``schema``) plus internal table names. All values are validated
upstream (the driver's ``location`` dict is JSON-loaded by
:func:`ovid2.config.addresses.load_address_config`; pipeline /
batch / table names come through
:mod:`ovid2.tools.table_specs.parse_table_specs`). No operator
input reaches the f-string interpolations directly.

Introduced by phase A+B of the engine/storage-orthogonality plan (ADR:
the adr-engine-storage-orthogonality design note).

Rationale
---------

Every engine (``pyspark`` / ``databricks_spark`` / ``snowpark``) needs to
materialise a source dataset into a form its compute layer can operate
on, and to persist a produced DataFrame to a target the compute layer
may or may not be able to write directly.

* For **Spark-family** engines the two operations are trivial:
  :meth:`Driver.read` / :meth:`Driver.write` are already Spark-native
  and can be called directly against any registered driver — the
  runtime image ships the connectors (JDBC, hadoop-aws, ABFS)
  every driver's ``spark.py`` module depends on.

* For **Snowpark**, DataFrames live inside Snowflake compute; the only
  driver whose data plane is natively reachable is ``snowflake``
  itself. Reaching a disc CSV, an S3 object, or an ADLS blob requires
  staging the source data into a Snowflake stage first (``PUT`` for
  disc; external stage w/ storage integration or ad-hoc credentials
  for s3 / azure_blob), then letting Snowpark read from the stage.

Rather than encode this asymmetry on the driver side (via a per-driver
:meth:`~ovid2.drivers.base.Driver.engine_families` set — the pre-A/B
mechanism), we move the responsibility onto the engine. Each engine
family owns an :class:`EngineStager` whose :meth:`supports` method
enumerates the driver types it can move data through, and whose
:meth:`materialise` / :meth:`persist` methods do the actual work.

Phase A+B ships two stagers:

* :class:`SparkEngineStager` — pure pass-through to
  :meth:`Driver.read` / :meth:`Driver.write`. Used by both
  ``pyspark`` and ``databricks_spark`` engines. Zero behaviour
  change relative to the pre-A/B world.

* :class:`SnowparkEngineStager` — dispatch-on-``driver.type_name`` via
  a strategy map. Today the map contains only
  ``{"snowflake": NativeSnowparkStrategy}``, so the compat surface is
  byte-identical to the pre-A/B world (Snowpark still rejects
  cross-peripheral pairings). Phase C will add ``"disc"`` (via user
  stage ``@~``); Phase D will add ``"s3"`` and ``"azure_blob"`` (via
  storage integrations or ad-hoc credentials).

Import-safety
-------------

This module intentionally imports **nothing heavy** at module scope —
no ``pyspark``, no ``snowflake.snowpark``, no ``boto3``. The
:class:`EngineStager` base class is pure Python; the concrete stagers
call into their strategies (which live in sibling modules) only when
:meth:`materialise` / :meth:`persist` runs. This keeps the web tier
(``ovid2.web.services.jobs_svc``) able to import the stager registry
via :func:`stager_class_for_engine_type` and consult
:meth:`supports` without dragging in a JVM or a Snowflake driver.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.base import Driver


class EngineStagerUnsupported(RuntimeError):
    """Raised when a stager has no strategy for a given driver type.

    The compat check (:mod:`ovid2.drivers.engine_compat`) inspects
    :meth:`EngineStager.supports` *before* :meth:`materialise` /
    :meth:`persist` would be called, so raising this at data-plane
    time is a "should not happen" belt-and-suspenders — the pipeline
    already refused the pairing at plan time. Callers catch it as an
    ordinary ``RuntimeError``.

    Attributes:
        engine_family: The engine family that could not stage
            (``spark`` / ``snowpark``).
        driver_type: The driver's ``type_name`` (``disc`` / ``s3`` / …).
    """

    def __init__(self, engine_family: str, driver_type: str) -> None:
        super().__init__(
            f"Engine family '{engine_family}' has no staging strategy "
            f"for driver type '{driver_type}'. This is a plan-time "
            "check leak — the compat check should have refused the "
            "pairing before this method ran."
        )
        self.engine_family = engine_family
        self.driver_type = driver_type


class EngineStager(ABC):
    """Move data between a :class:`Driver` and an engine session.

    Subclassed once per engine family:

    * :class:`SparkEngineStager` — one instance per Spark-family
      engine (``pyspark`` / ``databricks_spark``). Pure pass-through.
    * :class:`SnowparkEngineStager` — one instance per Snowpark
      engine. Dispatches to a strategy per source/target driver
      type.

    The stager is instantiated once per engine (``engine.get_stager()``)
    and reused across every table in a copy/move run.
    """

    #: Family value this stager services. Mirrors
    #: :class:`~ovid2.core.engines.protocols.EngineFamily` but kept as a
    #: bare string so the module can stay pyspark-free.
    family: str = ""

    #: Whether :meth:`materialise` / :meth:`persist` accept a legacy
    #: :class:`~ovid2.core.addresses.base.BaseAddress` (whose ``read`` /
    #: ``write`` already speak the engine's session) in place of a driver.
    #: ``True`` only for the Spark stager; the pipeline resolves a driver for
    #: every other stager.
    accepts_legacy_address: bool = False

    def __init__(self, engine: BaseEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Capability discovery (metadata; safe to call from the web tier)
    # ------------------------------------------------------------------

    @classmethod
    @abstractmethod
    def supports(cls, driver_type: str) -> bool:
        """Return ``True`` iff this stager can move data via ``driver_type``.

        The compat check consults this classmethod to refuse an
        incompatible engine/driver pairing at plan time (before a
        Spark cluster is spun up or a Snowpark session is built).

        Args:
            driver_type: A key of :data:`ovid2.drivers.registry.DRIVERS`
                (e.g. ``"disc"``, ``"s3"``, ``"snowflake"``).

        Returns:
            ``True`` when the stager knows a path to reach that
            driver; ``False`` otherwise.
        """

    @classmethod
    @abstractmethod
    def supported_driver_types(cls) -> frozenset[str]:
        """Return the full set of driver types this stager supports.

        Used by the compat check to render helpful error messages
        (name the supported types when refusing a pair).
        """

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    @abstractmethod
    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        """Read data from ``driver`` and return an engine-native DataFrame.

        Spark-family stagers pass through to :meth:`Driver.read`
        verbatim. Snowpark stagers may PUT a local file to a
        Snowflake stage, then ``session.read.csv('@stage/…')`` — the
        return value is a Snowpark DataFrame either way.

        Args:
            driver: The source driver (any registered type).
            pipeline: Pipeline name.
            batch_id: Batch identifier.
            file_name: Source-side lookup key (disc glob, dbtable,
                object key, …).
            delimiter: Optional field delimiter override.
            table_name: Optional logical CDM table name (used by
                Snowflake's identifier fallback and by
                ``ovid2 tools copy``'s destination naming).

        Raises:
            EngineStagerUnsupported: When no strategy exists for
                ``driver.type_name``.
        """

    @abstractmethod
    def persist(
        self,
        df: Any,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write an engine DataFrame to ``driver``.

        Spark-family stagers pass through to :meth:`Driver.write`
        verbatim. Snowpark stagers may write to a Snowflake stage,
        then GET the resulting files (disc), or leave them in an
        external stage (s3 / azure_blob).

        Raises:
            EngineStagerUnsupported: When no strategy exists for
                ``driver.type_name``.
        """

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def session(self) -> EngineSession:
        """Return the underlying engine session (cached by the engine)."""
        return self._engine.get_session()


# ---------------------------------------------------------------------------
# Spark-family stager
# ---------------------------------------------------------------------------


class SparkEngineStager(EngineStager):
    """Stager used by ``pyspark`` and ``databricks_spark``.

    * **Native** — drivers with a Spark data plane (``spark_capable``:
      disc, s3, azure_blob, snowflake, access) pass straight through to
      :meth:`Driver.read` / :meth:`Driver.write`. A remote
      (Databricks-Connect) session cannot see local files, so local-file
      drivers are bridged for engines with ``supports_local_files =
      False``.
    * **Bridge** — every other driver (postgres, mssql, gcs, databricks,
      any future type) goes through the Arrow bridge
      (:mod:`ovid2.core.engines.bridge`) on the driver's DuckDB strategy.
      So the Spark family reaches every storage type.
    """

    family = "spark"

    #: Legacy addresses are the Spark data plane of their driver.
    accepts_legacy_address = True

    #: Driver types whose native Spark data plane reads local files.
    _LOCAL_FILE_DRIVERS: frozenset[str] = frozenset({"disc", "access"})

    #: JVM classes a driver's native Spark plane needs. When the session's
    #: classpath lacks them (a bare ``pip install pyspark`` host without
    #: the image's hadoop-aws / hadoop-azure / spark-snowflake jars) the
    #: driver is bridged instead of failing with ``ClassNotFoundException``.
    _NATIVE_JVM_CLASSES: dict[str, str] = {
        "s3": "org.apache.hadoop.fs.s3a.S3AFileSystem",
        "azure_blob": "org.apache.hadoop.fs.azurebfs.SecureAzureBlobFileSystem",
        "snowflake": "net.snowflake.spark.snowflake.DefaultSource",
        "access": "net.ucanaccess.jdbc.UcanaccessDriver",
    }

    @classmethod
    def native_supports(cls, driver_type: str) -> bool:
        """Return ``True`` when ``driver_type`` has a native Spark data plane."""
        from ovid2.drivers.registry import DRIVERS, load_class

        if driver_type not in DRIVERS:
            return False
        try:
            return bool(load_class(driver_type).spark_capable)
        except (KeyError, ImportError, AttributeError):
            return False

    @classmethod
    def supports(cls, driver_type: str) -> bool:
        # Native Spark plane, or the Arrow bridge through the driver's
        # DuckDB strategy (every registered driver has one).
        return cls.native_supports(driver_type) or _bridgeable(driver_type)

    @classmethod
    def supported_driver_types(cls) -> frozenset[str]:
        from ovid2.drivers.registry import DRIVERS

        return frozenset(t for t in DRIVERS if cls.supports(t))

    def _use_native(self, driver: Driver) -> bool:
        if _is_legacy_address(driver):
            return True
        driver_type = str(getattr(driver, "type_name", "") or "")
        if not self.native_supports(driver_type):
            return False
        local_ok = bool(getattr(self._engine, "supports_local_files", True))
        if not local_ok and driver_type in self._LOCAL_FILE_DRIVERS:
            return False
        return self._jvm_has(self._NATIVE_JVM_CLASSES.get(driver_type))

    def _jvm_has(self, class_name: str | None) -> bool:
        """Return ``True`` when ``class_name`` loads in the session's JVM (or none is needed)."""
        if not class_name:
            return True
        cache: dict[str, bool] = self._engine.__dict__.setdefault("_jvm_class_cache", {})
        if class_name in cache:
            return cache[class_name]
        ok = True
        try:
            jvm = getattr(getattr(self.session(), "_jvm", None), "java", None)
            if jvm is not None:
                jvm.lang.Class.forName(class_name)
        except Exception:
            ok = False
        cache[class_name] = ok
        return ok

    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        if self._use_native(driver):
            return driver.read(
                self.session(),
                pipeline,
                batch_id,
                file_name,
                delimiter,
                table_name=table_name,
            )
        from ovid2.core.engines.bridge import bridge_read

        return bridge_read(
            self._engine, driver, pipeline, batch_id, file_name, delimiter, table_name=table_name
        )

    def persist(
        self,
        df: Any,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        if self._use_native(driver):
            driver.write(
                df,
                self.session(),
                pipeline,
                batch_id,
                table_name,
                delimiter,
            )
            return
        from ovid2.core.engines.bridge import bridge_write

        bridge_write(self._engine, df, driver, pipeline, batch_id, table_name, delimiter)


def _is_legacy_address(obj: Any) -> bool:
    """Whether ``obj`` is a legacy ``BaseAddress`` (Spark data plane, no ``type_name``)."""
    from ovid2.core.addresses.base import BaseAddress

    return isinstance(obj, BaseAddress)


def _bridgeable(driver_type: str) -> bool:
    """Return ``True`` when the Arrow bridge can reach ``driver_type`` (DuckDB strategy)."""
    from ovid2.core.engines.duckdb_stager import DuckDBEngineStager

    return DuckDBEngineStager.supports(driver_type)


# ---------------------------------------------------------------------------
# Snowpark stager
# ---------------------------------------------------------------------------


class SnowparkStagingStrategy(ABC):
    """One strategy in :class:`SnowparkEngineStager`'s dispatch map.

    Concrete strategies live in sibling modules and are wired into
    :attr:`SnowparkEngineStager._STRATEGIES`. Phase A+B ships only
    :class:`_NativeSnowparkStrategy` (for the ``snowflake`` driver);
    later phases add disc, s3, azure_blob.
    """

    #: The driver type this strategy handles (``"snowflake"`` /
    #: ``"disc"`` / ``"s3"`` / ``"azure_blob"``).
    driver_type: str = ""

    @abstractmethod
    def materialise(
        self,
        session: EngineSession,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None,
        table_name: str | None,
    ) -> Any: ...

    @abstractmethod
    def persist(
        self,
        session: EngineSession,
        df: Any,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None,
    ) -> None: ...


class _NativeSnowparkStrategy(SnowparkStagingStrategy):
    """Snowpark → Snowflake driver: native session ``read`` / ``write``.

    Read side delegates to :meth:`Driver.read` (which under Snowpark
    reads via :meth:`Session.table`). Write side goes DIRECTLY to
    :meth:`DataFrame.write.save_as_table` — bypassing the disc-legacy
    :class:`~ovid2.drivers.snowflake.spark.SnowflakeAddress.write`
    path, which is Spark-only (uses ``df.repartition`` +
    ``df.write.format('snowflake')``). Under Snowpark neither
    ``repartition`` nor ``.format('snowflake')`` exists on
    :class:`snowflake.snowpark.DataFrame`.

    The write target is ``{database}.{schema}.{table}`` exactly as the
    driver's location names it — the address is the exact location, so
    ``pipeline`` / ``batch_id`` never contribute. A location without a
    schema is rejected at persist time.
    """

    driver_type = "snowflake"

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
        del pipeline, batch_id, delimiter  # unused for Snowflake reads
        # Native Snowpark read — bypass the Spark-only
        # :meth:`SnowflakeAddress.read` (which uses
        # ``spark.read.format("snowflake")``) and go directly to
        # :meth:`Session.table` with the fully-qualified name.
        location = getattr(driver, "location", None) or {}
        database, schema = _snowflake_location_db_schema(location)
        # Identifier resolution — mirror SnowflakeAddress._resolve_dbtable
        # so ``ovid2 etl`` YAMLs authored for disc still work under
        # Snowpark: prefer ``file_name`` when it's a valid SQL id,
        # else fall back to ``table_name``.
        import re

        id_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
        candidate = (
            file_name
            if id_re.match(file_name or "")
            else (table_name if id_re.match(table_name or "") else None)
        )
        if not candidate:
            raise ValueError(
                f"_NativeSnowparkStrategy.materialise: neither "
                f"file_name={file_name!r} nor table_name={table_name!r} "
                "is a valid Snowflake identifier. Update the caller "
                "(disc-style globs are not valid dbtable names)."
            )
        if not database:
            raise ValueError(
                "_NativeSnowparkStrategy.materialise: driver has no "
                "'database' set in its location. Configure the "
                "Snowflake address's `location.database` field."
            )
        from ovid2.drivers._layout import sql_ident

        if schema:
            fqtn = f"{sql_ident(database)}.{sql_ident(schema)}.{candidate.upper()}"
        else:
            fqtn = f"{sql_ident(database)}.{candidate.upper()}"
        native = getattr(session, "native", session)
        return _all_string_table(native, fqtn)

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
        del delimiter, pipeline, batch_id  # the address is the exact location
        from ovid2.drivers._layout import sql_ident

        # Reach into the driver's location for the target schema. We
        # accept both a native ``SnowflakeDriver`` (with a ``.location``
        # dict) and the legacy address bridge.
        location = getattr(driver, "location", None) or {}
        database, schema = _snowflake_location_db_schema(location)
        if not database or not schema:
            raise ValueError(
                "Snowflake output address must name DATABASE/SCHEMA — the address "
                f"is the exact write location (got database={database!r}, "
                f"schema={schema!r})."
            )

        # Ensure the target schema exists — Snowpark ``save_as_table``
        # will not auto-create it. ``IF NOT EXISTS`` makes it idempotent
        # and safe when the operator pre-created the schema.
        native = getattr(session, "native", session)
        native.sql(  # noqa: OVID2001 administrative DDL, not a data action
            f"CREATE SCHEMA IF NOT EXISTS {sql_ident(database)}.{sql_ident(schema)}"
        ).collect()

        # Write the produced DataFrame to ``{database}.{schema}.{table}``.
        #
        # We must NOT switch the session's current database/schema here.
        # The produced DataFrame's lazy Snowpark plan can reference
        # session-scoped temp views that live in the workspace schema —
        # notably the OMOP ``concept`` / ``concept_relationship`` shared
        # views a PCORnet→OMOP formatter resolves via an *unqualified*
        # ``session.table("concept")``. Those temp views were registered
        # (by ``register_shared_tables``) in the workspace schema bound by
        # ``SnowparkEngine.prepare_workspace``. Building the target from a
        # ``select`` (below) executes the whole lazy plan; if we ran
        # ``USE SCHEMA <output_schema>`` first, the embedded ``concept``
        # reference would resolve against the output schema at execution
        # time and fail with ``Object 'CONCEPT' does not exist``. So the
        # session namespace is left untouched and every target is named
        # fully-qualified.
        target_fqtn = f"{sql_ident(database)}.{sql_ident(schema)}.{table_name.upper()}"

        # Apply the CDM's declared column types (the Snowpark analogue of
        # the Spark connector's ``createTableColumnTypes``). The produced
        # DataFrame is all-string (every ``_common`` / formatter column is
        # a string, with ``lit("")`` for unset numerics). Writing those
        # strings straight into a typed OMOP table makes Snowflake cast
        # ``''`` / ``'12.2 - NI'`` into ``NUMBER`` columns and fail with
        # ``Numeric value '' is not recognized``. Mirroring the Spark
        # path, we (1) resolve the output CDM's typed columns, (2)
        # ``CREATE OR REPLACE TABLE`` with those types, and (3) insert the
        # DataFrame with a per-column ``TRY_CAST`` (numeric / date /
        # timestamp) after coercing empty strings to NULL — so a bad or
        # empty value lands as NULL instead of aborting the whole write
        # (the lenient equivalent of the Spark writer's
        # ``on_error=CONTINUE``). VARCHAR columns pass through untouched.
        typed_columns = _resolve_typed_columns(driver, table_name)
        if typed_columns:
            _write_typed(native, df, target_fqtn, typed_columns)
        else:
            # No CDM type info (developer laptop without ``material/``):
            # fall back to the untyped all-string create, matching the
            # Spark path's warn-and-infer behaviour.
            df.write.save_as_table(
                [database, schema, table_name.upper()],
                mode="overwrite",
            )


def _snowflake_location_db_schema(location: Any) -> tuple[str, str]:
    """Return ``(database, schema)`` for a Snowflake driver location.

    Explicit ``database`` / ``schema`` keys win; otherwise a ``path`` of
    the form ``DATABASE[/SCHEMA]`` (the generic web-UI address shape) is
    split. Either element is ``""`` when absent — unlike the read-side
    ``SnowflakeDriver._database_schema`` there is no ``PUBLIC`` default,
    so a schema-less write target is rejected by :meth:`persist`.
    """
    from ovid2.config.addresses import split_snowflake_path

    loc = dict(location or {})
    path_db, path_schema = split_snowflake_path(loc.get("path"))
    database = str(loc.get("database") or path_db or "").strip()
    schema = str(loc.get("schema") or path_schema or "").strip()
    return database, schema


def _all_string_table(native: Any, fqtn: str) -> Any:
    """Return ``native.table(fqtn)`` with every column cast to string.

    The PCORnet→OMOP formatters (and every legacy Ovid formatter) were
    authored against the disc reader's ``inferSchema=false`` contract:
    every source column arrives as a *string*, and numeric / date
    coercion happens only at the final typed write. The disc staging
    strategy honours this by declaring its transient staging table
    all-``VARCHAR`` (see :meth:`SnowparkDiscStrategy.materialise`).

    A native Snowflake source table, however, carries its *declared*
    column types (``NUMBER``, ``DATE``, …). When a formatter unions two
    or more such source legs by name (e.g. ``drug_exposure`` fanning in
    ``MED_ADMIN`` + ``PRESCRIBING`` + ``DISPENSING`` + ``IMMUNIZATION``),
    Snowflake reconciles each column to a common super-type across the
    branches. If one leg's column is ``NUMBER`` and another leg supplies
    an empty string for the same output column, the ``UNION`` coerces
    ``''`` into ``NUMBER`` and the whole plan aborts at execution time
    with ``100038 (22018): Numeric value '' is not recognized`` — a
    failure that only surfaces when the typed target CTAS forces the
    lazy union to materialise.

    Casting every source column to ``VARCHAR`` up front makes the native
    read behave exactly like the disc read: unions never coerce, and the
    downstream :func:`_write_typed` CTAS applies the CDM's declared types
    leniently via ``TRY_CAST`` (empty / malformed → NULL). Lazy: the
    ``select`` adds no action, execution is still deferred to the CTAS.

    Args:
        native: The real Snowpark session.
        fqtn: Fully-qualified ``db[.schema].TABLE`` source name.

    Returns:
        The Snowpark DataFrame with every column ``CAST(... AS VARCHAR)``,
        column names preserved.
    """
    table = native.table(fqtn)
    columns = list(getattr(table, "columns", []) or [])
    if not columns:
        return table
    # Reference each column through the DataFrame's own ``__getitem__``
    # (``table[name]``) rather than ``snowflake.snowpark.functions.col``
    # so this module needs no direct ``snowflake.snowpark`` import — the
    # Snowpark dependency stays confined to ``core/engines/snowpark.py``
    # and the ``drivers/snowflake/staging/*`` strategies (enforced by
    # ``tests/contract/test_no_pyspark_outside_engine.py``).
    projected = [table[name].cast("string").alias(name) for name in columns]
    return table.select(*projected)


def _resolve_typed_columns(
    driver: Any,
    table_name: str,
) -> list[tuple[str, str]]:
    """Return the output CDM's ``(COLUMN, snowflake_type)`` list, or ``[]``.

    The CDM namespace comes from the driver's ``location['cdm']`` stamp
    (set by ``ovid2 etl --cdm`` / the C1 route). When unset or unknown,
    returns ``[]`` so the caller falls back to the untyped write.
    """
    location = getattr(driver, "location", None) or {}
    cdm_name = str(location.get("cdm") or "").strip()
    if not cdm_name:
        return []
    from ovid2.schema.cdm import build_snowflake_typed_columns

    try:
        return build_snowflake_typed_columns(f"{cdm_name}.{table_name.upper()}")
    except Exception:
        return []


#: Snowflake types that require a lenient ``TRY_CAST`` from the
#: all-string DataFrame. VARCHAR / TEXT columns are copied verbatim.
_NON_VARCHAR_PREFIXES: tuple[str, ...] = (
    "NUMBER",
    "FLOAT",
    "DOUBLE",
    "INT",
    "DECIMAL",
    "NUMERIC",
    "DATE",
    "TIMESTAMP",
    "TIME",
    "BOOLEAN",
)


def _write_typed(
    native: Any,
    df: Any,
    target_fqtn: str,
    typed_columns: list[tuple[str, str]],
) -> None:
    """Create the typed target table and insert the DataFrame into it.

    Registers the produced DataFrame as a temporary view, then runs a
    single ``CREATE OR REPLACE TABLE <target> AS SELECT <casts> FROM
    <view>``. Numeric / temporal columns are wrapped in
    ``TRY_CAST(NULLIF(TRIM(TO_VARCHAR(<col>)), '') AS <type>)`` so an
    empty or malformed source value becomes NULL rather than aborting
    the statement (the Snowpark equivalent of the Spark connector's
    ``on_error=CONTINUE``). String columns pass through with a plain
    ``CAST`` to pick up the declared VARCHAR width.

    Args:
        native: The real Snowpark session.
        df: The produced (all-string) Snowpark DataFrame.
        target_fqtn: Fully-qualified ``db.schema.TABLE`` target.
        typed_columns: Ordered ``(COLUMN, snowflake_type)`` pairs from
            the output CDM.
    """
    import re

    #: Matches ``VARCHAR(250)`` / ``TEXT(50)`` / ``STRING ( 32 )`` and
    #: captures the declared character width so it can be enforced with a
    #: truncating ``LEFT`` rather than a hard ``CAST`` (which Snowflake
    #: refuses when the value overflows the declared width).
    _varchar_width_re = re.compile(
        r"^\s*(?:VARCHAR|TEXT|STRING|CHAR|NVARCHAR|NCHAR)\s*\(\s*(\d+)\s*\)\s*$",
        re.IGNORECASE,
    )

    df_columns = {str(c).strip('"').upper() for c in getattr(df, "columns", [])}

    # Stage the produced DataFrame as a temporary view so the typed
    # CREATE TABLE AS SELECT can reference it by name. The view lives in
    # the session's current (workspace) schema; the CTAS target is
    # fully-qualified, so no session-schema switch is needed.
    view_name = (
        "OVID2_SNOWPARK_WRITE_"
        + re.sub(
            r"[^A-Za-z0-9]+",
            "_",
            target_fqtn,
        ).upper()
    )
    _register_engine_view_local(df, view_name)

    select_exprs: list[str] = []
    for name, sf_type in typed_columns:
        upper_name = name.upper()
        if upper_name not in df_columns:
            # The formatter did not produce this CDM column — emit a
            # typed NULL so the table shape still matches the CDM.
            select_exprs.append(f'CAST(NULL AS {sf_type}) AS "{upper_name}"')
            continue
        src = f'"{upper_name}"'
        if sf_type.upper().startswith(_NON_VARCHAR_PREFIXES) and not sf_type.upper().startswith(
            "VARCHAR"
        ):
            # Lenient cast: empty string → NULL, bad value → NULL.
            select_exprs.append(
                f"TRY_CAST(NULLIF(TRIM(TO_VARCHAR({src})), '') AS {sf_type}) AS \"{upper_name}\""
            )
        else:
            width_match = _varchar_width_re.match(sf_type)
            if width_match is not None:
                # String column with a declared width. A plain
                # ``CAST(... AS VARCHAR(N))`` makes Snowflake *reject*
                # any value longer than ``N`` ("String '...' is too long
                # and would be truncated"), aborting the whole write. The
                # legacy Spark→Snowflake connector silently truncated
                # over-width strings on ingest; mirror that here with an
                # explicit ``LEFT(..., N)`` so an over-long source value
                # lands truncated instead of failing the table.
                width = width_match.group(1)
                select_exprs.append(
                    f'CAST(LEFT(TO_VARCHAR({src}), {width}) AS {sf_type}) AS "{upper_name}"'
                )
            else:
                select_exprs.append(f'CAST({src} AS {sf_type}) AS "{upper_name}"')

    select_clause = ", ".join(select_exprs)
    ctas = f"CREATE OR REPLACE TABLE {target_fqtn} AS SELECT {select_clause} FROM {view_name}"
    native.sql(ctas).collect()  # noqa: OVID2001 — CTAS is the write action


def _register_engine_view_local(df: Any, view_name: str) -> None:
    """Register ``df`` as a temp view (Spark or Snowpark spelling)."""
    create_spark = getattr(df, "createOrReplaceTempView", None)
    if callable(create_spark):
        create_spark(view_name)
        return
    create_snowpark = getattr(df, "create_or_replace_temp_view", None)
    if callable(create_snowpark):
        create_snowpark(view_name)
        return
    raise AttributeError(
        f"DataFrame of type {type(df).__name__!r} exposes neither "
        "'createOrReplaceTempView' nor 'create_or_replace_temp_view'."
    )


class SnowparkEngineStager(EngineStager):
    """Stager for the ``snowpark`` engine family.

    Data plane is *not* uniform across driver types — Snowpark
    DataFrames only exist inside Snowflake compute. This stager
    dispatches on ``driver.type_name`` via :attr:`_STRATEGIES`; a
    missing entry means the pair is not supported and
    :meth:`materialise` / :meth:`persist` raises
    :class:`EngineStagerUnsupported`.

    Phase A+B strategies:

    * ``snowflake`` → :class:`_NativeSnowparkStrategy`

    Later phases will add:

    * ``disc`` → Snowflake user stage ``@~/ovid_ingest/…``
    * ``s3`` → external stage via storage integration or ad-hoc creds
    * ``azure_blob`` → external stage via SAS or SP credentials
    """

    family = "snowpark"

    #: Dispatch map. Populated at class-definition time with the
    #: driver types Snowpark natively reaches today; later phases plug
    #: new strategies in by mutating this dict via a registration
    #: helper (:func:`register_snowpark_strategy`) so the dependency
    #: direction stays clean.
    _STRATEGIES: dict[str, type[SnowparkStagingStrategy]] = {
        "snowflake": _NativeSnowparkStrategy,
    }

    #: Driver types whose *writes* always take the Arrow bridge even
    #: though a server-side strategy exists. A disc write has to land
    #: every byte on this host anyway (the native path is COPY INTO @~ +
    #: GET), and the bridge writes through the DuckDB disc writer, which
    #: is byte-identical to Spark's univocity output — the native
    #: Snowflake unload quotes every field and folds the header case.
    _BRIDGED_WRITES: frozenset[str] = frozenset({"disc"})

    @classmethod
    def native_supports(cls, driver_type: str) -> bool:
        """Return ``True`` when ``driver_type`` has a server-side Snowpark strategy."""
        _load_snowpark_strategies()
        return driver_type in cls._STRATEGIES

    @classmethod
    def supports(cls, driver_type: str) -> bool:
        # Server-side staging strategy, or the Arrow bridge through the
        # driver's DuckDB strategy (postgres / mssql / access …).
        return cls.native_supports(driver_type) or _bridgeable(driver_type)

    @classmethod
    def supported_driver_types(cls) -> frozenset[str]:
        from ovid2.drivers.registry import DRIVERS

        return frozenset(t for t in DRIVERS if cls.supports(t))

    def _strategy_for(self, driver: Driver) -> SnowparkStagingStrategy | None:
        driver_type = getattr(driver, "type_name", "") or type(driver).__name__
        _load_snowpark_strategies()
        cls = type(self)._STRATEGIES.get(driver_type)
        if cls is None:
            if _bridgeable(str(driver_type)):
                return None  # the Arrow bridge handles it
            raise EngineStagerUnsupported(self.family, driver_type)
        return cls()

    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        strategy = self._strategy_for(driver)
        if strategy is None:
            from ovid2.core.engines.bridge import bridge_read

            native_df = bridge_read(
                self._engine,
                driver,
                pipeline,
                batch_id,
                file_name,
                delimiter,
                table_name=table_name,
            )
        else:
            native_df = strategy.materialise(
                self.session(),
                driver,
                pipeline,
                batch_id,
                file_name,
                delimiter,
                table_name,
            )
        return _snow_wrap(self._engine, native_df)

    def persist(
        self,
        df: Any,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        from ovid2.core.engines._snow_frame import unwrap

        df = unwrap(df)
        driver_type = str(getattr(driver, "type_name", "") or "")
        if driver_type != "snowflake":
            # Files carry the column names verbatim: restore the CDM's
            # authored case (Snowflake folds unquoted names to upper).
            df = _restore_cdm_case(df, driver, table_name)
        strategy = self._strategy_for(driver)
        if strategy is None or driver_type in self._BRIDGED_WRITES:
            from ovid2.core.engines.bridge import bridge_write

            bridge_write(self._engine, df, driver, pipeline, batch_id, table_name, delimiter)
            return
        strategy.persist(
            self.session(),
            df,
            driver,
            pipeline,
            batch_id,
            table_name,
            delimiter,
        )


def _restore_cdm_case(df: Any, driver: Any, table_name: str) -> Any:
    """Rename a native Snowpark frame's columns to the CDM's authored case.

    Snowflake folds unquoted identifiers to upper case, so an OMOP frame
    built with ``alias("care_site_id")`` comes back as ``CARE_SITE_ID``.
    Spark and DuckDB keep the authored name, and file outputs carry it
    in the header — so before a file write the frame is renamed to the
    CDM YAML spelling (matched case-insensitively). Columns the CDM does
    not declare keep their Spark-visible name (quotes stripped). A frame
    with no CDM stamp on the driver is returned unchanged.
    """
    location = getattr(driver, "location", None) or {}
    cdm_name = str(location.get("cdm") or "").strip()
    columns = list(getattr(df, "columns", []) or [])
    if not cdm_name or not columns:
        return df
    from ovid2.cdm.yaml_loader import load_table_field_names_as_authored

    authored = {n.upper(): n for n in load_table_field_names_as_authored(cdm_name, table_name)}
    if not authored:
        return df
    targets: list[str] = []
    for raw in columns:
        bare = raw[1:-1].replace('""', '"') if raw[:1] == raw[-1:] == '"' else raw
        targets.append(authored.get(bare.upper(), bare))
    if all(t == c for t, c in zip(targets, columns, strict=True)):
        return df
    quoted = ['"' + t.replace('"', '""') + '"' for t in targets]
    return df.to_df(*quoted)


def _snow_wrap(engine: Any, df: Any) -> Any:
    """Wrap a native Snowpark frame in the PySpark-shaped :class:`SnowFrame`."""
    from ovid2.core.engines._snow_frame import SnowFrame

    if isinstance(df, SnowFrame) or df is None:
        return df
    if type(df).__module__.startswith("snowflake.snowpark"):
        return SnowFrame(df, engine)
    return df


def _load_snowpark_strategies() -> None:
    """Import the self-registering Snowpark staging strategies (idempotent)."""
    import importlib

    with __import__("contextlib").suppress(ImportError):
        importlib.import_module("ovid2.drivers.snowflake.staging")


# ---------------------------------------------------------------------------
# Session-scoped cleanup (stage files / transient tables)
# ---------------------------------------------------------------------------

#: ``id(native session)`` -> run-once cleanup callables registered by the
#: Snowpark staging strategies. Drained by :func:`run_session_cleanups`.
_SESSION_CLEANUPS: dict[int, list[Callable[[], None]]] = {}


def register_session_cleanup(native: Any, fn: Callable[[], None]) -> Callable[[], None]:
    """Schedule ``fn`` to run before ``native``'s session closes.

    Staging strategies leave per-run artefacts (``@~`` stage files,
    transient tables, external stages) that must be removed *over the
    live session*. :meth:`SnowparkEngine.stop_session` calls
    :func:`run_session_cleanups` just before closing the session; an
    :mod:`atexit` hook is also registered as a backstop for runs that
    never reach ``stop_session`` (crash, ``SIGKILL`` excepted). The
    wrapper runs at most once, so whichever fires first wins.

    Args:
        native: The real Snowpark session the cleanup talks to.
        fn: Zero-arg cleanup callable. Must not raise (strategies log).

    Returns:
        The run-once wrapper (the callable actually registered).
    """
    import atexit

    done = {"ran": False}

    def _once() -> None:
        if done["ran"]:
            return
        done["ran"] = True
        fn()

    _SESSION_CLEANUPS.setdefault(id(native), []).append(_once)
    atexit.register(_once)
    return _once


def run_session_cleanups(native: Any) -> None:
    """Run (and forget) every cleanup registered for ``native``.

    Never raises: a failing cleanup is logged and the rest still run.
    """
    import logging

    for fn in _SESSION_CLEANUPS.pop(id(native), []):
        try:
            fn()
        except Exception as exc:
            logging.getLogger(__name__).warning("session cleanup failed: %s", exc)


# ---------------------------------------------------------------------------
# Registration helper (used by Phase C/D strategies)
# ---------------------------------------------------------------------------


def register_snowpark_strategy(strategy_cls: type[SnowparkStagingStrategy]) -> None:
    """Add ``strategy_cls`` to the Snowpark stager's dispatch map.

    Phase C/D strategies (``SnowparkDiscStrategy``, ``SnowparkS3Strategy``,
    ``SnowparkAzureBlobStrategy``) will each call this at module import
    time so plugging in a new peripheral is a one-line change from the
    strategy module's side. Idempotent: re-registering the same driver
    type overwrites the previous entry.

    Args:
        strategy_cls: A concrete subclass whose ``driver_type`` names
            a key of :data:`ovid2.drivers.registry.DRIVERS`.

    Raises:
        ValueError: When ``strategy_cls.driver_type`` is empty.
    """
    driver_type = getattr(strategy_cls, "driver_type", "") or ""
    if not driver_type:
        raise ValueError(
            f"Snowpark strategy {strategy_cls!r} declares no driver_type; "
            "set the class attribute before registering."
        )
    SnowparkEngineStager._STRATEGIES[driver_type] = strategy_cls


# ---------------------------------------------------------------------------
# Family → stager map
# ---------------------------------------------------------------------------


#: Map from :class:`EngineFamily` value → stager class. The
#: :meth:`BaseEngine.get_stager` default implementation consults this
#: to instantiate the right stager for its family.
_FAMILY_TO_STAGER: dict[str, type[EngineStager]] = {
    "spark": SparkEngineStager,
    "snowpark": SnowparkEngineStager,
}


def stager_class_for_engine_family(family: str) -> type[EngineStager] | None:
    """Return the stager class for an engine family, or ``None``.

    Web-safe helper (no pyspark, no snowpark). Used by the compat
    check's type-name entry point
    (:func:`ovid2.drivers.engine_compat.assert_engine_type_supports_drivers`)
    to consult :meth:`EngineStager.supports` without instantiating a
    real :class:`BaseEngine`.

    Args:
        family: Engine family value (``"spark"`` / ``"snowpark"``).

    Returns:
        The stager class, or ``None`` when the family is unknown.
    """
    if family == "duckdb" and "duckdb" not in _FAMILY_TO_STAGER:
        # Lazy: the DuckDB stager module imports this one. Import-safe
        # (no ``import duckdb`` at module level).
        from ovid2.core.engines.duckdb_stager import DuckDBEngineStager

        _FAMILY_TO_STAGER["duckdb"] = DuckDBEngineStager
    return _FAMILY_TO_STAGER.get(family)


def stager_class_for_engine_type(engine_type: str) -> type[EngineStager] | None:
    """Return the stager class for an engine type name, or ``None``.

    Web-safe. Combines :func:`ovid2.config.engines.engine_family` with
    :func:`stager_class_for_engine_family` so callers can go from a
    ``--engine`` argv token straight to a stager class without a
    :class:`BaseEngine` instance.

    Args:
        engine_type: Engine type name (``"pyspark"`` /
            ``"databricks_spark"`` / ``"snowpark"``).

    Returns:
        The stager class, or ``None`` when the type is not registered
        in the web-safe family map.
    """
    from ovid2.config.engines import engine_family

    family = engine_family(engine_type)
    if family is None:
        return None
    return stager_class_for_engine_family(family)


__all__ = [
    "EngineStager",
    "EngineStagerUnsupported",
    "SnowparkEngineStager",
    "SnowparkStagingStrategy",
    "SparkEngineStager",
    "register_session_cleanup",
    "register_snowpark_strategy",
    "run_session_cleanups",
    "stager_class_for_engine_family",
    "stager_class_for_engine_type",
]
