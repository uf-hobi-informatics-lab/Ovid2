"""Mapper spec dataclass, loader, and validator.

A mapper spec declares, per pipeline and per table, an ordered list of
:class:`MapOp` invocations. The engine (:mod:`ovid2.mappers.engine`)
walks the list and applies each op lazily.

Two loading modes:

- YAML at ``pipelines/ovid2_pipelines/<pipeline>/mappers/<table>.yaml``
  (recommended, symmetrical with formatter specs).
- Python dict at ``ovid2_pipelines.<pipeline>.mappers.<table>.SPEC``
  (for pipelines that need computed specs).

The escape hatch is ``class_path`` — set it to a dotted path to a
:class:`~ovid2.transformations.mappers.base.BaseMapper` subclass and
the engine skips the op list entirely. This preserves backwards
compatibility with any legacy mapper class a pipeline ports over.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

_SCHEMA_CACHE: dict[str, Any] = {}
_DEPRECATION_LOGGER = logging.getLogger("ovid2.mappers.deprecations")


@dataclass(frozen=True)
class MapperOpSpec:
    """One entry in the mapper spec's ``ops:`` list.

    Attributes:
        op: Op name (e.g. ``'translate_by_dict'``). Resolved against
            :data:`ovid2.mappers.registry.MAP_OPS`.
        params: Op-specific parameters. Validated against the op's
            schema fragment by :func:`ovid2.mappers.engine.run`.
    """

    op: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MapperSpec:
    """One pipeline's mapping spec for one CDM table.

    Attributes:
        v: Schema version. Bumped on breaking changes.
        pipeline: Pipeline name (e.g. ``'demo'``).
        table: Target CDM table (e.g. ``'demographic'``).
        enabled: When ``False`` the map stage skips this table.
        ops: Ordered list of ops to apply.
        class_path: Escape hatch — dotted path to a legacy
            :class:`~ovid2.transformations.mappers.base.BaseMapper`
            subclass. When set, ``ops`` MUST be empty; the engine hands
            off to the class instead of walking the op list.
        source_path: On-disk location of the spec (``None`` for
            in-memory specs).
    """

    v: int
    pipeline: str
    table: str
    enabled: bool = True
    ops: tuple[MapperOpSpec, ...] = ()
    class_path: str | None = None
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MapperSpecValidationError(ValueError):
    """Raised when a mapper spec fails JSON-schema or semantic validation."""


class MapperSpecNotFoundError(FileNotFoundError):
    """Raised when no spec can be located for ``(pipeline, table)``."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    """Load (and cache) the bundled mapper-spec JSON schema."""
    if "mapper" not in _SCHEMA_CACHE:
        ref = resources.files("ovid2.resources.schemas") / "mapper_spec.v1.json"
        with ref.open("r", encoding="utf-8") as fh:
            _SCHEMA_CACHE["mapper"] = json.load(fh)
    return _SCHEMA_CACHE["mapper"]


def _validate_raw(raw: dict[str, Any], *, origin: str) -> None:
    """Validate ``raw`` against the JSON schema."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover — dev dep
        raise MapperSpecValidationError("jsonschema is required to validate specs.") from exc

    validator = Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(raw), key=lambda e: e.path)
    if errors:
        message = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise MapperSpecValidationError(
            f"Mapper spec at {origin} failed schema validation:\n{message}"
        )


def _build(raw: dict[str, Any], *, pipeline: str, source_path: Path | None) -> MapperSpec:
    """Convert a validated raw dict into a :class:`MapperSpec`."""
    ops = tuple(
        MapperOpSpec(op=str(entry["op"]), params={k: v for k, v in entry.items() if k != "op"})
        for entry in raw.get("ops", [])
    )
    return MapperSpec(
        v=int(raw["v"]),
        pipeline=pipeline,
        table=str(raw["table"]),
        enabled=bool(raw.get("enabled", True)),
        ops=ops,
        class_path=raw.get("class"),
        source_path=source_path,
    )


def load_from_dict(raw: dict[str, Any], *, pipeline: str, origin: str = "<memory>") -> MapperSpec:
    """Validate and materialise a mapper spec from an in-memory dict."""
    _validate_raw(raw, origin=origin)
    return _build(raw, pipeline=pipeline, source_path=None)


def load_from_yaml(path: Path, *, pipeline: str) -> MapperSpec:
    """Load a mapper spec from a YAML file."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover — required dep
        raise MapperSpecValidationError("PyYAML is required to load YAML specs.") from exc

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise MapperSpecValidationError(f"Mapper spec at {path} must be a mapping at the top level.")
    _validate_raw(raw, origin=str(path))
    return _build(raw, pipeline=pipeline, source_path=path)


def find_spec(pipeline: str, table: str) -> Path | None:
    """Locate the YAML spec for ``(pipeline, table)``.

    Search order:

    1. ``ovid2_pipelines.<pipeline>.mappers`` package (looks for
       ``<table>.yaml`` next to ``__init__.py``).

    Returns ``None`` when no spec exists — the caller should fall back
    to the legacy class path (:mod:`ovid2.transformations.mappers`).
    """
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}.mappers")
    except ModuleNotFoundError:
        return None

    pkg_paths = getattr(pkg, "__path__", None)
    if not pkg_paths:
        return None

    for base in pkg_paths:
        candidate = Path(base) / f"{table}.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_spec(pipeline: str, table: str) -> MapperSpec:
    """Return the :class:`MapperSpec` for ``(pipeline, table)``.

    Raises:
        MapperSpecNotFoundError: When neither a YAML nor a Python spec
            can be located.
    """
    path = find_spec(pipeline, table)
    if path is not None:
        return load_from_yaml(path, pipeline=pipeline)

    # Python-dict form: ``ovid2_pipelines.<pipeline>.mappers.<table>.SPEC``.
    module_name = f"ovid2_pipelines.{pipeline}.mappers.{table}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        raise MapperSpecNotFoundError(
            f"No mapper spec found for pipeline '{pipeline}', table '{table}'."
        )
    raw = getattr(module, "SPEC", None)
    if not isinstance(raw, dict):
        raise MapperSpecValidationError(
            f"Module '{module_name}' must define a top-level dict named SPEC."
        )
    return load_from_dict(raw, pipeline=pipeline, origin=module_name)
