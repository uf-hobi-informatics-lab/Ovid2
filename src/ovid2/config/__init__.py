"""Runtime configuration and JSON-schema loaders.

* :mod:`ovid2.config.settings`  — global defaults (Spark tuning, paths, salts).
* :mod:`ovid2.config.addresses` — 4-tier resolver for I/O address JSON configs.
* :mod:`ovid2.config.schemas`   — access to bundled JSON Schemas
  (formatter spec, event log, address config).

Implemented over Phase 2.
"""

from __future__ import annotations

__all__: list[str] = []
