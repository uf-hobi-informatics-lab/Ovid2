"""ETL pipeline execution.

Contains :func:`run_pipeline`, which chains lazy transformations end-to-end
on **one code path for every engine** (:func:`_run_pipeline`), and the
helpers that register shared and auxiliary tables as engine temp views.

Key invariants (enforced by contract tests):

- Pipeline code never imports from :mod:`ovid2.display`. Events go through
  :mod:`ovid2.core.events` instead.
- Pipeline code never imports a CDM-specific module. Every CDM concern
  is reached through :func:`ovid2.cdm.registry.resolve_cdm_for_pipeline`
  and the :class:`~ovid2.cdm.handle.CdmHandle` it returns.
- Every step is lazy until the final write. Reference tables are handed
  to ``engine.warm_reference`` (Spark persist, DuckDB scratch Parquet).
- No engine branching: engine-specific steps are engine / stager methods
  (``register_temp_view``, ``warm_reference``, ``read_shared_view_file``,
  ``prepare_workspace``, ``release_checkpoints``,
  ``EngineStager.accepts_legacy_address``). This module imports no
  ``pyspark``.
"""

from __future__ import annotations

import importlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ovid2.cdm.registry import resolve_cdm_for_pipeline
from ovid2.core.events import (
    Event,
    EventBus,
    get_default_bus,
    stage_entered,
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

    One path for every engine (:func:`_run_pipeline`): read and write go
    through ``engine.get_stager()``, so the compute engine (``pyspark`` /
    ``databricks_spark`` / ``duckdb`` / ``snowpark``) is orthogonal to the
    storage backend. The stager reads a Spark-native legacy address
    directly when it can (``accepts_legacy_address``); otherwise the
    address *name* (``input_address_name`` / ``output_address_name``) is
    resolved to a driver.

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
            Resolved to a driver whenever the stager cannot read
            ``input_address`` directly.
        output_address_name: Filename stem of the output address config
            (same rule for the write).
        sf_overrides: CLI-provided Snowflake overrides threaded to
            :func:`driver_from_address`.
        cdm_name: Active CDM stamp threaded to :func:`driver_from_address`.
    """
    bus = event_bus if event_bus is not None else get_default_bus()
    cdm = resolve_cdm_for_pipeline(pipeline)

    bus.emit(
        table_started(
            instance_id=instance_id, pipeline=pipeline, batch_id=batch_id, table=table_name
        )
    )
    started = time.monotonic()

    from ovid2.core.engines.dsl import use_engine

    with use_engine(engine):
        _run_pipeline(
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
            instance_id=instance_id,
            pipeline=pipeline,
            batch_id=batch_id,
            table=table_name,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
    )


def _resolve_endpoint(
    engine: BaseEngine,
    address: Any,
    name: str | None,
    *,
    pipeline: str,
    sf_overrides: dict[str, Any] | None,
    cdm_name: str | None,
    role: str,
) -> Any:
    """Return the data-plane object the stager reads / writes for one endpoint.

    The already-loaded address when there is one (Spark-native drivers:
    ``driver.read`` / ``write`` on it is the historical data plane),
    otherwise the native :class:`~ovid2.drivers.base.Driver` resolved from
    the address *name* — whichever the engine's stager consumes
    (:func:`stager_reads_address`).

    Raises:
        ValueError: When neither an address nor a name is available.
    """
    if address is not None and _is_native_address(address) and stager_reads_address(engine):
        return address
    if not name:
        raise ValueError(
            f"run_pipeline: the {role} has no Spark data plane and no address name; "
            f"pass '{role}_address_name' (the cli/etl.py and scheduler call sites "
            "do this automatically)."
        )
    from ovid2.drivers.registry import driver_from_address

    return driver_from_address(
        name, pipeline=pipeline, sf_overrides=sf_overrides, cdm_name=cdm_name
    )


def stager_reads_address(engine: BaseEngine) -> bool:
    """Whether ``engine``'s stager reads / writes a legacy address directly.

    Asks the stager (``accepts_legacy_address``), never the engine family:
    the Spark stager passes a legacy address straight to its Spark data
    plane; every other stager needs a driver.
    """
    stager_cls = type(engine.get_stager())
    return bool(getattr(stager_cls, "accepts_legacy_address", False))


def _is_native_address(address: Any) -> bool:
    """Whether ``address`` is a legacy address the engine reads through directly.

    Legacy addresses (``BaseAddress``) carry no ``type_name``; they are the
    Spark-native data plane of their driver. Driver objects carry one.
    """
    from ovid2.core.addresses.base import BaseAddress

    return isinstance(address, BaseAddress)


def _run_pipeline(
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
    """The one pipeline path, for every engine and every storage type.

    read (stager) → format → transform chain → register view → reference
    warm-up (``engine.warm_reference``) → write (stager) → release
    checkpoints. Engine-specific behaviour lives behind engine / stager
    methods, never in a branch here:

    * **Read / write** — ``engine.get_stager().materialise`` / ``persist``.
      Spark engines with a Spark-native address read / write through it
      (``SparkEngineStager`` → ``Driver.read`` / ``write``, the historical
      data plane); everything else is resolved to a driver and staged
      natively or through the Arrow bridge.
    * **Views** — ``engine.register_view`` (Spark / DuckDB / Snowpark
      spellings).
    * **Reference warm-up** — ``engine.warm_reference`` (Spark persist +
      count; DuckDB scratch Parquet; no-op on Snowpark).
    * **Workspace** — ``engine.prepare_workspace()`` re-binds Snowpark's
      scratch schema before formatting (no-op elsewhere).

    The per-pipeline read hook (``READ_HOOKS`` / legacy Datavant) runs
    first when declared, on every engine.
    """
    session = engine.get_session()
    stager = engine.get_stager()
    input_endpoint = _resolve_endpoint(
        engine,
        input_address,
        input_address_name,
        pipeline=pipeline,
        sf_overrides=sf_overrides,
        cdm_name=cdm_name,
        role="input",
    )
    output_endpoint = _resolve_endpoint(
        engine,
        output_address,
        output_address_name,
        pipeline=pipeline,
        sf_overrides=sf_overrides,
        cdm_name=cdm_name,
        role="output",
    )

    chain = [t for t in transformations if not isinstance(t, FormatTransformer)]
    stages = _chain_stages(chain)
    step = _StageEmitter(
        bus,
        instance_id=instance_id,
        pipeline=pipeline,
        batch_id=batch_id,
        table=table_name,
        stages=stages,
    )
    step("read")

    # Register auxiliary tables (lazy — no data read yet).
    _register_auxiliary_tables(
        engine, session, stager, input_endpoint, formatter, pipeline, batch_id, bus, instance_id
    )

    # Main source: per-pipeline read hook (Datavant is the shipping
    # example), else the stager.
    df = _read_primary(
        engine=engine,
        session=session,
        stager=stager,
        input_endpoint=input_endpoint,
        input_address=input_address,
        table_name=table_name,
        formatter=formatter,
        pipeline=pipeline,
        batch_id=batch_id,
        bus=bus,
        instance_id=instance_id,
    )

    # Re-bind the engine's writable workspace namespace before formatting.
    #
    # On Snowpark the shared views registered by ``register_shared_tables``
    # (e.g. the OMOP ``concept`` / ``concept_relationship`` views a
    # PCORnet→OMOP formatter resolves via an *unqualified*
    # ``session.table("concept")``) live in the session's current schema —
    # the workspace schema bound by ``prepare_workspace`` at run start.
    # But the previous table's ``stager.persist`` issues ``USE SCHEMA
    # <output_schema>``, which leaves the session pointed at the output
    # schema; the next table's formatter would then fail with ``Object
    # 'CONCEPT' does not exist``. No-op on Spark and DuckDB.
    engine.prepare_workspace()

    # Pipeline formatting.
    step("format")
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

    for transform in chain:
        stage = getattr(transform, "stage_name", None)
        if stage:
            step(stage)
        df = transform.transform(df, context)

    # Reference tables are read back by later tables' cross-table fixers:
    # the engine materialises them once (Spark persist + count, DuckDB
    # scratch Parquet) and the warmed frame is what is registered *and*
    # written, so the plan runs exactly once.
    if table_name in (getattr(cdm, "reference_tables", None) or ()):
        df = engine.warm_reference(session, df, table_name)

    # Register the finished table so cross-table fixers / concept joins can
    # reference it by name.
    engine.register_temp_view(session, table_name, df)

    step("write")
    try:
        stager.persist(df, output_endpoint, pipeline, batch_id, table_name)
    finally:
        # Per-table checkpoints (``engine.checkpoint``) are never reused by
        # the next table; free them now. Reference tables stay warm — later
        # tables' cross-table fixers read them.
        engine.release_checkpoints()


def _chain_stages(chain: list[BaseTransformer]) -> list[str]:
    """Return the ordered stages a table passes through (``read`` … ``write``).

    Only transformers that declare a ``stage_name`` contribute; a stage
    appears once even if two transformers share it.
    """
    stages = ["read", "format"]
    for transform in chain:
        stage = getattr(transform, "stage_name", None)
        if stage and stage not in stages:
            stages.append(stage)
    stages.append("write")
    return stages


class _StageEmitter:
    """Emit one ``stage.progress`` *enter* event per pipeline stage of a table.

    Every stage before ``write`` only builds the lazy plan; ``write`` is
    where the engine executes it. The events carry that distinction
    (``extra.lazy``) so the live views never pretend a lazy step took work.
    Emitting never raises: a broken bus must not fail the table.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        instance_id: str,
        pipeline: str,
        batch_id: str,
        table: str,
        stages: list[str],
    ) -> None:
        self._bus = bus
        self._ids = {"instance_id": instance_id, "pipeline": pipeline, "batch_id": batch_id}
        self._table = table
        self._stages = stages
        self._seen: set[str] = set()

    def __call__(self, stage: str) -> None:
        if stage in self._seen or stage not in self._stages:
            return
        self._seen.add(stage)
        try:
            self._bus.emit(
                stage_entered(
                    table=self._table,
                    stage=stage,
                    index=self._stages.index(stage) + 1,
                    total=len(self._stages),
                    lazy=stage != "write",
                    **self._ids,
                )
            )
        except Exception as exc:  # pragma: no cover - display only
            logger.debug("stage event for %s/%s failed: %s", self._table, stage, exc)


