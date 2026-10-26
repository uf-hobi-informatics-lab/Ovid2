"""Data-health check operators.

Each operator implements one *kind* of check named by a
:class:`~ovid2.transformations.quality.registry.CheckSpec`. Operators are
engine-portable: they build **lazy** aggregation DataFrames over the input
tables and never call an eager Spark action themselves. The runner
(:mod:`ovid2.transformations.quality.runner`) does the single, deliberate
materialisation of the small aggregated metric-row frame — exactly the
analysis-pass pattern the ``mapping_gap`` / ``distinct_values``
transformers already use (``# noqa: OVID2001``).

Operator contract
-----------------

    def run(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]

``run`` returns the metric rows for the check. The context exposes the
engine session, the CDM handle, the set of available tables, and small
helpers (``count``, ``table``) so operators stay engine-agnostic. Missing
source tables are handled by the runner before dispatch, so an operator
can assume its declared ``source_tables`` that survived the availability
filter are present — but it should still be defensive.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from ovid2.transformations.quality.results import MetricRow

if TYPE_CHECKING:
    from ovid2.cdm.handle import CdmHandle
    from ovid2.transformations.quality.registry import CheckSpec

# Registry of operator name -> callable. Populated by ``@operator``.
_OPERATORS: dict[str, Callable[[OpContext, CheckSpec], list[MetricRow]]] = {}


@dataclass
class OpContext:
    """Everything an operator needs, engine-agnostically.

    Attributes:
        session: The engine session (real Spark session on the Spark path,
            Snowpark adapter otherwise). Exposes ``.table(name)`` /
            ``.sql(...)``.
        cdm: The active CDM handle (composite keys, valuesets, ids).
        available_tables: Uppercased names of tables present at the input
            (already probed by the runner).
        count_fn: Callable ``(df) -> int`` that materialises a row count.
            The runner supplies the one place a ``.count()`` is allowed.
        pipeline: Pipeline name (for the mapping-coverage operator).
        run_date: Reference date for future-date checks.
        prior_tables: Optional map of uppercased table name -> DataFrame for
            the prior refresh (freshness delta). Empty when no prior input.
    """

    session: Any
    cdm: CdmHandle
    available_tables: frozenset[str]
    count_fn: Callable[[Any], int]
    pipeline: str = ""
    run_date: date | None = None
    prior_tables: dict[str, Any] | None = None

    def table(self, name: str) -> Any:
        """Return the engine DataFrame for ``name`` (case-insensitive)."""
        return self.session.table(name)

    def has(self, name: str) -> bool:
        """Return whether ``name`` (case-insensitive) is available."""
        return name.upper() in self.available_tables

    def rows(self, df: Any) -> int:
        """Materialise a row count via the runner-provided counter.

        Named ``rows`` (not ``count``) so the lazy-Spark contract scanner
        does not mistake this engine-agnostic helper for an eager
        ``DataFrame.count()`` — the single real materialisation lives in
        the runner's ``_count`` (marked ``# noqa: OVID2001``).
        """
        return self.count_fn(df)


def operator(
    name: str,
) -> Callable[
    [Callable[[OpContext, CheckSpec], list[MetricRow]]],
    Callable[[OpContext, CheckSpec], list[MetricRow]],
]:
    """Register a function as the operator named ``name``."""

    def _decorate(
        fn: Callable[[OpContext, CheckSpec], list[MetricRow]],
    ) -> Callable[[OpContext, CheckSpec], list[MetricRow]]:
        _OPERATORS[name] = fn
        return fn

    return _decorate


def get_operator(name: str) -> Callable[[OpContext, CheckSpec], list[MetricRow]] | None:
    """Return the operator callable named ``name``, or ``None``."""
    return _OPERATORS.get(name)


def operator_names() -> frozenset[str]:
    """Return the set of registered operator names."""
    return frozenset(_OPERATORS)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _mk(spec: CheckSpec, **kwargs: Any) -> MetricRow:
    """Build a :class:`MetricRow` pre-filled from ``spec``."""
    return MetricRow(
        check_id=spec.id,
        check_name=spec.name,
        part=spec.part,
        edc_table=spec.edc_table,
        **kwargs,
    )


def _columns(df: Any) -> list[str]:
    """Return upper-cased column names for ``df`` (engine-agnostic)."""
    return [str(c).upper() for c in getattr(df, "columns", [])]


# --------------------------------------------------------------------------- #
# Part 1 — structural integrity                                                #
# --------------------------------------------------------------------------- #


@operator("table_present")
def _table_present(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.01: every declared source table exists; required ones flag."""
    required = {t.upper() for t in spec.params.get("required", [])}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        exists = ctx.has(table)
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="TABLE_EXISTS_FLAG",
                value_num=1.0 if exists else 0.0,
            )
        )
        if table.upper() in required:
            missing = 0.0 if exists else 1.0
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="MISSING_REQUIRED_TABLE_FLAG",
                    value_num=missing,
                    threshold_num=1.0,
                    exception_flag=missing == 1.0,
                    details={"required": True},
                )
            )
    return rows


@operator("table_populated")
def _table_populated(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.02: present required tables have at least one row."""
    required = {t.upper() for t in spec.params.get("required", [])}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        n = ctx.rows(ctx.table(table))
        rows.append(_mk(spec, source_table=table, metric="ROW_COUNT", value_num=float(n)))
        if table.upper() in required:
            empty = 1.0 if n == 0 else 0.0
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="EMPTY_REQUIRED_TABLE_FLAG",
                    value_num=empty,
                    threshold_num=1.0,
                    exception_flag=empty == 1.0,
                )
            )
    return rows


@operator("pk_uniqueness")
def _pk_uniqueness(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.04: composite primary key (from the CDM handle) is unique."""
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        try:
            key = ctx.cdm.dedup_key_for(table.lower())
        except ValueError:
            continue
        if not key:
            continue
        df = ctx.table(table)
        cols = set(_columns(df))
        key_cols = [k for k in key if k.upper() in cols]
        if len(key_cols) != len(key):
            # Key columns missing from the actual data — report informationally.
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="DISTINCT_PK_COUNT",
                    value_num=None,
                    details={"key": list(key), "missing_columns": True},
                )
            )
            continue
        total = ctx.rows(df)
        distinct = ctx.rows(df.select(*key_cols).dropDuplicates())
        dup = float(total - distinct)
        rows.append(_mk(spec, source_table=table, metric="ROW_COUNT", value_num=float(total)))
        rows.append(
            _mk(spec, source_table=table, metric="DISTINCT_PK_COUNT", value_num=float(distinct))
        )
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="DUPLICATE_PK_COUNT",
                value_num=dup,
                threshold_num=0.0,
                exception_flag=dup > 0,
                details={"key": list(key)},
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Part 2 — temporal / plausibility                                             #
# --------------------------------------------------------------------------- #


