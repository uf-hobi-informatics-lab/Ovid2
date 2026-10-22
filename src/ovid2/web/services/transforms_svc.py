"""Public transformations catalogue service (web tier).

Surfaces the CDM-agnostic transformations library to the web panel and
the MCP server. Reads the web-safe catalogue mirror
(:mod:`ovid2.config.transforms_catalogue`) and **never imports pipeline
code** (see ``tests/contract/test_web_no_pipeline_imports.py``).

Powers:

- ``GET /api/v1/transforms`` (list, optional ``?category=`` filter),
- ``POST /api/v1/transforms:validate`` (schema + op-name validation of a
  candidate transform spec before it is saved),
- the ``list_transforms`` / ``validate_transform`` MCP tools.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def list_transforms(category: str | None = None) -> list[dict[str, Any]]:
    """Return the transform catalogue, optionally filtered by ``category``.

    Args:
        category: When set, only transforms in this category are
            returned. Unknown categories yield an empty list.

    Returns:
        A list of ``{name, summary, category, requires_family,
        params_schema}`` dicts.
    """
    from ovid2.config.transforms_catalogue import list_catalogue

    return list_catalogue(category)


def categories() -> list[str]:
    """Return the valid transform categories."""
    from ovid2.config.transforms_catalogue import CATEGORIES

    return list(CATEGORIES)


def _schema() -> dict[str, Any]:
    """Load the packaged transform-spec JSON schema (web-safe)."""
    import ovid2

    root = Path(ovid2.__file__).resolve().parent
    path = root / "resources" / "schemas" / "transform_spec.v1.json"
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_transform_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a candidate transform spec (web-safe, no pipeline import).

    Runs two checks:

    1. JSON-schema validation against ``transform_spec.v1.json``.
    2. Op-name validation — every ``steps[].op`` must be a catalogued
       transform. (The pipeline-tier engine also enforces this, but
       validating here lets an agent catch a bad op *before* saving.)

    Args:
        spec: The candidate spec dict.

    Returns:
        ``{"ok": bool, "errors": list[str]}``.
    """
    from ovid2.config.transforms_catalogue import transform_names

    errors: list[str] = []

    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover — dev dep
        return {"ok": False, "errors": ["jsonschema is not installed on the server."]}

    validator = Draft202012Validator(_schema())
    for err in sorted(validator.iter_errors(spec), key=lambda e: list(e.path)):
        location = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"{location}: {err.message}")

    known = set(transform_names())
    steps = spec.get("steps")
    if isinstance(steps, list):
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            op = step.get("op")
            if isinstance(op, str) and op not in known:
                errors.append(
                    f"steps/{idx}/op: unknown transform '{op}'. "
                    f"See GET /api/v1/transforms for the catalogue."
                )

    return {"ok": not errors, "errors": errors}


__all__ = ["categories", "list_transforms", "validate_transform_spec"]
