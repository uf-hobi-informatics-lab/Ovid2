"""Analysis transformers.

Ported and refined from the legacy Ovid ``pipelines/analysis.py``. Every
analysis transformer is a pass-through: it computes and emits summary
statistics as log events, then returns the DataFrame unchanged so the
main pipeline continues.

Modules:

- :mod:`ovid2.transformations.analysis.monthly_counts`   — group by yyyy-MM.
- :mod:`ovid2.transformations.analysis.distinct_values`  — per-column distinct counts.
"""

from __future__ import annotations

from ovid2.transformations.analysis.distinct_values import (
    DistinctValuesTransformer,
    RawDistinctValuesTransformer,
)
from ovid2.transformations.analysis.monthly_counts import MonthlyCountsTransformer

__all__ = [
    "DistinctValuesTransformer",
    "MonthlyCountsTransformer",
    "RawDistinctValuesTransformer",
]