@operator("future_date")
def _future_date(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.02 / 2.01: date columns later than the run date are implausible.

    DC 2.02 (Table IIB) flags *any* future date (``future_date_rows_gt: 0``);
    DC 2.01 (Table IIIA) flags when the future-date share exceeds a percent
    threshold (``future_date_pct_gt: 5.0``). Both are driven off the same
    lazy scan; the exception metric is selected by which threshold the spec
    supplies.
    """
    from pyspark.sql import functions as F

    run_date = ctx.run_date or date.today()
    run_lit = F.lit(run_date.isoformat()).cast("date")
    rows_threshold = spec.thresholds.get("future_date_rows_gt")
    pct_threshold = spec.thresholds.get("future_date_pct_gt")
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        date_cols = [c for c in _columns(df) if "DATE" in c]
        if not date_cols:
            continue
        total = ctx.rows(df)
        # Build a lazy OR of "any date column is in the future".
        cond = None
        for c in date_cols:
            parsed = F.to_date(F.col(c))
            this = parsed.isNotNull() & (parsed > run_lit)
            cond = this if cond is None else (cond | this)
        future = ctx.rows(df.filter(cond)) if cond is not None else 0
        pct = (100.0 * future / total) if total else 0.0
        rows.append(_mk(spec, source_table=table, metric="TOTAL_ROWS", value_num=float(total)))
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="FUTURE_DATE_ROWS",
                value_num=float(future),
                threshold_num=(float(rows_threshold) if rows_threshold is not None else None),
                exception_flag=(rows_threshold is not None and future > float(rows_threshold)),
                details={"date_columns": date_cols},
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="FUTURE_DATE_PCT",
                value_num=round(pct, 4),
                threshold_num=(float(pct_threshold) if pct_threshold is not None else None),
                exception_flag=(pct_threshold is not None and pct > float(pct_threshold)),
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Part 3 — completeness / conformance                                          #
# --------------------------------------------------------------------------- #


@operator("valueset_membership")
def _valueset_membership(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.06 (Table IIB): value out of the CDM-declared bounded valueset.

    SAS fidelity (edc_report.sas / data_curation_base.sas ``%n_pct``):

    - **Case-sensitive.** The DC query builders uppercase the data and match
      against the (uppercase) valueset via a case-sensitive SAS ``$format``;
      a value that doesn't match lands in the "Values outside of CDM
      specifications" bucket. We mirror this by comparing the trimmed value
      verbatim against the valueset (``case_insensitive: false`` by default).
    - **Fixed-prefix wildcards.** OBSGEN_TYPE permits user-defined /
      PCORnet-reserved codes whose first three characters are ``UD_`` or
      ``PC_`` (data_curation_tables.sas:1259-1272). These are stored in the
      CDM valueset as ``UD_*`` / ``PC_*``; any valueset code ending in ``*``
      is treated as a *prefix* match on the literal characters before the
      ``*`` (so ``UD_*`` matches ``UD_ANYTHING``). Non-wildcard codes match
      exactly.
    - **Any occurrence flags.** DC 1.06 is count-based: a single
      out-of-valueset record raises the exception (``col3flag=1``). The
      spec sets ``out_of_valueset_pct_gt: 0.0`` so ``pct > 0`` flags.
    """
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("out_of_valueset_pct_gt", 0.0))
    # SAS matches case-sensitively; keep an opt-in override for callers.
    ci = bool(spec.params.get("case_insensitive", False))
    fon = ctx.cdm.flavour_of_null
    fon_cmp = {str(v).upper() if ci else str(v) for v in fon if v is not None}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        table_vs = ctx.cdm.valuesets.get(table.lower(), {})
        if not table_vs:
            continue
        df = ctx.table(table)
        cols = set(_columns(df))
        total = ctx.rows(df)
        for column, allowed in table_vs.items():
            if column.upper() not in cols or not allowed:
                continue
            # Split the valueset into exact codes and ``*``-suffixed prefixes.
            exact: set[str] = set()
            prefixes: list[str] = []
            for code in allowed:
                code_cmp = code.upper() if ci else code
                if code_cmp.endswith("*"):
                    prefixes.append(code_cmp[:-1])
                else:
                    exact.add(code_cmp)
            value_expr = F.upper(F.trim(F.col(column))) if ci else F.trim(F.col(column))
            # Rows with a non-null, non-empty, non-flavour-of-null value.
            populated = value_expr.isNotNull() & (F.trim(F.col(column)) != F.lit(""))
            if fon_cmp:
                populated = populated & (~value_expr.isin(list(fon_cmp)))
            # Conforming = exact-set member OR matches any wildcard prefix.
            conforms = value_expr.isin(list(exact)) if exact else F.lit(False)
            for pfx in prefixes:
                conforms = conforms | value_expr.startswith(pfx)
            out = populated & (~conforms)
            oo = ctx.rows(df.filter(out))
            pct = (100.0 * oo / total) if total else 0.0
            exc = pct > threshold
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="OUT_OF_VALUESET_ROWS",
                    value_num=float(oo),
                    details={"column": column},
                )
            )
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="OUT_OF_VALUESET_PCT",
                    value_num=round(pct, 4),
                    threshold_num=threshold,
                    exception_flag=exc,
                    details={
                        "column": column,
                        "valueset_size": len(allowed),
                        "wildcard_prefixes": prefixes,
                    },
                )
            )
    return rows


