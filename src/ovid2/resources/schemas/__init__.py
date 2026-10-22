"""JSON Schemas bundled with the package.

Access via :func:`importlib.resources.files`::

    from importlib import resources
    schema_ref = resources.files("ovid2.resources.schemas") / "events.v1.json"
    with schema_ref.open() as fh:
        schema = json.load(fh)
"""

from __future__ import annotations

__all__: list[str] = []
