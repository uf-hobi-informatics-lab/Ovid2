"""Value-dictionary mappers, grouped by CDM family.

Sub-packages:

* ``pcornet`` — 26 mappers for PCORnet CDM v7.0 (most pipelines).
* ``nursing`` — nursing-specific mappers (nursing-shaped feeds).
* ``tumor``   — tumor registry mapper (tumor-registry feeds).
* ``veritas`` — Veritas mappers (Veritas-shaped feeds).

Selected at runtime by ``pipeline_settings.mappers_type``. Implemented in P2.
"""

from __future__ import annotations

__all__: list[str] = []