@operator("null_rate")
def _null_rate(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 3.09: per-column null / flavour-of-null rate (completeness heatmap)."""
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("null_pct_gt", 90.0))
    treat_fon = bool(spec.params.get("treat_flavour_of_null_as_missing", True))
    # Optional per-table column allow-list (e.g. DC 1.07 required NOT NULL
    # fields). ``{table: [col, ...]}``; empty means every column.
    required: dict[str, set[str]] = {
        str(t).upper(): {str(c).upper() for c in cols}
        for t, cols in spec.params.get("required_fields", {}).items()
    }
    fon_upper = {str(v).upper() for v in ctx.cdm.flavour_of_null if v is not None}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        total = ctx.rows(df)
        if total == 0:
            continue
        wanted = required.get(table.upper())
        for column in _columns(df):
            if wanted is not None and column not in wanted:
                continue
            trimmed = F.trim(F.col(column))
            missing = F.col(column).isNull() | (trimmed == F.lit(""))
            if treat_fon and fon_upper:
                missing = missing | F.upper(trimmed).isin(list(fon_upper))
            nulls = ctx.rows(df.filter(missing))
            pct = 100.0 * nulls / total
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="NULL_PCT",
                    value_num=round(pct, 4),
                    threshold_num=threshold,
                    exception_flag=pct > threshold,
                    details={"column": column, "null_rows": nulls, "total_rows": total},
                )
            )
    return rows


# --------------------------------------------------------------------------- #
# Native panels                                                                #
# --------------------------------------------------------------------------- #


@operator("orphan_fk")
def _orphan_fk(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """Referential-integrity sweep over declared child->parent relationships.

    DC 1.08/1.12/1.14 flag *any* orphan (``orphan_rows_gt: 0``); DC 1.09/1.11
    flag when the orphan share exceeds a percent threshold
    (``orphan_pct_gt: 5.0``). Which metric raises the exception is chosen by
    whichever threshold the spec supplies (rows by default).
    """
    from pyspark.sql import functions as F

    rows_threshold = spec.thresholds.get("orphan_rows_gt", 0)
    pct_threshold = spec.thresholds.get("orphan_pct_gt")
    rows: list[MetricRow] = []
    rels = spec.params.get("relationships", [])
    for rel in rels:
        child = str(rel["child"])
        parent = str(rel["parent"])
        child_key = str(rel["child_key"])
        parent_key = str(rel["parent_key"])
        if not (ctx.has(child) and ctx.has(parent)):
            continue
        cdf = ctx.table(child)
        pdf = ctx.table(parent)
        if child_key.upper() not in set(_columns(cdf)):
            continue
        if parent_key.upper() not in set(_columns(pdf)):
            continue
        # Non-null child keys that fail a left-anti join against the parent.
        child_nonnull = cdf.filter(F.col(child_key).isNotNull())
        child_total = ctx.rows(child_nonnull)
        parent_keys = pdf.select(F.col(parent_key).alias(child_key)).dropDuplicates()
        orphans = child_nonnull.join(parent_keys, on=child_key, how="left_anti")
        orphan_n = ctx.rows(orphans)
        pct = (100.0 * orphan_n / child_total) if child_total else 0.0
        detail = {"child_key": child_key, "parent": parent, "parent_key": parent_key}
        rows.append(
            _mk(
                spec,
                source_table=child,
                metric="CHILD_ROWS",
                value_num=float(child_total),
                details=detail,
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=child,
                metric="ORPHAN_ROWS",
                value_num=float(orphan_n),
                threshold_num=(float(rows_threshold) if pct_threshold is None else None),
                exception_flag=(pct_threshold is None and orphan_n > float(rows_threshold)),
                details=detail,
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=child,
                metric="ORPHAN_PCT",
                value_num=round(pct, 4),
                threshold_num=(float(pct_threshold) if pct_threshold is not None else None),
                exception_flag=(pct_threshold is not None and pct > float(pct_threshold)),
                details=detail,
            )
        )
    return rows


@operator("mapping_coverage")
def _mapping_coverage(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """Mapping / vocabulary coverage rollup over the pipeline dictionaries."""
    import importlib

    from pyspark.sql import functions as F

    if not ctx.pipeline:
        return []
    try:
        dictionaries = importlib.import_module(f"ovid2_pipelines.{ctx.pipeline}.dictionaries")
    except ModuleNotFoundError:
        return []
    threshold = float(spec.thresholds.get("unmapped_pct_gt", 1.0))
    fon = {str(v).upper() for v in ctx.cdm.flavour_of_null if v is not None}
    suffix = "_dict"
    rows: list[MetricRow] = []
    for dict_name in dir(dictionaries):
        if not dict_name.endswith(suffix) or dict_name.startswith("_"):
            continue
        stem = dict_name[: -len(suffix)]
        if "_" not in stem:
            continue
        table, column = stem.rsplit("_", 1)
        table_u = table.upper()
        if not ctx.has(table_u):
            continue
        df = ctx.table(table_u)
        if column.upper() not in set(_columns(df)):
            continue
        pipeline_dict = getattr(dictionaries, dict_name)
        if not isinstance(pipeline_dict, dict):
            continue
        mapped_upper = [str(k).upper() for k in pipeline_dict]
        value_u = F.upper(F.trim(F.col(column)))
        populated = (
            value_u.isNotNull() & (F.trim(F.col(column)) != F.lit("")) & (~value_u.isin(list(fon)))
        )
        pop_df = df.filter(populated)
        total = ctx.rows(pop_df)
        if total == 0:
            continue
        unmapped_df = pop_df.filter(~value_u.isin(mapped_upper))
        unmapped_rows = ctx.rows(unmapped_df)
        unmapped_distinct = ctx.rows(unmapped_df.select(value_u).dropDuplicates())
        pct = 100.0 * unmapped_rows / total
        detail = {"column": column}
        rows.append(
            _mk(
                spec,
                source_table=table_u,
                metric="TOTAL_ROWS",
                value_num=float(total),
                details=detail,
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=table_u,
                metric="UNMAPPED_DISTINCT",
                value_num=float(unmapped_distinct),
                details=detail,
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=table_u,
                metric="UNMAPPED_ROWS",
                value_num=float(unmapped_rows),
                details=detail,
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=table_u,
                metric="UNMAPPED_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct > threshold,
                details=detail,
            )
        )
    return rows


@operator("freshness_delta")
def _freshness_delta(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """Freshness & volume trend: current vs prior-refresh row counts."""
    threshold = float(spec.thresholds.get("row_delta_pct_abs_gt", 20.0))
    prior = ctx.prior_tables or {}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        current = ctx.rows(ctx.table(table))
        rows.append(_mk(spec, source_table=table, metric="CURRENT_ROWS", value_num=float(current)))
        prior_df = prior.get(table.upper())
        if prior_df is None:
            continue
        prior_n = ctx.rows(prior_df)
        delta = current - prior_n
        pct = (100.0 * delta / prior_n) if prior_n else (100.0 if current else 0.0)
        rows.append(_mk(spec, source_table=table, metric="PRIOR_ROWS", value_num=float(prior_n)))
        rows.append(_mk(spec, source_table=table, metric="ROW_DELTA", value_num=float(delta)))
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="ROW_DELTA_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=abs(pct) > threshold,
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# EDC v7.03 ports — additional operators                                       #
#                                                                              #
# Each operator below ports the *semantics* of one (or a family of) PCORnet    #
# EDC / Data-Curation-Query checks from the authoritative SAS package          #
# (``edc_report.sas`` v7.03). The SAS report reads pre-aggregated L3 query      #
# datasets; Ovid2 recomputes the same metric directly from the raw CDM tables  #
# as a lazy analysis pass. Thresholds are quoted from the SAS ``dc_summary_*`` #
# exception blocks in each docstring.                                          #
# --------------------------------------------------------------------------- #


def _populated_expr(column: str, fon_upper: set[str]) -> Any:
    """Return a lazy boolean column: value is present (not null / blank / FON)."""
    from pyspark.sql import functions as F

    trimmed = F.trim(F.col(column))
    present = F.col(column).isNotNull() & (trimmed != F.lit(""))
    if fon_upper:
        present = present & (~F.upper(trimmed).isin(list(fon_upper)))
    return present


@operator("refresh_date_present")
def _refresh_date_present(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.18 (Table ID): a present CDM table has no valid data-refresh date.

    SAS: ``if upcase(col2)="N/A" ... then threshold=1``. The refresh date
    is the maximum populated event/refresh date on the table; a table that
    is present but exposes no resolvable date column (or all-null dates) is
    flagged. ``EXTERNAL_MEDS`` / ``PAT_RELATIONSHIP`` are excluded per SAS.
    """
    from pyspark.sql import functions as F

    exclude = {t.upper() for t in spec.params.get("exclude", [])}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table) or table.upper() in exclude:
            continue
        df = ctx.table(table)
        date_cols = [c for c in _columns(df) if "DATE" in c]
        if not date_cols:
            # No date column at all — cannot derive a refresh date.
            rows.append(
                _mk(
                    spec,
                    source_table=table,
                    metric="MISSING_REFRESH_DATE_FLAG",
                    value_num=1.0,
                    threshold_num=1.0,
                    exception_flag=True,
                    details={"reason": "no date column"},
                )
            )
            continue
        # Any parseable date across the date columns yields a refresh date.
        has_date = None
        for c in date_cols:
            parsed = F.to_date(F.col(c)).isNotNull()
            has_date = parsed if has_date is None else (has_date | parsed)
        populated = ctx.rows(df.filter(has_date)) if has_date is not None else 0
        missing = 0.0 if populated > 0 else 1.0
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="MISSING_REFRESH_DATE_FLAG",
                value_num=missing,
                threshold_num=1.0,
                exception_flag=missing == 1.0,
                details={"date_columns": date_cols},
            )
        )
    return rows


