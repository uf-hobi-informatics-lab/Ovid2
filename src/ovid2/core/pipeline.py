"""ETL pipeline execution.

Contains :func:`run_pipeline`, which chains lazy Spark transformations
end-to-end, and the helpers that register shared and auxiliary tables as
Spark temporary views.

Key invariants (enforced by contract tests):

- Pipeline code never imports from :mod:`ovid2.display`. Events go through
  :mod:`ovid2.core.events` instead.
- Pipeline code never imports a CDM-specific module. Every CDM concern
  is reached through :func:`ovid2.cdm.registry.resolve_cdm_for_pipeline`
  and the :class:`~ovid2.cdm.handle.CdmHandle` it returns.
- Every step is lazy. The **only** deliberate materialisation is the
  ``.count()`` used to warm the cache for the CDM's ``reference_tables``
  set — this is gated by an explicit ``# noqa: OVID2001`` marker so
  contract tests can allow it while banning everything else.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ovid2.cdm.registry import resolve_cdm_for_pipeline
from ovid2.core.events import (
    Event,
    EventBus,
    get_default_bus,
    table_completed,
    table_started,
    warning_event,
)
from ovid2.formatters.base import BaseFormatter
from ovid2.transformations.format import FormatTransformer

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.cdm.handle import CdmHandle
    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine
    from ovid2.transformations.base import BaseTransformer

logger = logging.getLogger(__name__)


def run_pipeline(
    engine: BaseEngine,
    input_address: BaseAddress,
    output_address: BaseAddress,
    table_name: str,
    formatter: BaseFormatter,
    transformations: list[BaseTransformer],
    pipeline: str,
    batch_id: str,
    event_bus: EventBus | None = None,
    instance_id: str = "",
    *,
    input_address_name: str | None = None,
    output_address_name: str | None = None,
    sf_overrides: dict[str, Any] | None = None,
    cdm_name: str | None = None,
) -> None:
    """Execute the ETL pipeline for a single CDM table.

    All DataFrame operations (read, format, transform) are lazy — they
    build the execution plan. Only the final write (through the engine's
    :class:`~ovid2.core.engines.stager.EngineStager`) triggers actual
    computation.

    Engine portability
    ------------------

    The read and write legs route through ``engine.get_stager()`` so the
    choice of compute engine (``pyspark`` / ``databricks_spark`` /
    ``snowpark``) is orthogonal to the storage backend. Two behaviours:

    * **Spark-family** engines (``engine.session_is_spark`` is ``True``):
      the path is byte-identical to the pre-portability world — the
      :class:`~ovid2.core.engines.stager.SparkEngineStager` passes
      straight through to :meth:`Driver.read` / :meth:`Driver.write`,
      which post-D3 is the same code every pipeline has always run.
      Auxiliary-table registration, the per-pipeline read hook (Datavant),
      and the reference-table ``persist().count()`` warm-up all run
      exactly as before.

    * **Non-Spark** engines (Snowpark): the read/write legs go through
      the family's stager (``materialise`` / ``persist``); the Spark-only
      warm-up + Datavant + filesystem-glob aux-table steps are skipped in
      favour of engine-appropriate equivalents. Requires the address
      *names* (``input_address_name`` / ``output_address_name``) so the
      driver can be resolved via :func:`driver_from_address`.

    Args:
        engine: Compute engine (provides a session + a stager).
        input_address: Where to read raw data from (Spark path + hooks).
        output_address: Where to write the finished table (Spark path).
        table_name: CDM table being processed.
        formatter: The pipeline formatter (from YAML spec or custom class).
        transformations: Ordered list of transformers to apply.
        pipeline: Pipeline name.
        batch_id: Batch identifier.
        event_bus: Optional event bus. Defaults to the process-wide bus.
        instance_id: Owning :class:`OvidInstance` id. Attached to every
            emitted event.
        input_address_name: Filename stem of the input address config.
            Required for the non-Spark (Snowpark) read path so the driver
            can be resolved. On the Spark path it is optional (the
            already-loaded ``input_address`` is used directly).
        output_address_name: Filename stem of the output address config.
            Required for the non-Spark write path.
        sf_overrides: CLI-provided Snowflake overrides threaded to
            :func:`driver_from_address` on the non-Spark path.
        cdm_name: Active CDM stamp threaded to :func:`driver_from_address`.
    """
    bus = event_bus if event_bus is not None else get_default_bus()
    cdm = resolve_cdm_for_pipeline(pipeline)

    bus.emit(
        table_started(
            instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table_name
        )
    )

    if engine.session_is_spark:
        _run_pipeline_spark(
            engine=engine,
            input_address=input_address,
            output_address=output_address,
            table_name=table_name,
            formatter=formatter,
            transformations=transformations,
            pipeline=pipeline,
            batch_id=batch_id,
            bus=bus,
            instance_id=instance_id,
            cdm=cdm,
        )
    else:
        _run_pipeline_staged(
            engine=engine,
            input_address=input_address,
            output_address=output_address,
            table_name=table_name,
            formatter=formatter,
            transformations=transformations,
            pipeline=pipeline,
            batch_id=batch_id,
            bus=bus,
            instance_id=instance_id,
            cdm=cdm,
            input_address_name=input_address_name,
            output_address_name=output_address_name,
            sf_overrides=sf_overrides,
            cdm_name=cdm_name,
        )

    bus.emit(
        table_completed(
            instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table_name
        )
    )


def _run_pipeline_spark(
    *,
    engine: BaseEngine,
    input_address: BaseAddress,
    output_address: BaseAddress,
    table_name: str,
    formatter: BaseFormatter,
    transformations: list[BaseTransformer],
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
    cdm: CdmHandle,
) -> None:
    """Spark-family pipeline leg — byte-identical to the pre-portability path.

    Reads route through the engine's :class:`SparkEngineStager`, which is
    a pure pass-through to :meth:`Driver.read` / :meth:`Driver.write`; the
    resulting DataFrames are the same objects a direct
    ``input_address.read`` / ``output_address.write`` produced before D3.
    Auxiliary tables, the per-pipeline read hook, and the reference-table
    warm-up all run exactly as they always have.
    """
    from pyspark import StorageLevel

    # Spark-family leg: the engine session *is* a ``SparkSession``.
    spark = cast("SparkSession", engine.get_session())

    # Register auxiliary tables (lazy — no data read yet).
    _register_auxiliary_tables(
        spark, input_address, formatter, pipeline, batch_id, bus, instance_id
    )

    # Per-pipeline / per-table read hook (Datavant is the shipping example).
    df = _read_with_pipeline_hook(
        spark=spark,
        input_address=input_address,
        table_name=table_name,
        formatter=formatter,
        pipeline=pipeline,
        batch_id=batch_id,
        bus=bus,
        instance_id=instance_id,
    )

    # Pipeline formatting.
    df = formatter.format(df)

    context: dict[str, Any] = {
        "table_name": table_name,
        "pipeline": pipeline,
        "input_address": input_address,
        "spark": spark,
        "engine": engine,
        "batch_id": batch_id,
        "event_bus": bus,
        "instance_id": instance_id,
        "cdm": cdm,
    }

    for transform in transformations:
        if isinstance(transform, FormatTransformer):
            continue
        df = transform.transform(df, context)

    # Register the finished table so cross-table fixers can reference it.
    df.createOrReplaceTempView(table_name)

    if table_name in cdm.reference_tables:
        cached_df = spark.table(table_name).persist(StorageLevel.MEMORY_AND_DISK)
        # Materialise now so downstream ``spark.table(name)`` calls hit
        # the cache instead of recomputing the plan. This is the ONE
        # deliberate action in the pipeline.
        cached_df.count()  # noqa: OVID2001 deliberate reference-table warm-up
        cached_df.createOrReplaceTempView(table_name)
        df = cached_df

    output_address.write(df, spark, pipeline, batch_id, table_name)


def _run_pipeline_staged(
    *,
    engine: BaseEngine,
    input_address: BaseAddress,
    output_address: BaseAddress,
    table_name: str,
    formatter: BaseFormatter,
    transformations: list[BaseTransformer],
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
    cdm: CdmHandle,
    input_address_name: str | None,
    output_address_name: str | None,
    sf_overrides: dict[str, Any] | None,
    cdm_name: str | None,
) -> None:
    """Non-Spark (Snowpark) pipeline leg — reads/writes through the stager.

    The engine's session is not Spark-compatible, so the filesystem-glob
    auxiliary-table registration, the Datavant read hook, and the
    ``persist().count()`` reference-table warm-up (all of which call
    ``spark.read`` / ``StorageLevel``) are skipped. The source is
    materialised into an engine-native DataFrame through the stager; the
    formatted result is persisted the same way.

    Requires the address *names* so :func:`driver_from_address` can resolve
    each endpoint to a native :class:`Driver` the stager can bridge.
    """
    from ovid2.drivers.registry import driver_from_address

    if not input_address_name or not output_address_name:
        raise ValueError(
            f"run_pipeline: engine '{engine.type_name}' is not Spark-"
            "compatible, so it reads/writes through the stager and needs "
            "the address names. Pass 'input_address_name' and "
            "'output_address_name' (the cli/etl.py and scheduler call "
            "sites do this automatically)."
        )

    session = engine.get_session()
    stager = engine.get_stager()

    input_driver = driver_from_address(
        input_address_name,
        pipeline=pipeline,
        sf_overrides=sf_overrides,
        cdm_name=cdm_name,
    )
    output_driver = driver_from_address(
        output_address_name,
        pipeline=pipeline,
        sf_overrides=sf_overrides,
        cdm_name=cdm_name,
    )

    # Register auxiliary tables through the stager (engine-native views).
    _register_auxiliary_tables_staged(
        engine,
        session,
        stager,
        input_driver,
        formatter,
        pipeline,
        batch_id,
        bus,
        instance_id,
    )

    # Materialise the main source into an engine-native DataFrame.
    df = stager.materialise(
        input_driver,
        pipeline,
        batch_id,
        formatter.input_file_name,
        table_name=table_name,
    )

    # Re-bind the engine's writable workspace namespace before formatting.
    #
    # On Snowpark the shared views registered by ``register_shared_tables``
    # (e.g. the OMOP ``concept`` / ``concept_relationship`` views a
    # PCORnet→OMOP formatter resolves via an *unqualified*
    # ``session.table("concept")``) live in the session's current schema —
    # the workspace schema bound by ``prepare_workspace`` at run start.
    # But the previous table's ``stager.persist`` issues ``USE SCHEMA
    # <output_schema>`` (see ``_NativeSnowparkStrategy.persist``), which
    # leaves the session pointed at the output schema. The next table's
    # ``formatter.format`` then resolves ``session.table("concept")``
    # against that output schema, where the temp view does not exist, and
    # Snowflake raises ``Object 'CONCEPT' does not exist or not
    # authorized``. Re-binding here restores the workspace schema so
    # unqualified shared-view references resolve for every table. No-op on
    # Spark (session-scoped catalogue; ``prepare_workspace`` returns
    # immediately).
    engine.prepare_workspace()

    # Pipeline formatting.
    df = formatter.format(df)

    context: dict[str, Any] = {
        "table_name": table_name,
        "pipeline": pipeline,
        "input_address": input_address,
        "spark": session,
        "engine": engine,
        "batch_id": batch_id,
        "event_bus": bus,
        "instance_id": instance_id,
        "cdm": cdm,
    }

    for transform in transformations:
        if isinstance(transform, FormatTransformer):
            continue
        df = transform.transform(df, context)

    # Register the finished table so cross-table fixers / concept joins can
    # reference it by name. Snowpark exposes ``create_or_replace_temp_view``.
    _register_engine_view(session, table_name, df)

    stager.persist(df, output_driver, pipeline, batch_id, table_name)


def register_shared_tables(
    spark: SparkSession,
    bus: EventBus | None = None,
    *,
    cdm: CdmHandle | None = None,
    engine: BaseEngine | None = None,
    pipeline: str = "",
    batch_id: str = "",
    sf_overrides: dict[str, Any] | None = None,
) -> None:
    """Register the CDM's shared views (e.g. OMOP ``concept`` for PCORnet).

    Iterates over :attr:`CdmHandle.shared_views` and registers each source
    as an engine temp view. Two source shapes are handled:

    * ``source_kind == 'file'`` — read directly from the filesystem.
      Under a Spark-family session this uses ``spark.read.csv``
      (byte-identical to the pre-portability behaviour). Under a
      non-Spark engine (Snowpark) the file is routed through the
      engine's stager via a synthetic ``disc`` driver, so the CSV is
      staged into the compute layer engine-natively. Missing files
      raise when ``required=True``, warn otherwise.
    * ``source_kind == 'address'`` — resolve the named address to a
      :class:`Driver` and materialise it through the active engine's
      stager, then register the result as a view. Works under both Spark
      and Snowpark, so multi-GB vocabularies can live in a database the
      JVM-less Snowpark session can read natively.

    Args:
        spark: Active engine session (a real Spark session on the Spark
            path; the engine session otherwise). Named ``spark`` for
            backwards compatibility.
        bus: Optional event bus for warnings.
        cdm: CDM handle whose ``shared_views`` should be registered.
            Defaults to ``pcornet_7_0`` for backwards compatibility with
            call sites that predate P10.
        engine: Active compute engine. Required to register any
            ``source_kind == 'address'`` view (it drives the stager).
            When ``None``, only file-sourced views are registered on the
            assumption that ``spark`` is a real Spark session.
        pipeline: Pipeline name (threaded to the stager for address views).
        batch_id: Batch identifier (threaded to the stager).
        sf_overrides: Optional Snowflake overrides for address resolution.
    """
    bus = bus if bus is not None else get_default_bus()

    if cdm is None:
        from ovid2.cdm.registry import get_cdm

        cdm = get_cdm("pcornet_7_0")

    session_is_spark = engine is None or getattr(engine, "session_is_spark", True)

    registered: set[str] = set()
    if session_is_spark:
        registered = {t.name.lower() for t in spark.catalog.listTables() if t.isTemporary}

    for view in cdm.shared_views:
        if view.view_name.lower() in registered:
            continue
        if view.source_kind == "address":
            _register_shared_view_from_address(
                view=view,
                engine=engine,
                session=spark,
                bus=bus,
                pipeline=pipeline,
                batch_id=batch_id,
                cdm_name=cdm.name,
                sf_overrides=sf_overrides,
            )
            continue

        # File-sourced view.
        if view.source_path is None or not view.source_path.exists():
            location = view.source_path if view.source_path is not None else "<unset>"
            message = f"Shared view '{view.view_name}' missing: {location}"
            if view.required:
                raise FileNotFoundError(message)
            bus.emit(warning_event(instance_id="", message=message))
            continue

        if not session_is_spark:
            # Non-Spark engines (Snowpark) have no ``spark.read.csv``.
            # Route the local file through the engine's stager using a
            # synthetic disc driver rooted at the file's parent dir, so
            # the CSV is staged into the compute layer engine-natively
            # (e.g. Snowflake ``PUT`` + ``COPY INTO``).
            _register_shared_view_from_local_file(
                view=view,
                engine=engine,
                session=spark,
                bus=bus,
            )
            continue

        # The classic Spark path.
        df = spark.read.csv(
            str(view.source_path),
            header=view.header,
            sep=view.delimiter,
            inferSchema=False,
        )
        # OMOP shared views (e.g. the Athena CONCEPT.csv export) ship with
        # UPPERCASE headers (CONCEPT_ID, DOMAIN_ID, CONCEPT_CODE, …), but the
        # semantic-layer vocabularies and every ``semantic_join`` /
        # ``omop_concept_join`` spec reference the columns in lowercase
        # (``concept_code``, ``domain_id``, …). Normalise at registration time,
        # matching the legacy ``commonFunctions.spark_read_omop_concept``
        # behaviour so pipeline formatters can rely on lowercase columns
        # regardless of the source file's header casing.
        df = df.toDF(*[column.lower() for column in df.columns])
        df.createOrReplaceTempView(view.view_name)


def _register_shared_view_from_address(
    *,
    view: Any,
    engine: BaseEngine | None,
    session: Any,
    bus: EventBus,
    pipeline: str,
    batch_id: str,
    cdm_name: str,
    sf_overrides: dict[str, Any] | None,
) -> None:
    """Register an ``source_kind == 'address'`` shared view via the stager.

    Resolves ``view.address`` to a native :class:`Driver`, materialises it
    through the engine's stager, lowercases the columns (so the same
    downstream ``semantic_join`` / ``omop_concept_join`` specs work
    regardless of source-column casing), and registers the result as a
    temp view. Missing / unreachable sources raise when ``required``,
    warn otherwise.
    """
    if engine is None:
        message = (
            f"Shared view '{view.view_name}' is address-sourced "
            f"('{view.address}') but no engine was supplied to "
            "register_shared_tables; cannot resolve without a stager."
        )
        if view.required:
            raise RuntimeError(message)
        bus.emit(warning_event(instance_id="", message=message))
        return

    from ovid2.drivers.registry import driver_from_address

    try:
        driver = driver_from_address(
            str(view.address),
            pipeline=pipeline,
            sf_overrides=sf_overrides,
            cdm_name=cdm_name,
        )
        stager = engine.get_stager()
        df = stager.materialise(
            driver,
            pipeline,
            batch_id,
            str(view.address),
            view.delimiter,
            table_name=view.view_name,
        )
    except Exception as exc:
        message = (
            f"Shared view '{view.view_name}' from address "
            f"'{view.address}' failed to materialise: {exc}"
        )
        if view.required:
            raise RuntimeError(message) from exc
        bus.emit(warning_event(instance_id="", message=message))
        return

    # Lowercase columns for casing-agnostic downstream joins, matching the
    # file path's normalisation. Both engines expose ``toDF``/``columns``.
    columns = list(getattr(df, "columns", []))
    if columns and hasattr(df, "toDF"):
        df = df.toDF(*[str(c).lower() for c in columns])
    _register_engine_view(session, view.view_name, df)


def _register_shared_view_from_local_file(
    *,
    view: Any,
    engine: BaseEngine | None,
    session: Any,
    bus: EventBus,
) -> None:
    """Register a ``source_kind == 'file'`` view on a non-Spark engine.

    Non-Spark engine sessions (Snowpark) expose no ``spark.read.csv``,
    so a local CSV shared view (e.g. the OMOP ``CONCEPT.csv`` export)
    cannot be read the classic way. Instead the file is routed through
    the engine's stager via a synthetic ``disc`` driver rooted at the
    file's parent directory, so the CSV is staged into the compute
    layer engine-natively (Snowflake ``PUT`` + ``COPY INTO`` for
    Snowpark). The result is registered as a temp view under
    ``view.view_name``. Missing / unreadable sources raise when
    ``required``, warn otherwise.
    """
    if engine is None:
        message = (
            f"Shared view '{view.view_name}' is file-sourced "
            f"('{view.source_path}') but the session is not Spark and no "
            "engine was supplied to register_shared_tables; cannot "
            "stage without an engine."
        )
        if view.required:
            raise RuntimeError(message)
        bus.emit(warning_event(instance_id="", message=message))
        return

    from ovid2.drivers.disc.driver import DiscDriver

    source_path = view.source_path
    parent = str(source_path.parent)
    basename = source_path.name

    try:
        # Synthetic disc driver rooted at the file's parent directory.
        # ``resolve_read_path(parent, "", "", basename)`` resolves to
        # ``<parent>/<basename>`` (empty pipeline/batch collapse away),
        # so the stager finds the exact file.
        driver = DiscDriver(
            args={"host_path": parent, "container_path": parent},
            secrets={},
            location={"format": "csv"},
        )
        stager = engine.get_stager()
        df = stager.materialise(
            driver,
            "",
            "",
            basename,
            view.delimiter,
            table_name=view.view_name,
        )
    except Exception as exc:
        message = (
            f"Shared view '{view.view_name}' from file "
            f"'{view.source_path}' failed to materialise: {exc}"
        )
        if view.required:
            raise RuntimeError(message) from exc
        bus.emit(warning_event(instance_id="", message=message))
        return

    # Column-casing note: the Spark path lowercases the OMOP-export
    # headers so downstream ``col("concept_code")`` matches (Spark
    # identifiers are case-insensitive either way). Under Snowpark the
    # disc staging strategy creates the transient table with
    # **uppercased** quoted column identifiers (see
    # :meth:`SnowparkDiscStrategy.materialise`), so an unquoted
    # ``col("concept_code")`` — which Snowpark folds to
    # ``CONCEPT_CODE`` — resolves against the ``"CONCEPT_CODE"`` column
    # both on a base-table read and after a ``groupBy().agg()``. The
    # OMOP vocabulary exports ship *lowercase* headers
    # (``concept_id_1``, ``domain_id`` …); without the uppercase
    # normalisation the columns would be quoted-lowercase and
    # ``concept_relationship``'s post-aggregation ``select`` would fail
    # with ``invalid identifier 'CONCEPT_ID_1'``. So no rename is done
    # here — the strategy already produced case-consistent columns.
    _register_engine_view(session, view.view_name, df)


def _register_auxiliary_tables(
    spark: SparkSession,
    input_address: BaseAddress,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
) -> None:
    """Register each aux table declared by the formatter as a temp view."""
    if not formatter.auxiliary_tables:
        return
    registered = {t.name.lower() for t in spark.catalog.listTables() if t.isTemporary}
    for aux_table in formatter.auxiliary_tables:
        if aux_table.lower() in registered:
            continue
        try:
            aux_df = input_address.read(
                spark,
                pipeline,
                batch_id,
                f"*{aux_table}*",
                table_name=aux_table,
            )
        except Exception as exc:
            bus.emit(
                warning_event(
                    instance_id=instance_id,
                    message=f"Auxiliary table '{aux_table}' not loaded: {exc}",
                )
            )
            continue
        aux_df.createOrReplaceTempView(aux_table)


def _register_engine_view(session: Any, table_name: str, df: Any) -> None:
    """Register ``df`` as a temp view under ``table_name``, engine-agnostically.

    Spark exposes ``createOrReplaceTempView``; Snowpark's DataFrame
    exposes ``create_or_replace_temp_view``. Prefer the Spark spelling
    (it exists on Databricks-Connect sessions too) and fall back to the
    Snowpark spelling.
    """
    create_spark = getattr(df, "createOrReplaceTempView", None)
    if callable(create_spark):
        create_spark(table_name)
        return
    create_snowpark = getattr(df, "create_or_replace_temp_view", None)
    if callable(create_snowpark):
        create_snowpark(table_name)
        return
    raise AttributeError(
        f"DataFrame of type {type(df).__name__!r} exposes neither "
        "'createOrReplaceTempView' nor 'create_or_replace_temp_view'; "
        f"cannot register table '{table_name}' as a view."
    )


def _register_auxiliary_tables_staged(
    engine: BaseEngine,
    session: Any,
    stager: Any,
    input_driver: Any,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
) -> None:
    """Register each aux table via the stager as an engine-native view.

    The non-Spark analogue of :func:`_register_auxiliary_tables`: instead
    of ``input_address.read`` + ``createOrReplaceTempView`` on a Spark
    session, the source is materialised through the engine's stager and
    registered with the engine-agnostic view helper.
    """
    del engine  # session is enough; kept for signature symmetry
    if not formatter.auxiliary_tables:
        return
    for aux_table in formatter.auxiliary_tables:
        try:
            aux_df = stager.materialise(
                input_driver,
                pipeline,
                batch_id,
                f"*{aux_table}*",
                table_name=aux_table,
            )
        except Exception as exc:
            bus.emit(
                warning_event(
                    instance_id=instance_id,
                    message=f"Auxiliary table '{aux_table}' not loaded: {exc}",
                )
            )
            continue
        _register_engine_view(session, aux_table, aux_df)


def _read_with_pipeline_hook(
    spark: SparkSession,
    input_address: BaseAddress,
    table_name: str,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
) -> DataFrame:
    """Read the main input, optionally routing through a per-pipeline hook.

    A pipeline declares hooks via ``pipeline_settings.READ_HOOKS``, a dict
    keyed by table name whose values are callables with the signature::

        hook(*, spark, input_address, formatter, pipeline, batch_id, bus,
             instance_id) -> DataFrame | None

    A hook returning ``None`` (or raising and being caught by the hook
    itself) falls back to ``input_address.read(...)``. A hook returning a
    DataFrame short-circuits the read entirely.

    The legacy Datavant integration for ``hash_token`` remains wired as a
    default hook when a pipeline sets ``DATAVANT_KEY`` but does not declare
    ``READ_HOOKS``. This preserves backwards compatibility with every
    pipeline that shipped before P10.
    """
    try:
        pipeline_settings = importlib.import_module(f"ovid2_pipelines.{pipeline}.pipeline_settings")
    except ModuleNotFoundError:
        pipeline_settings = None

    read_hooks: dict[str, Any] = {}
    if pipeline_settings is not None:
        read_hooks = getattr(pipeline_settings, "READ_HOOKS", {}) or {}

    hook = read_hooks.get(table_name)
    if hook is not None:
        try:
            result = hook(
                spark=spark,
                input_address=input_address,
                formatter=formatter,
                pipeline=pipeline,
                batch_id=batch_id,
                bus=bus,
                instance_id=instance_id,
            )
        except Exception as exc:
            bus.emit(
                Event(
                    kind="error",
                    payload={
                        "message": f"Pipeline read hook for '{table_name}' failed: {exc}",
                        "table": table_name,
                    },
                    instance_id=instance_id,
                )
            )
            return input_address.read(
                spark,
                pipeline,
                batch_id,
                formatter.input_file_name,
                table_name=table_name,
            )
        if result is not None:
            return cast("DataFrame", result)

    # Backwards-compat: legacy Datavant hash_token integration.
    datavant_key = getattr(pipeline_settings, "DATAVANT_KEY", None) if pipeline_settings else None
    if table_name == "hash_token" and datavant_key:
        return _legacy_datavant_read(
            spark=spark,
            input_address=input_address,
            formatter=formatter,
            pipeline=pipeline,
            batch_id=batch_id,
            bus=bus,
            instance_id=instance_id,
            table_name=table_name,
        )

    try:
        return input_address.read(
            spark,
            pipeline,
            batch_id,
            formatter.input_file_name,
            table_name=table_name,
        )
    except FileNotFoundError:
        # The eager read is only a placeholder for self-reading adapters
        # (``source_passthrough`` / ``union_of_specs`` / ``custom``): they
        # re-read their own sources in ``execute_spec`` and already honour
        # ``optional: true`` there. When the table's primary source is
        # optional and simply absent for this batch (e.g. an ACME delivery
        # with no ``*PREVENTABLE*`` trial extract), degrade to an empty
        # DataFrame so the table produces an empty CDM-shaped output
        # instead of failing the whole run. Non-optional primaries keep
        # the historical hard-fail behaviour.
        if getattr(formatter, "primary_source_optional", False):
            from pyspark.sql.types import StructType

            bus.emit(
                warning_event(
                    instance_id=instance_id,
                    message=(
                        f"Optional primary source for table '{table_name}' "
                        f"(glob '{formatter.input_file_name}') not found for "
                        f"batch '{batch_id}'; producing an empty table."
                    ),
                    table=table_name,
                )
            )
            return spark.createDataFrame([], schema=StructType([]))
        raise


def _legacy_datavant_read(
    spark: SparkSession,
    input_address: BaseAddress,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
    table_name: str,
) -> DataFrame:
    """Legacy Datavant pre-processing for the PCORnet ``hash_token`` table.

    Retained for backwards compatibility with pipelines that predate the
    ``READ_HOOKS`` mechanism. Pipelines are encouraged to migrate to
    ``READ_HOOKS = {"hash_token": ovid2.utils.datavant.datavant_read_hook}``.
    """
    # Only disc addresses expose the filesystem path Datavant needs.
    from ovid2.drivers.disc import DiscAddress

    if not isinstance(input_address, DiscAddress):
        bus.emit(
            warning_event(
                instance_id=instance_id,
                message="Datavant pre-processing skipped — non-disc input address.",
                table="hash_token",
            )
        )
        return input_address.read(
            spark,
            pipeline,
            batch_id,
            formatter.input_file_name,
            table_name=table_name,
        )

    raw_path = input_address._get_read_path(pipeline, batch_id, formatter.input_file_name)
    input_dir = raw_path if Path(raw_path).is_dir() else str(Path(raw_path).parent)
    file_glob = "*" if Path(raw_path).is_dir() else Path(raw_path).name

    try:
        from ovid2.utils.datavant import transfer_tokens_to_pcornet

        working_dir = transfer_tokens_to_pcornet(
            pipeline=pipeline,
            batch_id=batch_id,
            input_dir=input_dir,
            file_glob=file_glob,
        )
    except Exception as exc:
        bus.emit(
            Event(
                kind="error",
                payload={
                    "message": f"Datavant pre-processing failed: {exc}",
                    "table": "hash_token",
                },
                instance_id=instance_id,
            )
        )
        return cast(
            "DataFrame",
            input_address.read(
                spark,
                pipeline,
                batch_id,
                formatter.input_file_name,
                table_name=table_name,
            ),
        )

    return (
        spark.read.option("header", "true")
        .option("inferSchema", "false")
        .option("delimiter", ",")
        .csv(working_dir)
    )
