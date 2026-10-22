"""Bundled resources (JSON Schemas, templates, log4j properties, …).

Anything the runtime needs to open with :func:`importlib.resources.files`
lives here so it ships inside the installed wheel.
"""

from __future__ import annotations

__all__: list[str] = []