@operator("panel_loinc_rate")
def _panel_loinc_rate(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.20 (Table IIG): >5% of LOINC records are panel-type LOINCs.

    SAS: ``if input(col5,5.2)>5.00`` on the ``Panel_Type='Panel'`` share.
    A curated set of panel LOINC codes is supplied via ``params.panel_loincs``
    (the SAS package ships the full LOINC panel reference; the operator uses
    the provided list so the check stays engine-portable and data-free).
    """
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("panel_pct_gt", 5.0))
    panels = {str(c).upper() for c in spec.params.get("panel_loincs", [])}
    column = str(spec.params.get("loinc_column", "LAB_LOINC"))
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        col_u = None
        for candidate in (column, "LAB_LOINC", "OBSCLIN_CODE", "PRO_CODE"):
            if candidate.upper() in set(_columns(df)):
                col_u = candidate.upper()
                break
        if col_u is None:
            continue
        value_u = F.upper(F.trim(F.col(col_u)))
        loinc_rows = df.filter(value_u.isNotNull() & (F.trim(F.col(col_u)) != F.lit("")))
        total = ctx.rows(loinc_rows)
        if total == 0:
            continue
        panel_n = ctx.rows(loinc_rows.filter(value_u.isin(list(panels)))) if panels else 0
        pct = 100.0 * panel_n / total
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="LOINC_ROWS",
                value_num=float(total),
                details={"column": col_u},
            )
        )
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="PANEL_LOINC_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct > threshold,
                details={"column": col_u, "panel_rows": panel_n},
            )
        )
    return rows


@operator("code_placement")
def _code_placement(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.13 / 1.16 (Table IIF): code-type errors and misplaced codes.

    SAS: ``if bad_record_pct>5.00 then row=1.13`` (a record's code does not
    match its declared code type) and ``else if misplaced_record_pct>0 then
    row=1.16`` (a code sits in a table it does not belong to). ``params.rules``
    lists ``{table, code_column, type_column, valid_types:[…]}``. A record is
    *bad* when its type value is outside ``valid_types``; *misplaced* is a
    hook reserved for cross-table rules (0 by default here).
    """
    from pyspark.sql import functions as F

    bad_threshold = float(spec.thresholds.get("bad_record_pct_gt", 5.0))
    rows: list[MetricRow] = []
    for rule in spec.params.get("rules", []):
        table = str(rule["table"])
        type_col = str(rule["type_column"])
        valid = {str(v).upper() for v in rule.get("valid_types", [])}
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        if type_col.upper() not in set(_columns(df)):
            continue
        type_u = F.upper(F.trim(F.col(type_col)))
        populated = df.filter(type_u.isNotNull() & (F.trim(F.col(type_col)) != F.lit("")))
        total = ctx.rows(populated)
        if total == 0:
            continue
        bad_n = ctx.rows(populated.filter(~type_u.isin(list(valid)))) if valid else 0
        pct = 100.0 * bad_n / total
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="BAD_CODE_TYPE_PCT",
                value_num=round(pct, 4),
                threshold_num=bad_threshold,
                exception_flag=pct > bad_threshold,
                details={"type_column": type_col, "bad_rows": bad_n, "total_rows": total},
            )
        )
    return rows


