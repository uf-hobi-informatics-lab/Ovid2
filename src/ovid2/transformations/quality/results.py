"""Data-health check result model.

A single :class:`MetricRow` mirrors one row of the Snowflake port's
``DCQ_RESULTS`` table so results can be diffed against the already-verified
reference implementation at
the PCORnet characterization-on-Snowflake runner.

:class:`CheckOutcome` bundles the metric rows a single check produced with
its terminal status, ready to be (a) written as a durable table to the
output storage account and (b) folded into the per-run JSON report artifact
the web viewer reads.

This module is pure data — no engine imports — so it is safe to import from
anywhere (including the collector that builds the JSON artifact).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

CheckStatus = Literal["passed", "exception", "error", "skipped", "not_applicable"]

# Column order for the durable ``dcq_results`` metric-row table. Kept
# byte-comparable with the Snowflake port's DCQ_RESULTS schema.
METRIC_ROW_COLUMNS: tuple[str, ...] = (
    "RUN_ID",
    "CHECK_ID",
    "CHECK_NAME",
    "PART",
    "EDC_TABLE",
    "SOURCE_TABLE",
    "METRIC",
    "VALUE_NUM",
    "VALUE_STR",
    "THRESHOLD_NUM",
    "EXCEPTION_FLAG",
    "DETAILS",
)


@dataclass(frozen=True)
class MetricRow:
    """One metric emitted by a check.

    Attributes:
        check_id: Stable check id (e.g. ``'1.01'`` / ``'health.orphan_fk'``).
        check_name: Machine name (e.g. ``'DC_1_01'``).
        part: Grouping bucket (``part1`` … ``semantic``).
        edc_table: EDC report table this check populates (may be blank).
        source_table: CDM table the metric is about (``None`` for
            cross-table metrics).
        metric: Metric name (e.g. ``'ROW_COUNT'``).
        value_num: Numeric value, when the metric is numeric.
        value_str: String value, when the metric is categorical.
        threshold_num: Threshold applied to decide ``exception_flag``.
        exception_flag: ``True`` when this metric breached its threshold.
        details: Free-form JSON-serialisable detail (column name, etc.).
    """

    check_id: str
    check_name: str
    part: str
    edc_table: str
    source_table: str | None
    metric: str
    value_num: float | None = None
    value_str: str | None = None
    threshold_num: float | None = None
    exception_flag: bool = False
    details: dict[str, Any] | None = None

    def as_table_row(self, run_id: str) -> tuple[Any, ...]:
        """Return this metric as an ordered tuple for the durable table.

        The order matches :data:`METRIC_ROW_COLUMNS`; ``DETAILS`` is
        serialised to a JSON string so it round-trips through a CSV / SQL
        table cleanly.
        """
        import json

        details_str = json.dumps(self.details, sort_keys=True) if self.details else None
        return (
            run_id,
            self.check_id,
            self.check_name,
            self.part,
            self.edc_table,
            self.source_table,
            self.metric,
            self.value_num,
            self.value_str,
            self.threshold_num,
            self.exception_flag,
            details_str,
        )

    def as_report_dict(self) -> dict[str, Any]:
        """Return the compact form embedded in the JSON report artifact."""
        return {
            "source_table": self.source_table,
            "metric": self.metric,
            "value_num": self.value_num,
            "value_str": self.value_str,
            "threshold_num": self.threshold_num,
            "exception_flag": self.exception_flag,
            "details": self.details,
        }


@dataclass
class CheckOutcome:
    """The result of running one check.

    Attributes:
        check_id: Stable check id.
        check_name: Machine name.
        part: Grouping bucket.
        edc_table: EDC report table.
        description: One-line description (copied from the spec).
        doc_interpretation: How to read an exception (from the spec).
        status: Terminal status.
        rows: Metric rows produced.
        error: Error message when ``status == 'error'``.
    """

    check_id: str
    check_name: str
    part: str
    edc_table: str
    description: str
    doc_interpretation: str = ""
    status: CheckStatus = "passed"
    rows: list[MetricRow] = field(default_factory=list)
    error: str | None = None

    @property
    def exception_count(self) -> int:
        """Number of metric rows that breached their threshold."""
        return sum(1 for r in self.rows if r.exception_flag)

    def resolve_status(self) -> None:
        """Set :attr:`status` from the metric rows (unless already errored)."""
        if self.status in ("error", "skipped", "not_applicable"):
            return
        self.status = "exception" if self.exception_count > 0 else "passed"

    def as_report_dict(self) -> dict[str, Any]:
        """Return the per-check block for the JSON report artifact."""
        return {
            "id": self.check_id,
            "name": self.check_name,
            "part": self.part,
            "edc_table": self.edc_table,
            "description": self.description,
            "status": self.status,
            "exception_count": self.exception_count,
            "metric_count": len(self.rows),
            "error": self.error,
            "doc_interpretation": self.doc_interpretation,
            "metrics": [r.as_report_dict() for r in self.rows],
        }


__all__ = [
    "METRIC_ROW_COLUMNS",
    "CheckOutcome",
    "CheckStatus",
    "MetricRow",
    "asdict",
]