def _read_primary(
    *,
    engine: BaseEngine,
    session: Any,
    stager: Any,
    input_endpoint: Any,
    input_address: Any,
    table_name: str,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
) -> Any:
    """Read the table's primary source: a pipeline read hook, else the stager.

    An absent primary source whose formatter declares it optional yields
    an empty frame (the self-reading adapters honour ``optional: true``
    themselves) instead of failing the table.
    """
    hooked = _read_with_pipeline_hook(
        spark=session,
        input_address=input_address,
        table_name=table_name,
        formatter=formatter,
        pipeline=pipeline,
        batch_id=batch_id,
        bus=bus,
        instance_id=instance_id,
    )
    if hooked is not None:
        return hooked
    try:
        return stager.materialise(
            input_endpoint,
            pipeline,
            batch_id,
            formatter.input_file_name,
            table_name=table_name,
        )
    except FileNotFoundError:
        # The eager read is only a placeholder for self-reading adapters
        # (``source_passthrough`` / ``union_of_specs`` / ``custom``): they
        # re-read their own sources in ``execute_spec`` and already honour
        # ``optional: true`` there. An optional primary source that is
        # absent for this batch (e.g. an ACME delivery with no
        # ``*PREVENTABLE*`` trial extract) degrades to an empty frame so the
        # table produces an empty CDM-shaped output. Non-optional primaries
        # keep the hard failure.
        if not getattr(formatter, "primary_source_optional", False):
            raise
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
        from ovid2.formatters.adapters._sources import _empty_df

        del engine
        return _empty_df(session)


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

    * ``source_kind == 'file'`` — read through
      ``engine.read_shared_view_file`` (Spark / DuckDB: the columnar
      vocabulary cache); engines without a local reader (Snowpark) stage
      the file through the stager with a synthetic ``disc`` driver.
      Missing files raise when ``required=True``, warn otherwise.
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
        engine: Active compute engine (drives every read). ``None`` keeps
            the historical call shape — a bare Spark session — by wrapping it
            in a ``pyspark`` engine from the engine registry.
        pipeline: Pipeline name (threaded to the stager for address views).
        batch_id: Batch identifier (threaded to the stager).
        sf_overrides: Optional Snowflake overrides for address resolution.
    """
    bus = bus if bus is not None else get_default_bus()

    if cdm is None:
        from ovid2.cdm.registry import get_cdm

        cdm = get_cdm("pcornet_7_0")

    if engine is None:
        from ovid2.core.engines.registry import load_engine_class

        # Historical call shape: a bare Spark session and no engine.
        engine = load_engine_class("pyspark")()
        engine._session = spark  # type: ignore[attr-defined]

    registered = engine.registered_view_names(spark)

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

        # The engine reads the local vocabulary file its own way: Spark and
        # DuckDB through the columnar vocabulary cache (multi-GB CONCEPT text
        # parsed once per file version), Snowpark by staging it (``PUT`` +
        # ``COPY INTO``). ``None`` means "no engine-native file reader": fall
        # back to the stager with a synthetic disc driver.
        try:
            df = engine.read_shared_view_file(
                Path(view.source_path), delimiter=view.delimiter, header=view.header, bus=bus
            )
        except Exception as exc:
            message = (
                f"Shared view '{view.view_name}' from file "
                f"'{view.source_path}' failed to materialise: {exc}"
            )
            if view.required:
                raise RuntimeError(message) from exc
            bus.emit(warning_event(instance_id="", message=message))
            continue
        if df is None:
            _register_shared_view_from_local_file(view=view, engine=engine, session=spark, bus=bus)
            continue
        # OMOP shared views (e.g. the Athena CONCEPT.csv export) ship with
        # UPPERCASE headers (CONCEPT_ID, DOMAIN_ID, CONCEPT_CODE, …), but the
        # semantic-layer vocabularies and every ``semantic_join`` /
        # ``omop_concept_join`` spec reference the columns in lowercase
        # (``concept_code``, ``domain_id``, …). Normalise at registration time,
        # matching the legacy ``commonFunctions.spark_read_omop_concept``
        # behaviour so pipeline formatters can rely on lowercase columns
        # regardless of the source file's header casing.
        df = df.toDF(*[column.lower() for column in df.columns])
        engine.register_temp_view(spark, view.view_name, df)


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
        # The address is the exact location, so ``resolve_read_path(parent,
        # basename)`` resolves to ``<parent>/<basename>``.
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
    engine: BaseEngine,
    session: Any,
    stager: Any,
    input_endpoint: Any,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
) -> None:
    """Register each aux table declared by the formatter as a temp view.

    Materialised through the engine's stager and registered with
    ``engine.register_view``; a view already registered on the session is
    left alone.
    """
    if not formatter.auxiliary_tables:
        return
    registered = engine.registered_view_names(session)
    for aux_table in formatter.auxiliary_tables:
        if aux_table.lower() in registered:
            continue
        try:
            aux_df = stager.materialise(
                input_endpoint,
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
        engine.register_temp_view(session, aux_table, aux_df)


def _register_engine_view(session: Any, table_name: str, df: Any) -> None:
    """Register ``df`` as a temp view under ``table_name``, engine-agnostically.

    Kept for the shared-view registration helpers; the per-table path uses
    ``engine.register_view``.
    """
    for method in ("createOrReplaceTempView", "create_or_replace_temp_view"):
        create = getattr(df, method, None)
        if callable(create):
            create(table_name)
            return
    raise AttributeError(
        f"DataFrame of type {type(df).__name__!r} exposes neither "
        "'createOrReplaceTempView' nor 'create_or_replace_temp_view'; "
        f"cannot register table '{table_name}' as a view."
    )


def _read_with_pipeline_hook(
    spark: Any,
    input_address: BaseAddress,
    table_name: str,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
) -> DataFrame | None:
    """Return the main input from a per-pipeline hook, or ``None`` for "no hook".

    A pipeline declares hooks via ``pipeline_settings.READ_HOOKS``, a dict
    keyed by table name whose values are callables with the signature::

        hook(*, spark, input_address, formatter, pipeline, batch_id, bus,
             instance_id) -> DataFrame | None

    A hook returning ``None`` falls back to the stager read
    (:func:`_read_primary`); a hook that raises is reported as an ``error``
    event and also falls back. A hook returning a DataFrame short-circuits
    the read. Hooks receive the engine session, whatever the engine.

    The legacy Datavant integration for ``hash_token`` remains wired as a
    default hook when a pipeline sets ``DATAVANT_KEY`` but does not declare
    ``READ_HOOKS`` (:func:`_legacy_datavant_read`, disc inputs only).
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
            return None
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
    return None


