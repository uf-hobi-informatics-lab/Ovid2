"""Per-pipeline transform *assignments* — which chains run on which table.

A pipeline assigns reusable transform chains (its own, the user library's,
or the shipped library's — see :mod:`ovid2.transforms.spec`) to tables in
``pipelines/ovid2_pipelines/<pipeline>/transforms/assignments.yaml``::

    v: 1
    tables:
      "*":                       # every table, first
        - spec: clean_whitespace_and_nulls
      demographic:
        - spec: normalise_sex
        - steps:                 # an inline, anonymous chain
            - op: derive_column
              column: SEX
              expr: "coalesce(nullif(trim(SEX), ''), 'UN')"

The ``transform`` pipeline stage (:mod:`ovid2.transformations.transform`)
runs the assigned chains between deduplicate and map. This module only
loads and validates the file (pure Python; no engine imports), so the web
tier, MCP validator and executor share it. It also owns
:func:`user_library_dir`, the user transform library location.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, cast

__all__ = [
    "ASSIGNMENTS_FILE",
    "Assignment",
    "AssignmentsError",
    "assignments_path",
    "load_assignments",
    "parse_assignments",
    "table_assignments",
    "user_library_dir",
]

#: File name under the pipeline package's ``transforms/`` directory.
ASSIGNMENTS_FILE = "assignments.yaml"

_SCHEMA: dict[str, Any] = {}


def user_library_dir() -> Path:
    """Return the user transform-library directory (may not exist yet).

    ``$OVID2_TRANSFORMS_DIR`` when set, else
    ``$XDG_CONFIG_HOME/ovid2/transforms``. Run containers see it through
    the read-only XDG config mount.
    """
    import os

    override = os.environ.get("OVID2_TRANSFORMS_DIR")
    if override:
        return Path(override)
    from ovid2.config.paths import config_dir

    return config_dir() / "transforms"


class AssignmentsError(ValueError):
    """Raised when an assignments file fails validation."""


@dataclass(frozen=True)
class Assignment:
    """One chain assigned to a table.

    Attributes:
        spec: Named chain (``None`` for an inline chain).
        steps: Inline steps (``None`` for a named chain).
        enabled: When ``False`` the entry is skipped.
    """

    spec: str | None = None
    steps: tuple[dict[str, Any], ...] | None = None
    enabled: bool = True

    @property
    def label(self) -> str:
        """Return a short display label."""
        return self.spec or f"inline ({len(self.steps or ())} steps)"


def _schema() -> dict[str, Any]:
    if not _SCHEMA:
        ref = resources.files("ovid2.resources.schemas") / "transform_assignments.v1.json"
        with ref.open("r", encoding="utf-8") as fh:
            _SCHEMA.update(json.load(fh))
    return _SCHEMA


def parse_assignments(raw: Any, *, origin: str = "<memory>") -> dict[str, list[Assignment]]:
    """Validate and parse a raw assignments document.

    Args:
        raw: The YAML-decoded document.
        origin: Label for error messages.

    Returns:
        ``{table_or_*: [Assignment, …]}``.

    Raises:
        AssignmentsError: On a schema violation.
    """
    from jsonschema import Draft202012Validator

    if not isinstance(raw, dict):
        raise AssignmentsError(f"{origin}: assignments must be a mapping")
    errors = sorted(Draft202012Validator(_schema()).iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(
            f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise AssignmentsError(f"{origin}: {detail}")
    out: dict[str, list[Assignment]] = {}
    for table, entries in cast("dict[str, list[dict[str, Any]]]", raw["tables"]).items():
        out[table] = [
            Assignment(
                spec=e.get("spec"),
                steps=tuple(e["steps"]) if "steps" in e else None,
                enabled=bool(e.get("enabled", True)),
            )
            for e in entries
        ]
    return out


def assignments_path(pipeline: str) -> Path | None:
    """Return ``<pipeline pkg>/transforms/assignments.yaml`` if it exists."""
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return None
    for base in getattr(pkg, "__path__", []) or []:
        candidate = Path(base) / "transforms" / ASSIGNMENTS_FILE
        if candidate.is_file():
            return candidate
    return None


def load_assignments(pipeline: str) -> dict[str, list[Assignment]]:
    """Return the pipeline's parsed assignments (empty when none declared)."""
    path = assignments_path(pipeline)
    if path is None:
        return {}
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_assignments(raw, origin=str(path))


def table_assignments(assignments: dict[str, list[Assignment]], table: str) -> list[Assignment]:
    """Return the enabled chains for ``table`` (``*`` entries first)."""
    return [a for a in [*assignments.get("*", []), *assignments.get(table, [])] if a.enabled]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.transform_assignments is a library module.")
