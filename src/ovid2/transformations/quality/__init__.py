"""Data-quality (DQ) checks.

Ports of the checks from the legacy Ovid ``pipelines/dq_checks.py``:

- :class:`MappingGapTransformer` — find values not covered by a pipeline
  dictionary.
- :class:`MappingReportTransformer` — export the pipeline's dictionaries
  as a Spark DataFrame / temp view.
- The **data-health** subsystem (registry + ops + runner + report) ports
  the PCORnet EDC / Data Curation Query (DCQ) checks plus Ovid2-native
  health panels. Declarative check specs live under
  ``resources/health/checks/``; :class:`HealthCheckRunner` executes them
  and produces a durable metric-row table + a JSON report artifact. This
  fills the seam previously reserved for ``DcChecksTransformer``.
"""

from __future__ import annotations

from ovid2.transformations.quality.mapping_gap import MappingGapTransformer
from ovid2.transformations.quality.mapping_report import MappingReportTransformer
from ovid2.transformations.quality.runner import HealthCheckRunner

__all__ = ["HealthCheckRunner", "MappingGapTransformer", "MappingReportTransformer"]
