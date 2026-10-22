"""Fixers — declarative fix chains + shared common-fix implementations.

Per-pipeline fix chains live in ``pipelines/<p>/fixer_configs.py`` as declarative
dicts. Implementations live in :mod:`ovid2.transformations.fixers.common`.
Implemented in P2.
"""

from __future__ import annotations

__all__: list[str] = []