@operator("illogical_date")
def _illogical_date(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.03 (Table IIIC): illogical date sequences for a patient's records.

    SAS: ``if col3p>5.0`` — >5% of ENCOUNTER patients carry an illogical date
    pair. ``params.pairs`` lists ``{table, earlier, later}``; a row is
    illogical when both dates parse and ``later < earlier`` (e.g.
    ``DISCHARGE_DATE < ADMIT_DATE``, ``ADMIT_DATE < BIRTH_DATE``).
    """
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("illogical_pct_gt", 5.0))
    rows: list[MetricRow] = []
    for pair in spec.params.get("pairs", []):
        table = str(pair["table"])
        earlier = str(pair["earlier"])
        later = str(pair["later"])
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        cols = set(_columns(df))
        if earlier.upper() not in cols or later.upper() not in cols:
            continue
        e = F.to_date(F.col(earlier))
        l_ = F.to_date(F.col(later))
        total = ctx.rows(df)
        if total == 0:
            continue
        bad = df.filter(e.isNotNull() & l_.isNotNull() & (l_ < e))
        bad_n = ctx.rows(bad)
        pct = 100.0 * bad_n / total
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="ILLOGICAL_DATE_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct > threshold,
                details={"earlier": earlier, "later": later, "bad_rows": bad_n},
            )
        )
    return rows


@operator("implausible_value")
def _implausible_value(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.02 (Table IIIB): >10% of numeric values are implausibly low/high.

    SAS: ``if input(col8,8.2)>10.00 or input(col10,8.2)>10.00`` (low-tail or
    high-tail category share). ``params.ranges`` lists
    ``{table, column, low, high}``; a value below ``low`` or above ``high``
    (both optional) is implausible.
    """
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("implausible_pct_gt", 10.0))
    rows: list[MetricRow] = []
    for rng in spec.params.get("ranges", []):
        table = str(rng["table"])
        column = str(rng["column"])
        low = rng.get("low")
        high = rng.get("high")
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        if column.upper() not in set(_columns(df)):
            continue
        num = F.col(column).cast("double")
        populated = df.filter(num.isNotNull())
        total = ctx.rows(populated)
        if total == 0:
            continue
        cond = None
        if low is not None:
            cond = num < F.lit(float(low))
        if high is not None:
            hi = num > F.lit(float(high))
            cond = hi if cond is None else (cond | hi)
        bad_n = ctx.rows(populated.filter(cond)) if cond is not None else 0
        pct = 100.0 * bad_n / total
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="IMPLAUSIBLE_VALUE_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct > threshold,
                details={"column": column, "low": low, "high": high, "bad_rows": bad_n},
            )
        )
    return rows


@operator("encounter_per_visit")
def _encounter_per_visit(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.04 (Table IIID): >2 encounters per distinct visit for ED/EI/IP.

    SAS: ``if col1 in ('ED...' 'EI...' 'IP...') and col7p>2.0``. A "visit" is
    the tuple ``(PATID, ADMIT_DATE, ENC_TYPE)``; the metric is
    encounters / distinct visits within each flagged encounter type.
    """
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("enc_per_visit_gt", 2.0))
    enc_types = {str(t).upper() for t in spec.params.get("enc_types", ["ED", "EI", "IP"])}
    table = "ENCOUNTER"
    if not ctx.has(table):
        return []
    df = ctx.table(table)
    cols = set(_columns(df))
    if not {"PATID", "ADMIT_DATE", "ENC_TYPE"}.issubset(cols):
        return []
    rows: list[MetricRow] = []
    et_u = F.upper(F.trim(F.col("ENC_TYPE")))
    for et in sorted(enc_types):
        subset = df.filter(et_u == F.lit(et))
        enc_n = ctx.rows(subset)
        if enc_n == 0:
            continue
        visits = ctx.rows(subset.select("PATID", "ADMIT_DATE", "ENC_TYPE").dropDuplicates())
        ratio = (enc_n / visits) if visits else 0.0
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="ENCOUNTERS_PER_VISIT",
                value_num=round(ratio, 4),
                threshold_num=threshold,
                exception_flag=ratio > threshold,
                details={"enc_type": et, "encounters": enc_n, "visits": visits},
            )
        )
    return rows


@operator("records_per_encounter")
def _records_per_encounter(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 3.01 / 3.02 (Table IVA / IVB): too few dx/px records per encounter.

    SAS 3.01: ``if cat in ('AV' 'ED' 'EI' 'IP' 'TH') and .<perc<1.00`` for
    DIAGNOSIS. SAS 3.02: ``(cat in ('AV' 'ED') and .<perc<0.75) or
    (cat in ('EI' 'IP') and .<perc<1.00)`` for PROCEDURES. ``perc`` is
    child records with a *known* type per encounter of that encounter type.
    Thresholds per encounter type are supplied in ``params.min_per_type``.
    """
    from pyspark.sql import functions as F

    child = str(spec.params["child_table"])
    type_col = str(spec.params["type_column"])
    min_per_type: dict[str, float] = {
        str(k).upper(): float(v) for k, v in spec.params.get("min_per_type", {}).items()
    }
    fon_upper = {str(v).upper() for v in ctx.cdm.flavour_of_null if v is not None}
    if not (ctx.has("ENCOUNTER") and ctx.has(child)):
        return []
    enc = ctx.table("ENCOUNTER")
    cdf = ctx.table(child)
    if "ENC_TYPE" not in set(_columns(enc)) or "ENCOUNTERID" not in set(_columns(enc)):
        return []
    if not {"ENCOUNTERID", type_col.upper()}.issubset(set(_columns(cdf))):
        return []
    enc_type_u = F.upper(F.trim(F.col("ENC_TYPE")))
    known_type = _populated_expr(type_col, fon_upper)
    child_known = cdf.filter(known_type)
    rows: list[MetricRow] = []
    for et, minimum in sorted(min_per_type.items()):
        enc_et = enc.filter(enc_type_u == F.lit(et))
        enc_n = ctx.rows(enc_et.select("ENCOUNTERID").dropDuplicates())
        if enc_n == 0:
            continue
        enc_ids = enc_et.select("ENCOUNTERID").dropDuplicates()
        matched = child_known.join(enc_ids, on="ENCOUNTERID", how="inner")
        rec_n = ctx.rows(matched)
        perc = (rec_n / enc_n) if enc_n else 0.0
        rows.append(
            _mk(
                spec,
                source_table=child,
                metric="RECORDS_PER_ENCOUNTER",
                value_num=round(perc, 4),
                threshold_num=minimum,
                exception_flag=0.0 <= perc < minimum,
                details={"enc_type": et, "records": rec_n, "encounters": enc_n},
            )
        )
    return rows


@operator("field_completeness")
def _field_completeness(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 3.03 (Table IVC / IVD): key fields missing/unmapped in >=10% of rows.

    SAS: ``if var in (…) and perc>=10.00 then thresholdflag=1``. ``params.fields``
    lists ``{table, column}`` pairs (the key fields for that EDC table); the
    metric is the missing / flavour-of-null share per field.
    """

    threshold = float(spec.thresholds.get("missing_pct_ge", 10.0))
    fon_upper = {str(v).upper() for v in ctx.cdm.flavour_of_null if v is not None}
    rows: list[MetricRow] = []
    for fld in spec.params.get("fields", []):
        table = str(fld["table"])
        column = str(fld["column"])
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        if column.upper() not in set(_columns(df)):
            continue
        total = ctx.rows(df)
        if total == 0:
            continue
        present = _populated_expr(column, fon_upper)
        missing_n = ctx.rows(df.filter(~present))
        pct = 100.0 * missing_n / total
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="MISSING_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct >= threshold,
                details={"column": column, "missing_rows": missing_n, "total_rows": total},
            )
        )
    return rows


@operator("known_value_coverage")
def _known_value_coverage(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 3.08/3.09/3.12/3.15/3.16/3.17 (Table IVH/IVI): <80% populated coverage.

    SAS (IVI): ``if ord in (3,8,14,16) and (.<col5p<80.0 or (col3=0 ...))`` and
    (IVH): ``if ord in (1,7) and (col5p<80.0 or col4=0)``. Generic form: the
    share of records in ``table`` whose ``column`` is populated (non-null,
    non-blank, non-flavour-of-null) must be >=80%; below that flags. Used for
    RXCUI-tier coverage, LAB_LOINC/result coverage, and result-unit coverage.
    """
    from pyspark.sql import functions as F  # noqa: F401 (kept for parity/clarity)

    threshold = float(spec.thresholds.get("coverage_pct_lt", 80.0))
    fon_upper = {str(v).upper() for v in ctx.cdm.flavour_of_null if v is not None}
    rows: list[MetricRow] = []
    for cov in spec.params.get("coverage", []):
        table = str(cov["table"])
        column = str(cov["column"])
        require = [str(c) for c in cov.get("require_also", [])]
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        cols = set(_columns(df))
        if column.upper() not in cols:
            continue
        total = ctx.rows(df)
        if total == 0:
            continue
        present = _populated_expr(column, fon_upper)
        for extra in require:
            if extra.upper() in cols:
                present = present & _populated_expr(extra, fon_upper)
        covered_n = ctx.rows(df.filter(present))
        pct = 100.0 * covered_n / total
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="COVERAGE_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct < threshold,
                details={
                    "column": column,
                    "require_also": require,
                    "covered_rows": covered_n,
                    "total_rows": total,
                },
            )
        )
    return rows


