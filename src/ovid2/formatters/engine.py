"""Formatter spec engine.

Executes a :class:`FormatterSpec` by picking the right adapter, running
the source reads + joins, applying the ``projections:`` block, appending
any ``post_expressions:``, and (optionally) validating the output
column set against the declared CDM table.

Also exposes :func:`build_formatter`, which returns a shim implementing
:class:`~ovid2.formatters.base.BaseFormatter` so ``run_pipeline`` can
consume specs and hand-written classes uniformly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ovid2.formatters.adapters import ADAPTERS, AdapterContext
from ovid2.formatters.base import BaseFormatter
from ovid2.formatters.spec import FormatterSpec, find_and_load
from ovid2.schema.cdm import load_cdm_table_columns

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

    from ovid2.core.addresses.base import BaseAddress

logger = logging.getLogger(__name__)


class FormatterSpecError(RuntimeError):
    """Raised when a spec cannot be executed (unknown adapter, missing columns, …)."""


def _projection_expr(expr: str) -> str:
    """Return the SQL fragment to substitute into ``<expr> as <column>``.

    An empty or whitespace-only ``expr`` in the YAML — for example
    ``ENCOUNTERID: ''`` — round-trips through PyYAML as the Python
    empty string. Rendering that verbatim yields the fragment
    ``" as ENCOUNTERID"``, which Spark parses as a reference to a
    column literally named ``as`` (and then fails with
    ``AnalysisException: Column 'as' does not exist``). The legacy
    convention this pattern migrates from is ``lit('').alias('COL')``
    — a SQL empty-string literal, not ``NULL`` — so we emit the
    two-character SQL literal ``''`` for empty projections.
    """
    if not expr or not expr.strip():
        return "''"
    return expr


def execute_spec(spec: FormatterSpec, ctx: AdapterContext) -> DataFrame:
    """Execute a spec end-to-end and return the projected DataFrame.

    Steps:

    1. Look up the adapter and read/prepare the source-side DataFrame.
    2. Apply the ``projections:`` block via ``selectExpr``.
    3. Append every ``post_expressions:`` item (also ``selectExpr``).
    4. If ``output_columns_from:`` is set and the referenced CDM table
       is available on disk, verify the output column set matches.

    Args:
        spec: The validated formatter spec.
        ctx: Adapter runtime context (Spark session, input address, …).

    Returns:
        Lazy DataFrame ready to be handed to the pipeline.

    Raises:
        FormatterSpecError: On unknown adapter or CDM column mismatch.
    """
    if not spec.enabled:
        raise FormatterSpecError(f"Spec '{spec.formatter}' is disabled (enabled: false).")

    adapter_cls = ADAPTERS.get(spec.adapter)
    if adapter_cls is None:
        raise FormatterSpecError(
            f"Unknown adapter '{spec.adapter}'. Valid: {', '.join(sorted(ADAPTERS))}."
        )
    adapter = adapter_cls()
    df = adapter.resolve(spec, ctx)

    # Empty-source shortcut. When every source backing the adapter was
    # optional and absent (``read_source`` -> ``_empty_df``), the resolved
    # DataFrame carries no columns. Applying the ``projections:`` block
    # would then fail with ``Column '<src>' does not exist`` because the
    # source identifiers the projections reference are gone. Instead,
    # synthesise an empty (zero-row) DataFrame whose schema is exactly the
    # projected *output* columns so the table lands as an empty
    # CDM-shaped artefact rather than crashing the run. This mirrors the
    # legacy behaviour where a missing optional input produced an empty
    # output table.
    if spec.projections and not df.columns:
        return _empty_projected_df(df, spec)

    # Projections: dict[column] = SQL expression.
    if spec.projections:
        select_exprs = [
            f"{_projection_expr(expr)} as {column}"
            for column, expr in spec.projections.items()
        ]
        df = df.selectExpr(*select_exprs)

    # Post-expressions overwrite (or add) columns after projections.
    for post in spec.post_expressions:
        df = df.selectExpr("*", post)

    if spec.output_columns_from:
        _validate_output_columns(df, spec)

    return df


def _empty_projected_df(df: DataFrame, spec: FormatterSpec) -> DataFrame:
    """Return a zero-row DataFrame whose columns are the spec's projections.

    Used when every source backing the adapter was optional and absent, so
    the resolved DataFrame has no columns and the projection expressions
    cannot be evaluated. Rather than crash, we produce an empty output with
    the correct CDM column names (all nullable ``string``) so the table
    lands as an empty artefact — matching the legacy behaviour for a
    missing optional input.

    Args:
        df: The (column-less) resolved source DataFrame; used only for its
            active :class:`~pyspark.sql.SparkSession`.
        spec: The formatter spec whose ``projections`` keys become the
            output columns.

    Returns:
        An empty DataFrame with one nullable ``string`` column per
        projection key, in declaration order.
    """
    from pyspark.sql.types import StringType, StructField, StructType

    schema = StructType(
        [StructField(column, StringType(), True) for column in spec.projections]
    )
    return df.sparkSession.createDataFrame([], schema=schema)


def _validate_output_columns(df: DataFrame, spec: FormatterSpec) -> None:
    """Ensure ``df`` exposes every column declared by ``output_columns_from``.

    A missing CDM CSV on disk (developer laptop without ``material/``)
    is treated as a soft warning — the runtime dependency is optional so
    unit tests without the CDM data still work.
    """
    if not spec.output_columns_from:
        return

    try:
        expected = load_cdm_table_columns(spec.output_columns_from)
    except ValueError as exc:
        raise FormatterSpecError(f"Spec '{spec.formatter}': {exc}") from exc

    if not expected:
        logger.warning(
            "CDM schema for %s not available on disk — column validation skipped for spec '%s'.",
            spec.output_columns_from,
            spec.formatter,
        )
        return

    actual = {c.upper() for c in df.columns}
    missing = [c for c in expected if c not in actual]
    if missing:
        raise FormatterSpecError(
            f"Spec '{spec.formatter}' output is missing CDM columns: {missing}."
        )


# ---------------------------------------------------------------------------
# BaseFormatter shim so run_pipeline can consume specs and classes uniformly
# ---------------------------------------------------------------------------


class _SpecFormatter(BaseFormatter):
    """Adapter that presents a :class:`FormatterSpec` as a :class:`BaseFormatter`.

    :meth:`format` must accept a DataFrame per the base contract, but
    the spec engine reads its own sources — we ignore the incoming
    ``df`` (the pipeline reads a placeholder) and return whatever
    :func:`execute_spec` produces.
    """

    def __init__(self, spec: FormatterSpec, ctx: AdapterContext) -> None:
        self._spec = spec
        self._ctx = ctx

    @property
    def table_name(self) -> str:
        return self._spec.formatter

    @property
    def input_file_name(self) -> str:
        if self._spec.input_file_name:
            return self._spec.input_file_name
        # For a ``custom`` spec the real source identifier lives on the
        # hand-written class (its ``input_file_name`` property), not on the
        # YAML. Surface it so the pipeline's placeholder read
        # (``run_pipeline`` -> ``_read_with_pipeline_hook``) targets the true
        # source table rather than the CDM *output* table name. This matters
        # for eager readers like the Snowflake connector, whose ``.load()``
        # resolves the table schema up front: without this, a ``person``
        # formatter reading PCORnet ``DEMOGRAPHIC`` would trip
        # ``Object 'PERSON' does not exist`` because ``table_name`` (the OMOP
        # output name) was used as the read fallback.
        if self._spec.adapter == "custom" and self._spec.class_path:
            class_input = self._custom_class_input_file_name()
            if class_input:
                return class_input
        # Fall back to the first source's glob, if any.
        for source in self._spec.sources.values():
            return source.glob
        return "*"

    def _custom_class_input_file_name(self) -> str | None:
        """Return the ``input_file_name`` declared by a ``custom`` class.

        Instantiates the class named by the spec's ``class:`` field and
        reads its :attr:`~ovid2.formatters.base.BaseFormatter.input_file_name`
        property. Any import / attribute / instantiation failure is swallowed
        (the caller falls through to the source-glob / ``"*"`` default) so a
        misconfigured spec still surfaces its real error later in
        :meth:`format` rather than here.
        """
        import importlib

        try:
            module_name, _, class_name = self._spec.class_path.rpartition(".")  # type: ignore[union-attr]
            if not module_name:
                return None
            module = importlib.import_module(module_name)
            formatter_class = getattr(module, class_name)
            return str(formatter_class().input_file_name)
        except Exception:  # noqa: BLE001 — best-effort; format() surfaces real errors
            logger.debug(
                "Could not resolve input_file_name from custom class %r for spec '%s'; "
                "falling back to source glob.",
                self._spec.class_path,
                self._spec.formatter,
                exc_info=True,
            )
            return None

    @property
    def auxiliary_tables(self) -> list[str]:
        return list(self._spec.auxiliary_tables)

    @property
    def primary_source_optional(self) -> bool:
        """Report whether the spec's primary source is declared optional.

        The primary source is the first entry in the spec's ``sources:``
        block (the one whose glob backs :attr:`input_file_name`). When it
        carries ``optional: true``, a missing input file must not fail the
        table — the adapter's own ``read_source`` already returns an empty
        DataFrame in that case, so the pipeline's eager placeholder read
        can safely degrade to empty too.
        """
        for source in self._spec.sources.values():
            return source.optional
        return False

    def format(self, df: DataFrame) -> DataFrame:
        return execute_spec(self._spec, self._ctx)


def build_formatter(
    pipeline: str,
    table: str,
    *,
    spark: object,
    input_address: BaseAddress,
    batch_id: str,
    engine: object | None = None,
    input_address_name: str | None = None,
) -> BaseFormatter:
    """Locate the spec for ``(pipeline, table)`` and return an executable shim.

    Args:
        pipeline: Pipeline name.
        table: CDM table name.
        spark: Active Spark session.
        input_address: Where to read raw source files.
        batch_id: Batch identifier.
        engine: Active compute engine. Threaded onto the
            :class:`AdapterContext` so engine-portable custom formatters
            can reach ``ctx.functions`` / ``ctx.engine``. ``None`` for
            back-compat with callers that predate engine portability.
        input_address_name: Address config name. Threaded onto the
            context so ``read_source`` can materialise sources through the
            engine's stager under a non-Spark engine.

    Returns:
        A :class:`BaseFormatter` instance wired to the spec's context.
    """
    spec = find_and_load(pipeline, table)
    ctx = AdapterContext(
        spark=spark,  # type: ignore[arg-type]
        input_address=input_address,
        pipeline=pipeline,
        batch_id=batch_id,
        engine=engine,  # type: ignore[arg-type]
        input_address_name=input_address_name,
    )
    return _SpecFormatter(spec, ctx)
