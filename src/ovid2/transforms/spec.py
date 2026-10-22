"""Transform spec dataclass, loader, and validator.

A transform spec is a *free-standing, reusable* chain of transform ops.
Unlike a mapper spec, it is not bound to a CDM table — it is a named
recipe that a user or Agent Ovid composes once and can reference from
many places.

Two storage locations are supported (both loaded by :func:`find_spec`):

1. **Shared library** —
   ``src/ovid2/resources/transforms/<name>.yaml``. Reusable across
   every pipeline; ships in the package.
2. **Per-pipeline** —
   ``pipelines/ovid2_pipelines/<pipeline>/transforms/<name>.yaml``
   (or a Python-dict ``SPEC = {...}`` module at
   ``ovid2_pipelines.<pipeline>.transforms.<name>``). Overrides the
   shared library when both define ``<name>``.

The engine (:mod:`ovid2.transforms.engine`) walks ``steps`` and applies
each op lazily.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, cast

_SCHEMA_CACHE: dict[str, Any] = {}


@dataclass(frozen=True)
class TransformStepSpec:
    """One entry in a transform spec's ``steps:`` list.

    Attributes:
        op: Op name resolved against
            :data:`ovid2.transforms.registry.TRANSFORMS`.
        params: Op-specific parameters.
    """

    op: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransformSpec:
    """A named, reusable chain of transform ops.

    Attributes:
        v: Schema version.
        name: Spec identifier (file stem / module name).
        enabled: When ``False`` the chain is a no-op.
        steps: Ordered ops to apply.
        description: Optional human/agent-readable summary.
        source_path: On-disk location (``None`` for in-memory specs).
    """

    v: int
    name: str
    enabled: bool = True
    steps: tuple[TransformStepSpec, ...] = ()
    description: str | None = None
    source_path: Path | None = None


class TransformSpecValidationError(ValueError):
    """Raised when a transform spec fails schema or semantic validation."""


class TransformSpecNotFoundError(FileNotFoundError):
    """Raised when no spec can be located for a ``name``."""


def _schema() -> dict[str, Any]:
    """Load (and cache) the bundled transform-spec JSON schema."""
    if "transform" not in _SCHEMA_CACHE:
        ref = resources.files("ovid2.resources.schemas") / "transform_spec.v1.json"
        with ref.open("r", encoding="utf-8") as fh:
            _SCHEMA_CACHE["transform"] = json.load(fh)
    return cast("dict[str, Any]", _SCHEMA_CACHE["transform"])


def _validate_raw(raw: dict[str, Any], *, origin: str) -> None:
    """Validate ``raw`` against the JSON schema."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover — dev dep
        raise TransformSpecValidationError("jsonschema is required to validate specs.") from exc

    validator = Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        message = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise TransformSpecValidationError(
            f"Transform spec at {origin} failed schema validation:\n{message}"
        )


def _build(raw: dict[str, Any], *, name: str, source_path: Path | None) -> TransformSpec:
    """Convert a validated raw dict into a :class:`TransformSpec`."""
    steps = tuple(
        TransformStepSpec(
            op=str(entry["op"]),
            params={k: v for k, v in entry.items() if k != "op"},
        )
        for entry in raw.get("steps", [])
    )
    return TransformSpec(
        v=int(raw["v"]),
        name=str(raw.get("name", name)),
        enabled=bool(raw.get("enabled", True)),
        steps=steps,
        description=raw.get("description"),
        source_path=source_path,
    )


def load_from_dict(
    raw: dict[str, Any], *, name: str = "<memory>", origin: str = "<memory>"
) -> TransformSpec:
    """Validate and materialise a transform spec from an in-memory dict."""
    _validate_raw(raw, origin=origin)
    return _build(raw, name=name, source_path=None)


def load_from_yaml(path: Path) -> TransformSpec:
    """Load a transform spec from a YAML file."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover — required dep
        raise TransformSpecValidationError("PyYAML is required to load YAML specs.") from exc

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise TransformSpecValidationError(
            f"Transform spec at {path} must be a mapping at the top level."
        )
    _validate_raw(raw, origin=str(path))
    return _build(raw, name=path.stem, source_path=path)


def _shared_dir() -> Path | None:
    """Return the packaged shared-library transforms directory."""
    try:
        ref = resources.files("ovid2.resources.transforms")
    except ModuleNotFoundError:  # pragma: no cover — resources always present
        return None
    path = Path(str(ref))
    return path if path.is_dir() else None


def find_spec(name: str, pipeline: str | None = None) -> Path | None:
    """Locate the YAML spec for ``name``.

    Search order (first hit wins):

    1. Per-pipeline: ``ovid2_pipelines.<pipeline>.transforms/<name>.yaml``.
    2. Shared library: ``ovid2.resources.transforms/<name>.yaml``.

    Returns ``None`` when no YAML spec exists (a Python-dict ``SPEC``
    module may still exist — see :func:`load_spec`).
    """
    if pipeline is not None:
        try:
            pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}.transforms")
        except ModuleNotFoundError:
            pkg = None
        if pkg is not None:
            for base in getattr(pkg, "__path__", []) or []:
                candidate = Path(base) / f"{name}.yaml"
                if candidate.is_file():
                    return candidate

    shared = _shared_dir()
    if shared is not None:
        candidate = shared / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    return None


def load_spec(name: str, pipeline: str | None = None) -> TransformSpec:
    """Return the :class:`TransformSpec` for ``name``.

    Raises:
        TransformSpecNotFoundError: When neither a YAML nor a Python
            spec can be located.
    """
    path = find_spec(name, pipeline)
    if path is not None:
        return load_from_yaml(path)

    if pipeline is not None:
        module_name = f"ovid2_pipelines.{pipeline}.transforms.{name}"
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            module = None
        if module is not None:
            raw = getattr(module, "SPEC", None)
            if not isinstance(raw, dict):
                raise TransformSpecValidationError(
                    f"Module '{module_name}' must define a top-level dict named SPEC."
                )
            return load_from_dict(raw, name=name, origin=module_name)

    raise TransformSpecNotFoundError(
        f"No transform spec found for name '{name}'"
        + (f" (pipeline '{pipeline}')." if pipeline else ".")
    )


__all__ = [
    "TransformSpec",
    "TransformSpecNotFoundError",
    "TransformSpecValidationError",
    "TransformStepSpec",
    "find_spec",
    "load_from_dict",
    "load_from_yaml",
    "load_spec",
]