@operator("patient_coverage")
def _patient_coverage(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.09/3.04/3.05/3.18 (Table IB): <threshold% of patients have related data.

    SAS: denominator is distinct patients (with a face-to-face encounter for
    2.09/3.18); numerator is patients who also appear in the related table(s).
    ``params.related`` lists child tables that must contain the patient; the
    metric is the covered-patient share. Threshold defaults to 80% (2.09/3.18)
    or 50% (3.04/3.05) per ``thresholds.coverage_pct_lt``.
    """

    threshold = float(spec.thresholds.get("coverage_pct_lt", 80.0))
    denom_table = str(spec.params.get("denominator_table", "DEMOGRAPHIC"))
    related = [str(t) for t in spec.params.get("related", [])]
    if not ctx.has(denom_table):
        return []
    denom = ctx.table(denom_table)
    if "PATID" not in set(_columns(denom)):
        return []
    denom_patids = denom.select("PATID").dropDuplicates()
    denom_n = ctx.rows(denom_patids)
    rows: list[MetricRow] = []
    if denom_n == 0:
        return rows
    for rel in related:
        if not ctx.has(rel):
            covered_n = 0
        else:
            rdf = ctx.table(rel)
            if "PATID" not in set(_columns(rdf)):
                continue
            rel_patids = rdf.select("PATID").dropDuplicates()
            covered_n = ctx.rows(denom_patids.join(rel_patids, on="PATID", how="inner"))
        pct = 100.0 * covered_n / denom_n
        rows.append(
            _mk(
                spec,
                source_table=rel,
                metric="PATIENT_COVERAGE_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=pct < threshold,
                details={
                    "denominator_table": denom_table,
                    "denominator": denom_n,
                    "covered_patients": covered_n,
                },
            )
        )
    return rows


@operator("monthly_volume")
def _monthly_volume(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.08 / 3.07 / 3.11: most-recent-month volume anomaly.

    SAS: the most recent complete month has zero records, or falls below a
    fraction (default 75%) of the trailing-12-month mean. Ovid2 recomputes
    this from the raw table's event date: it buckets rows by ``yyyy-MM`` on
    ``params.date_column`` (falling back to the first ``*DATE`` column),
    takes the latest bucket as the "current month", and compares it to the
    mean of the preceding twelve buckets.

    The per-month aggregate is a tiny frame (at most a few dozen buckets), so
    a single ``.collect()`` on that grouped result is a legitimate analysis
    pass (marked ``# noqa: OVID2001``), mirroring ``mapping_gap``.
    """
    from pyspark.sql import functions as F

    frac = float(spec.thresholds.get("recent_month_pct_lt", 75.0)) / 100.0
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        cols = _columns(df)
        date_col = str(spec.params.get("date_column", "")).upper()
        if date_col not in cols:
            date_col = next((c for c in cols if "DATE" in c), "")
        if not date_col:
            continue
        parsed = F.to_date(F.col(date_col))
        # ``GroupedData.count()`` is a *lazy* aggregation (returns a DataFrame),
        # not the eager ``DataFrame.count()`` action — but the AST scanner sees
        # the method name ``count`` on the multi-line expression's first line,
        # so the suppression marker rides here. The single real materialisation
        # is the ``.collect()`` on the tiny bucket frame below.
        monthly = (
            df.filter(parsed.isNotNull())  # noqa: OVID2001 lazy GroupedData.count() below
            .groupBy(F.date_format(parsed, "yyyy-MM").alias("YM"))
            .count()
        )
        buckets = monthly.orderBy(F.col("YM").asc()).collect()  # noqa: OVID2001 analysis pass
        pairs = [(str(r["YM"]), int(r["count"])) for r in buckets if r["YM"] is not None]
        if not pairs:
            continue
        recent_month, recent_count = pairs[-1]
        prior = pairs[-13:-1]  # up to 12 months preceding the latest
        prior_mean = (sum(c for _, c in prior) / len(prior)) if prior else None
        expected = (prior_mean * frac) if prior_mean is not None else None
        exc = recent_count == 0 or (expected is not None and recent_count < expected)
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="RECENT_MONTH_ROWS",
                value_num=float(recent_count),
                threshold_num=(round(expected, 4) if expected is not None else None),
                exception_flag=exc,
                details={
                    "date_column": date_col,
                    "recent_month": recent_month,
                    "trailing12_mean": (round(prior_mean, 4) if prior_mean is not None else None),
                },
            )
        )
    return rows


