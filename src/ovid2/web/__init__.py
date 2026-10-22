"""Ovid2 web control panel.

Provides a FastAPI + HTMX interface for observing and (in later phases)
driving the Ovid2 pipeline. The web app is layered on top of the
existing CLI and event/log surfaces — it never imports pipeline code
(``ovid2.core`` / ``ovid2.transformations`` / ``ovid2.formatters``)
directly. All effects flow through:

- ``ovid2.config.*`` — read address/secret configs from XDG paths.
- ``ovid2.installer.*`` — read-only doctor checks.
- ``ovid2.display.reader`` — tail JSONL event logs.
- ``ovid2.scheduler.batch_tracker`` — canonical batch-status store.
- ``subprocess.Popen(["ovid2", ...])`` — dispatch ETL runs in a
  dedicated worker process (W3+).

Two long-running processes make up the web tier:

- ``ovid2 web`` — FastAPI/uvicorn HTTP server.
- ``ovid2 web-worker`` — SQLite-backed job queue consumer (W3+).

A third process — ``ovid2 mcp`` — wraps the REST API in the
Model-Context-Protocol so any agent/LLM can drive Ovid2.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
