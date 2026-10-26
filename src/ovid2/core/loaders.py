"""Component loading and resolution utilities.

Dynamically loads engines, addresses, formatters, and transformers by name
using :mod:`importlib` and naming conventions.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ovid2.transformations.base import BaseTransformer

if TYPE_CHECKING:
    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine
    from ovid2.formatters.base import BaseFormatter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table / transformation resolution
# ---------------------------------------------------------------------------


def get_all_tables(pipeline: str) -> list[str]:
    """Return the full table list for ``pipeline`` based on its CDM.

    Resolves the pipeline's CDM via :func:`ovid2.cdm.registry.resolve_cdm_for_pipeline`
    (which honours ``pipeline_settings.cdm`` first, then falls back to the
    legacy ``pipeline_settings.mappers_type``). The CDM's insertion-ordered
    ``composite_keys`` dict determines processing order.

    Args:
        pipeline: Pipeline name (e.g. ``'demo'``).

    Returns:
        Ordered list of table names.

    Raises:
        ValueError: If the pipeline has no settings or the configured CDM
            is unknown.
    """
    from ovid2.cdm.registry import resolve_cdm_for_pipeline

    try:
        handle = resolve_cdm_for_pipeline(pipeline)
    except ValueError:
        # Re-raise with pipeline context.
        raise

    tables = handle.tables()
    if not tables:
        raise ValueError(
            f"CDM '{handle.name}' registered for pipeline '{pipeline}' has an empty table list. "
            f"Populate its composite_keys before running '--tables all'."
        )
    return tables


def get_all_transformations() -> list[str]:
    """Return the canonical ETL transformation chain.

    Order:

    1. ``format``       — pipeline-specific reshaping.
    2. ``deduplicate``  — before Map so the map UDF sees fewer rows.
    3. ``map``          — value dictionaries + ID encryption.
    4. ``fix``          — cross-table and single-column repairs.
    """
    return ["format", "deduplicate", "map", "fix"]


def resolve_transformations(transformations: list[str]) -> list[str]:
    """Return ``transformations`` sorted into the canonical order.

    When ``transformations == ["all"]``, expands to :func:`get_all_transformations`.
    If any name is unknown (e.g. DQ-only stages ``mapping_gap``), the input
    is returned as-is so :func:`load_transformations` can raise a clearer
    error.
    """
    canonical = get_all_transformations()
    if transformations == ["all"]:
        return canonical
    canonical_index = {name: i for i, name in enumerate(canonical)}
    if any(t not in canonical_index for t in transformations):
        return list(transformations)
    return sorted(transformations, key=canonical_index.__getitem__)


def resolve_tables(tables: list[str], pipeline: str) -> list[str]:
    """Return the concrete table list for a run.

    Two modes:

    * **Explicit list** — the caller has already named the tables they want.
      Returned unchanged. If a table has no formatter it will fail loudly at
      :func:`load_formatter` time; that surface exists on purpose so typos
      or missing migrations are impossible to miss.
    * **``['all']``** — expands to :func:`get_all_tables` for the pipeline's
      CDM, then *filters* out tables that have neither a declarative
      formatter spec (YAML / ``SPEC = ...``) nor a hand-written
      ``formatters_custom.<table>_formatter`` module. This makes
      ``--tables all`` mean *all tables actually configured for this
      pipeline* — a partial migration (P8) no longer stops the run at the
      first unmigrated table.

    A ``logger.warning`` is emitted whenever the ``all`` expansion drops
    tables so operators still learn which ones were skipped.
    """
    if tables != ["all"]:
        return list(tables)

    all_tables = get_all_tables(pipeline)
    kept: list[str] = []
    skipped: list[str] = []
    for table in all_tables:
        if has_formatter(pipeline, table):
            kept.append(table)
        else:
            skipped.append(table)

    if skipped:
        logger.warning(
            "Pipeline '%s': skipping %d table(s) with no formatter spec: %s. "
            "Add a YAML at pipelines/ovid2_pipelines/%s/formatters/<table>.yaml "
            "or a class at ovid2_pipelines.%s.formatters_custom.<table>_formatter "
            "to include them.",
            pipeline,
            len(skipped),
            ", ".join(skipped),
            pipeline,
            pipeline,
        )

    return kept


def has_formatter(pipeline: str, table: str) -> bool:
    """Return ``True`` iff a formatter can be resolved for ``(pipeline, table)``.

    Mirrors the resolution order of :func:`load_formatter` but performs no
    Spark I/O and no YAML parsing — only filesystem / ``importlib``
    existence checks. Safe to call from ``resolve_tables`` without touching
    an engine session.

    Resolution probes, in order:

    1. Python-factory spec: module ``ovid2_pipelines.<pipeline>.formatters.<table>``
       is importable (its body may still define ``SPEC = spec(...)`` — that
       is validated at :func:`load_formatter` time; existence is enough here).
    2. YAML spec: file ``<pipeline-pkg-path>/formatters/<table>.yaml`` exists
       on any of the pipeline package's ``__path__`` entries.
    3. Hand-written class: module
       ``ovid2_pipelines.<pipeline>.formatters_custom.<table>_formatter``
       is importable.

    Args:
        pipeline: Pipeline name.
        table: CDM table name.

    Returns:
        ``True`` if any of the three probes succeed, ``False`` otherwise.
        Also returns ``False`` if the pipeline package itself is missing.
    """
    # 1) Python-factory spec — an importable module is a strong enough signal.
    module_name = f"ovid2_pipelines.{pipeline}.formatters.{table}"
    if _module_exists(module_name):
        return True

    # 2) YAML spec on disk. Locate the pipeline package's search dirs the same
    #    way ovid2.formatters.spec.find_and_load does.
    try:
        pipeline_pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return False
    for path_str in getattr(pipeline_pkg, "__path__", []):
        candidate = Path(path_str) / "formatters" / f"{table}.yaml"
        if candidate.is_file():
            return True

    # 3) Hand-written class fallback.
    custom_module = f"ovid2_pipelines.{pipeline}.formatters_custom.{table}_formatter"
    return bool(_module_exists(custom_module))


def _module_exists(dotted_name: str) -> bool:
    """Return True iff ``dotted_name`` can be imported without executing it.

    :func:`importlib.util.find_spec` raises :class:`ModuleNotFoundError` when
    an intermediate parent package is absent, which is exactly the case we
    want to treat as ``False`` here (e.g. probing
    ``ovid2_pipelines.demo.formatters_custom.demographic_formatter`` when the
    ``formatters_custom`` sub-package doesn't exist).
    """
    try:
        return importlib.util.find_spec(dotted_name) is not None
    except ModuleNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Component loading
# ---------------------------------------------------------------------------


def load_engine(engine_name: str) -> BaseEngine:
    """Load a compute engine by name.

    Resolution order:

    1. **On-disk config** — if a JSON file named ``<engine_name>.json``
       exists on the :func:`ovid2.config.engines._search_paths` chain,
       load it, resolve secrets, and instantiate the registered class
       with ``args`` and ``secrets``.
    2. **Bare type** — if ``engine_name`` matches a key in
       :data:`ovid2.core.engines.registry.ENGINE_TYPE_MAP`, instantiate
       the class with no args. This is the back-compat path so
       existing routes and settings that reference ``"pyspark"``
       continue to work with zero disk changes.
    3. Otherwise raise :class:`ValueError`.

    Args:
        engine_name: Either a config file stem (e.g. ``pyspark_prod``)
            or a bare type name (``pyspark``, ``databricks_spark``,
            ``snowpark``).

    Returns:
        A configured :class:`BaseEngine` instance.

    Raises:
        ValueError: If neither a config file nor a bare type matches.
    """
    from ovid2.config import engines as engines_config
    from ovid2.core.engines.registry import ENGINE_TYPE_MAP, load_engine_class

    # 1) On-disk config path.
    config_path = engines_config.locate_engine(engine_name)
    if config_path is not None:
        config = engines_config.load_engine_config(engine_name)
        cls = load_engine_class(str(config["type"]))
        return cls(
            args=dict(config.get("args") or {}),
            secrets=engines_config.resolve_secrets(config),
        )

    # 2) Bare-type back-compat path.
    if engine_name in ENGINE_TYPE_MAP:
        cls = load_engine_class(engine_name)
        return cls()

    raise ValueError(
        f"Unknown engine '{engine_name}'. "
        f"Known types: {sorted(ENGINE_TYPE_MAP)}; "
        f"known named configs: {engines_config.list_available_engines()}."
    )


def load_address(
    address_name: str,
    sf_overrides: dict[str, str] | None = None,
    pipeline: str | None = None,
    cdm_name: str | None = None,
) -> BaseAddress:
    """Load an address by name from a JSON config file.

    See :func:`ovid2.config.addresses.load_address` for full documentation
    of the four-tier resolution and lookup paths.

    Phase C1 adds the ``cdm_name`` keyword — passed as the ``cdm`` key
    of the config dict — so the Snowflake writer can pick the right
    typed DDL. Non-Snowflake addresses ignore it.
    """
    from ovid2.config.addresses import load_address as _load_address

    return _load_address(
        address_name,
        sf_overrides=sf_overrides,
        pipeline=pipeline,
        cdm_name=cdm_name,
    )


def load_formatter(
    pipeline: str,
    table: str,
    *,
    spark: object | None = None,
    input_address: BaseAddress | None = None,
    batch_id: str = "",
    engine: object | None = None,
    input_address_name: str | None = None,
) -> BaseFormatter:
    """Load a pipeline-specific formatter for a table.

    Resolution order:

    1. **YAML / Python spec** at ``ovid2_pipelines.<pipeline>.formatters.<table>``
       — the declarative path built in Phase 4.
    2. **Hand-written class** at
       ``ovid2_pipelines.<pipeline>.formatters_custom.<table>_formatter`` —
       for legacy migrations and the ``adapter: custom`` escape hatch.

    The spec path needs a Spark session and an input address (the spec
    reads its own sources). When those are not provided the loader falls
    through to the class path, so contract tests can look up formatters
    without spinning up Spark.

    Args:
        pipeline: Pipeline name.
        table: CDM table name.
        spark: Active Spark session (spec path only).
        input_address: Where the spec should read source files from.
        batch_id: Batch identifier.
        engine: Active compute engine. Threaded onto the formatter's
            :class:`AdapterContext` so engine-portable custom formatters
            can reach ``ctx.functions`` / ``ctx.engine``. ``None`` for
            back-compat with callers that predate engine portability.

    Returns:
        A ready-to-use :class:`BaseFormatter`.

    Raises:
        ValueError: If neither a spec nor a class can be resolved.
    """
    # 1) Try the declarative spec path first when the pipeline context is present.
    if spark is not None and input_address is not None:
        try:
            from ovid2.formatters.engine import build_formatter
            from ovid2.formatters.spec import SpecNotFoundError

            try:
                return build_formatter(
                    pipeline=pipeline,
                    table=table,
                    spark=spark,
                    input_address=input_address,
                    batch_id=batch_id,
                    engine=engine,
                    input_address_name=input_address_name,
                )
            except SpecNotFoundError:
                pass  # fall through to class path
        except ModuleNotFoundError:
            pass  # ovid2.formatters not importable (should not happen in prod)

    # 2) Fall back to a hand-written class.
    module_name = f"ovid2_pipelines.{pipeline}.formatters_custom.{table}_formatter"
    class_name = f"{table.capitalize()}Formatter"

    try:
        module = importlib.import_module(module_name)
        formatter_class = getattr(module, class_name)
    except (ModuleNotFoundError, AttributeError) as exc:
        raise ValueError(
            f"Formatter not found for pipeline '{pipeline}' table '{table}': {exc}"
        ) from exc
    formatter: BaseFormatter = formatter_class()
    return formatter


def load_transformations(transformation_names: list[str]) -> list[BaseTransformer]:
    """Instantiate transformer objects for the given names.

    Args:
        transformation_names: Names like ``['format', 'deduplicate', ...]``.

    Returns:
        Transformer instances in the requested order.

    Raises:
        ValueError: If a name is unknown.
    """
    from ovid2.transformations.analysis.distinct_values import (
        DistinctValuesTransformer,
        RawDistinctValuesTransformer,
    )
    from ovid2.transformations.analysis.monthly_counts import MonthlyCountsTransformer
    from ovid2.transformations.deduplicate import DeduplicateTransformer
    from ovid2.transformations.fix import FixTransformer
    from ovid2.transformations.format import FormatTransformer
    from ovid2.transformations.map import MapTransformer
    from ovid2.transformations.quality.mapping_gap import MappingGapTransformer
    from ovid2.transformations.quality.mapping_report import MappingReportTransformer

    transformer_map: dict[str, type[BaseTransformer]] = {
        "format": FormatTransformer,
        "deduplicate": DeduplicateTransformer,
        "map": MapTransformer,
        "fix": FixTransformer,
        # Analysis
        "monthly_counts": MonthlyCountsTransformer,
        "distinct_values": DistinctValuesTransformer,
        "raw_distinct_values": RawDistinctValuesTransformer,
        # Data quality
        "mapping_gap": MappingGapTransformer,
        "mapping_report": MappingReportTransformer,
    }

    transformers: list[BaseTransformer] = []
    for name in transformation_names:
        cls = transformer_map.get(name)
        if cls is None:
            raise ValueError(f"Unknown transformation: {name}")
        transformers.append(cls())
    return transformers


# Re-export a convenient alias used elsewhere.
__all__ = [
    "get_all_tables",
    "get_all_transformations",
    "has_formatter",
    "load_address",
    "load_engine",
    "load_formatter",
    "load_transformations",
    "resolve_tables",
    "resolve_transformations",
]
