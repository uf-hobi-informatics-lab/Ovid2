"""Ovid2 — data processing engine for healthcare pipeline data.

Transforms pipeline data into PCORnet CDM v7.0 and OMOP CDM 5.3, uploaded to Snowflake.

The public API is intentionally small: users interact through the ``ovid2`` CLI
(:mod:`ovid2.cli`) or by launching the TUI viewer via ``ovid2-watch``
(:mod:`ovid2.display.renderers.tui`).

See ``AGENTS.md`` and ``docs/`` for architectural details.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__: str = "0.1.0"
