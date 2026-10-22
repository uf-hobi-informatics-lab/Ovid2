"""Pipeline transformations.

Transformers implement ``transform(df, context) -> DataFrame`` and are composed
in a fixed order by :mod:`ovid2.core.pipeline`:

    read → format → deduplicate → map → fix → write

Sub-packages:

* :mod:`ovid2.transformations.deduplicators` — skew-relief salted dedup.
* :mod:`ovid2.transformations.mappers`       — value-dictionary mappers per CDM family.
* :mod:`ovid2.transformations.fixers`        — declarative fix chains + common fixers.
* :mod:`ovid2.transformations.analysis`      — analysis jobs (monthly counts, …).
* :mod:`ovid2.transformations.quality`       — DQ checks (mapping gap, mapping report, …).

Implemented over Phases 2 and 5.
"""

from __future__ import annotations

__all__: list[str] = []
