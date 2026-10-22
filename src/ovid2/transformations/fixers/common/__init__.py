"""Shared common fixers.

Each fix is a ``BaseFixer`` subclass registered in ``registry.py`` under a
canonical name (``patid_orphans``, ``race_eth``, ``loinc_mover``, …). Pipeline
``fixer_configs.py`` files reference these by name.

Ported from the April 2026 refactor's ``transformations/fixers/common/``.
Implemented in P2.
"""

from __future__ import annotations

__all__: list[str] = []