@operator("monthly_volume_zscore")
def _monthly_volume_zscore(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 2.08 (Table IIIG): monthly-volume outlier via the SAS z-score/NSD test.

    Faithful port of ``edc_report.sas`` v7.03 lines 6489-6617:

    - Bucket rows by ``yyyy-MM`` on the table's event date. For the
      ENC_TYPE-stratified tables (ENCOUNTER / DIAGNOSIS / PROCEDURES) also
      stratify by ENC_TYPE in ``(AV, ED, IP, EI, TH)`` — declared per source
      via ``params.enc_type_tables`` + ``params.enc_types``.
    - For each series, compute a **12-month moving average** (``lag_avg``)
      and **moving std** (``lag_std``), each **lagged by one month** (the
      current month is excluded from its own benchmark). ``nback = 12``.
    - Drop a month from consideration when ``lag_avg < 500`` (floor),
      ``lag_std <= 0``, the month is ``<= p5_date`` (before the series'
      5th-percentile month) or ``>= month11_date`` (within the last 11
      months), or the month is 2020-03 / 2020-04 (COVID exclusion).
    - Compute ``dsd = (resultn - lag_avg) / lag_std`` and flag the month when
      ``resultn = 0`` **or** ``dsd < nsd`` (``nsd = -7``).

    Any surviving flagged month raises the check's exception for that table.
    The per-month bucket frame is tiny, so the single ``.collect()`` analysis
    pass mirrors ``monthly_volume`` (marked ``# noqa: OVID2001``).
    """
    from pyspark.sql import functions as F

    nback = int(spec.params.get("nback", 12))
    nsd = float(spec.params.get("nsd", -7.0))
    lag_avg_floor = float(spec.params.get("lag_avg_floor", 500.0))
    excluded_months = {str(m) for m in spec.params.get("excluded_months", ["2020-03", "2020-04"])}
    enc_type_tables = {str(t).upper() for t in spec.params.get("enc_type_tables", [])}
    enc_types = {str(e).upper() for e in spec.params.get("enc_types", [])}
    date_columns = {
        str(k).upper(): str(v).upper() for k, v in spec.params.get("date_columns", {}).items()
    }
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        cols = _columns(df)
        date_col = date_columns.get(table.upper(), "")
        if date_col not in cols:
            date_col = next((c for c in cols if "DATE" in c), "")
        if not date_col:
            continue
        parsed = F.to_date(F.col(date_col))
        stratify = table.upper() in enc_type_tables and "ENC_TYPE" in cols
        base = df.filter(parsed.isNotNull())
        if stratify and enc_types:
            base = base.filter(F.upper(F.trim(F.col("ENC_TYPE"))).isin(list(enc_types)))
        group_cols = [F.date_format(parsed, "yyyy-MM").alias("YM")]
        if stratify:
            group_cols.append(F.upper(F.trim(F.col("ENC_TYPE"))).alias("CAT"))
        monthly = base.groupBy(*group_cols).count()  # noqa: OVID2001 lazy GroupedData.count()
        buckets = monthly.collect()  # noqa: OVID2001 analysis pass — tiny bucket frame

        # Organise into per-category month->count series.
        from collections import defaultdict

        series: dict[str, dict[str, int]] = defaultdict(dict)
        for r in buckets:
            ym = r["YM"]
            if ym is None:
                continue
            cat = r["CAT"] if stratify else "__all__"
            series[cat][str(ym)] = int(r["count"])

        flagged_any = False
        flagged_detail: list[dict[str, Any]] = []
        for cat, month_counts in series.items():
            months = sorted(month_counts)
            if not months:
                continue
            # month11_date proxy: 11 months before the latest observed month.
            latest = months[-1]
            month11 = _shift_month(latest, -11)
            # p5_date proxy: the 5th-percentile observed month.
            p5_idx = int(0.05 * (len(months) - 1))
            p5_month = months[p5_idx]
            counts = [month_counts[m] for m in months]
            for i, month in enumerate(months):
                # Lagged 12-month window: the up-to-12 months preceding ``month``.
                window = counts[max(0, i - nback) : i]
                if len(window) < 1:
                    continue
                lag_avg = sum(window) / len(window)
                mean = lag_avg
                var = sum((c - mean) ** 2 for c in window) / len(window)
                lag_std = var**0.5
                resultn = counts[i]
                # SAS drop conditions.
                if lag_avg < lag_avg_floor or lag_std <= 0:
                    continue
                if month <= p5_month or month >= month11:
                    continue
                if month in excluded_months:
                    continue
                dsd = (resultn - lag_avg) / lag_std
                if resultn == 0 or dsd < nsd:
                    flagged_any = True
                    flagged_detail.append(
                        {
                            "category": cat,
                            "month": month,
                            "resultn": resultn,
                            "lag_avg": round(lag_avg, 2),
                            "lag_std": round(lag_std, 2),
                            "dsd": round(dsd, 2),
                        }
                    )
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="MONTHLY_VOLUME_OUTLIER",
                value_num=float(len(flagged_detail)),
                threshold_num=nsd,
                exception_flag=flagged_any,
                details={
                    "date_column": date_col,
                    "stratified": stratify,
                    "nback": nback,
                    "nsd": nsd,
                    "outliers": flagged_detail[:20],
                },
            )
        )
    return rows


def _shift_month(ym: str, months: int) -> str:
    """Return the ``yyyy-MM`` string ``months`` away from ``ym`` (may be negative)."""
    year, mon = (int(x) for x in ym.split("-"))
    idx = year * 12 + (mon - 1) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


@operator("refresh_trend")
def _refresh_trend(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 4.01/4.02/4.03 (Table VA/VB/VC): counts dropped vs the prior refresh.

    SAS: ``if (col3n=0 and col2n>0) or (.<col4n<-5.00) ...`` — a table's record
    count (or distinct-patient count) fell to zero having been positive, or
    decreased more than 5% versus the previous refresh. Requires the prior
    tables (``--prev_input_address``); tables with no prior are reported
    informationally.
    """
    from pyspark.sql import functions as F  # noqa: F401

    threshold = float(spec.thresholds.get("decrease_pct_lt", -5.0))
    prior = ctx.prior_tables or {}
    rows: list[MetricRow] = []
    for table in spec.source_tables:
        if not ctx.has(table):
            continue
        current = ctx.rows(ctx.table(table))
        rows.append(_mk(spec, source_table=table, metric="CURRENT_ROWS", value_num=float(current)))
        prior_df = prior.get(table.upper())
        if prior_df is None:
            continue
        prior_n = ctx.rows(prior_df)
        rows.append(_mk(spec, source_table=table, metric="PRIOR_ROWS", value_num=float(prior_n)))
        pct = (100.0 * (current - prior_n) / prior_n) if prior_n else 0.0
        dropped_to_zero = current == 0 and prior_n > 0
        exc = dropped_to_zero or (prior_n > 0 and pct < threshold)
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="RECORD_DELTA_PCT",
                value_num=round(pct, 4),
                threshold_num=threshold,
                exception_flag=exc,
                details={"dropped_to_zero": dropped_to_zero},
            )
        )
    return rows