def _legacy_datavant_read(
    spark: Any,
    input_address: BaseAddress,
    formatter: BaseFormatter,
    pipeline: str,
    batch_id: str,
    bus: EventBus,
    instance_id: str,
    table_name: str,
) -> DataFrame | None:
    """Legacy Datavant pre-processing for the PCORnet ``hash_token`` table.

    Retained for backwards compatibility with pipelines that predate the
    ``READ_HOOKS`` mechanism. Pipelines are encouraged to migrate to
    ``READ_HOOKS = {"hash_token": ovid2.utils.datavant.datavant_read_hook}``.

    Engine-neutral: needs a local-path input (the address declares
    ``is_local_path``), reads the transformed tokens through the session's
    portable ``read.csv``, and returns ``None`` — the normal stager read —
    whenever pre-processing is skipped or fails.
    """
    del table_name
    # Only local-path inputs expose the filesystem path Datavant needs.
    if not getattr(input_address, "is_local_path", False):
        bus.emit(
            warning_event(
                instance_id=instance_id,
                message="Datavant pre-processing skipped — input has no local file path.",
                table="hash_token",
            )
        )
        return None

    raw_path = input_address._get_read_path(  # type: ignore[attr-defined]
        pipeline, batch_id, formatter.input_file_name
    )
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
        return None

    return cast(
        "DataFrame",
        spark.read.option("header", "true")
        .option("inferSchema", "false")
        .option("delimiter", ",")
        .csv(working_dir),
    )