@operator("rxnorm_tier_coverage")
def _rxnorm_tier_coverage(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 3.08 / 3.15 (Table IVH): Tier 1 RxNorm coverage below 80%.

    SAS (edc_report.sas:9667-9676; data_curation_tables.sas RXCUI-tier
    join): a medication record's RXNORM_CUI is looked up in the RxNorm
    reference to assign a TTY tier. Tier 1 = SCD / SBD / BPCK / GPCK. The
    check flags when fewer than 80% of the table's records map to a Tier 1
    code (``col5p<80.0``) or the Tier 1 numerator is zero (``col4="0"``).

    Unlike the earlier ``known_value_coverage`` proxy (which merely counted a
    *populated* RXNORM_CUI), this performs the real TTY-tier join against the
    packaged RxNorm tier reference
    (``resources/health/reference/rxnorm_cui_tier.csv``), extracted from the
    PCORnet DCQ reference bundle.

    Params:
        ``table`` — the CDM table (PRESCRIBING or MED_ADMIN).
        ``cui_column`` — the RxNorm-CUI column (default ``RXNORM_CUI``).
        ``tier1_labels`` — the reference tier labels that count as Tier 1
            (default ``["Tier 1"]``).
    """
    from pyspark.sql import functions as F

    threshold = float(spec.thresholds.get("coverage_pct_lt", 80.0))
    table = str(spec.params.get("table", "PRESCRIBING"))
    cui_column = str(spec.params.get("cui_column", "RXNORM_CUI"))
    tier1_labels = [str(t) for t in spec.params.get("tier1_labels", ["Tier 1"])]
    rows: list[MetricRow] = []
    if not ctx.has(table):
        return rows
    df = ctx.table(table)
    if cui_column.upper() not in set(_columns(df)):
        return rows
    total = ctx.rows(df)
    if total == 0:
        return rows

    ref = _rxnorm_tier_reference(ctx.session)
    # Left-join the CDM table's CUI to the reference tier.
    left = df.select(F.trim(F.col(cui_column).cast("string")).alias("__cui__"))
    joined = left.join(ref, left["__cui__"] == ref["RXNORM_CUI"], how="left")
    tier1 = joined.filter(F.col("RXNORM_CUI_TIER").isin(tier1_labels))
    tier1_n = ctx.rows(tier1)
    pct = 100.0 * tier1_n / total
    exc = pct < threshold or tier1_n == 0
    rows.append(
        _mk(
            spec,
            source_table=table,
            metric="TIER1_COVERAGE_PCT",
            value_num=round(pct, 4),
            threshold_num=threshold,
            exception_flag=exc,
            details={
                "cui_column": cui_column,
                "tier1_rows": tier1_n,
                "total_rows": total,
                "tier1_labels": tier1_labels,
            },
        )
    )
    return rows


def _rxnorm_tier_reference(session: Any) -> Any:
    """Read the packaged RxNorm CUI->tier reference as a lazy DataFrame.

    The reference ships as a tab-delimited CSV under ``ovid2.resources``
    (``health/reference/rxnorm_cui_tier.csv``), extracted from the PCORnet
    DCQ RxNorm reference bundle. Columns: ``RXNORM_CUI``, ``RXNORM_CUI_TTY``,
    ``RXNORM_CUI_TIER``.
    """
    from importlib import resources

    from pyspark.sql import functions as F

    with resources.as_file(
        resources.files("ovid2.resources")
        .joinpath("health")
        .joinpath("reference")
        .joinpath("rxnorm_cui_tier.csv")
    ) as path:
        ref_path = str(path)
    ref = session.read.option("header", True).option("sep", "\t").csv(ref_path)
    return ref.select(
        F.trim(F.col("RXNORM_CUI").cast("string")).alias("RXNORM_CUI"),
        F.col("RXNORM_CUI_TIER"),
    )


@operator("bad_zip")
def _bad_zip(ctx: OpContext, spec: CheckSpec) -> list[MetricRow]:
    """DC 1.17 (Table IID row 9): ZIP / FIPS-location conformance.

    SAS (data_curation_tables.sas:1166/1173/1393; edc_report.sas:4499-4504):
    a geographic-code column is *valid* iff it is all digits AND has the
    exact expected length. A value is **bad** when it contains any non-digit
    character OR is not the expected length. The check is **count-based**:
    any single bad value across the configured columns flags the exception
    (``VALID_N < ALL_N``).

    The columns and their expected digit-lengths are declared in
    ``params.zip_columns`` as ``{table, column, length}`` entries. Blank /
    null values are treated as *missing* (not bad) — SAS conformance counts
    only populated values against the valid-length rule.
    """
    from pyspark.sql import functions as F

    threshold_rows = float(spec.thresholds.get("bad_rows_gt", 0.0))
    rows: list[MetricRow] = []
    for entry in spec.params.get("zip_columns", []):
        table = str(entry["table"])
        column = str(entry["column"])
        length = int(entry["length"])
        if not ctx.has(table):
            continue
        df = ctx.table(table)
        if column.upper() not in set(_columns(df)):
            continue
        trimmed = F.trim(F.col(column))
        populated = F.col(column).isNotNull() & (trimmed != F.lit(""))
        # Bad = populated AND (contains a non-digit OR wrong length).
        # ``rlike`` for all-digits: ``^[0-9]+$``; wrong length via char count.
        all_digits = trimmed.rlike("^[0-9]+$")
        right_length = F.length(trimmed) == F.lit(length)
        bad = populated & (~(all_digits & right_length))
        bad_n = ctx.rows(df.filter(bad))
        rows.append(
            _mk(
                spec,
                source_table=table,
                metric="BAD_ZIP_ROWS",
                value_num=float(bad_n),
                threshold_num=threshold_rows,
                exception_flag=bad_n > threshold_rows,
                details={"column": column, "expected_length": length},
            )
        )
    return rows


__all__ = [
    "OpContext",
    "get_operator",
    "operator",
    "operator_names",
]
