"""Formatter spec loader and dataclass model.

Loads a pipeline formatter from YAML (or a Python ``spec(...)`` factory),
validates it against ``resources/schemas/formatter_spec.v1.json``, and
returns a fully typed :class:`FormatterSpec` dataclass tree that the
engine can execute.

Two lookup mechanisms:

- **YAML on disk**: ``ovid2_pipelines/<pipeline>/formatters/<table>.yaml``
- **Python factory**: ``ovid2_pipelines/<pipeline>/formatters/<table>.py``
  exposing a module-level ``SPEC`` produced by :func:`spec`.
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
_DEPRECATION_LOGGER = logging.getLogger("ovid2.formatters.deprecations")


# ---------------------------------------------------------------------------
# Dataclass model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    """A single named source block inside a formatter spec."""

    name: str
    glob: str
    exclude: tuple[str, ...] = ()
    optional: bool = False
    filter: str | None = None
    lowercase_columns: bool = True


@dataclass(frozen=True)
class JoinSpec:
    """One join in the spec's ``joins:`` list."""

    with_: str
    on: str
    how: str
    prefix: str
    domain_filter: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubSpecRef:
    """A reference to another spec, used by ``union_of_specs``."""

    spec: str


@dataclass(frozen=True)
class FormatterSpec:
    """Fully-validated, pipeline-scoped formatter definition."""

    v: int
    pipeline: str
    formatter: str
    adapter: str
    enabled: bool = True
    input_file_name: str | None = None
    auxiliary_tables: tuple[str, ...] = ()
    sources: dict[str, SourceSpec] = field(default_factory=dict)
    joins: tuple[JoinSpec, ...] = ()
    projections: dict[str, str] = field(default_factory=dict)
    post_expressions: tuple[str, ...] = ()
    output_columns_from: str | None = None
    sub_specs: tuple[SubSpecRef, ...] = ()
    class_path: str | None = None
    source_path: Path | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SpecValidationError(ValueError):
    """Raised when a spec fails JSON-schema or semantic validation."""


class SpecNotFoundError(FileNotFoundError):
    """Raised when no spec can be located for ``(pipeline, table)``."""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _schema() -> dict[str, Any]:
    """Load (and cache) the bundled formatter-spec JSON schema."""
    if "formatter" not in _SCHEMA_CACHE:
        ref = resources.files("ovid2.resources.schemas") / "formatter_spec.v1.json"
        with ref.open("r", encoding="utf-8") as fh:
            _SCHEMA_CACHE["formatter"] = json.load(fh)
    return _SCHEMA_CACHE["formatter"]


def _validate_raw(raw: dict[str, Any], *, origin: str) -> None:
    """Validate ``raw`` against the JSON schema."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover — dev dep
        raise SpecValidationError("jsonschema is required to validate specs.") from exc

    validator = Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(raw), key=lambda e: e.path)
    if errors:
        message = "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
        raise SpecValidationError(f"Spec at {origin} failed schema validation:\n{message}")


def _build(raw: dict[str, Any], *, pipeline: str, source_path: Path | None) -> FormatterSpec:
    """Convert a validated raw dict into a :class:`FormatterSpec`."""
    adapter_name = str(raw["adapter"])
    if adapter_name == "pcornet_passthrough":
        origin = str(source_path) if source_path is not None else "<memory>"
        _DEPRECATION_LOGGER.warning(
            "Adapter 'pcornet_passthrough' is deprecated (P10) at %s; "
            "rename to 'source_passthrough' — the behaviour is identical.",
            origin,
        )

    sources_raw = raw.get("sources") or {}
    sources = {
        name: SourceSpec(
            name=name,
            glob=cfg["glob"],
            exclude=tuple(cfg.get("exclude", ())),
            optional=bool(cfg.get("optional", False)),
            filter=cfg.get("filter"),
            lowercase_columns=bool(cfg.get("lowercase_columns", True)),
        )
        for name, cfg in sources_raw.items()
    }

    joins = tuple(
        JoinSpec(
            with_=cfg["with"],
            on=cfg["on"],
            how=str(cfg.get("how", "left")),
            prefix=cfg["prefix"],
            domain_filter=tuple(cfg.get("domain_filter", ())),
        )
        for cfg in raw.get("joins", [])
    )

    sub_specs = tuple(SubSpecRef(spec=cfg["spec"]) for cfg in raw.get("sub_specs", []))

    return FormatterSpec(
        v=int(raw["v"]),
        pipeline=pipeline,
        formatter=str(raw["formatter"]),
        adapter=str(raw["adapter"]),
        enabled=bool(raw.get("enabled", True)),
        input_file_name=raw.get("input_file_name"),
        auxiliary_tables=tuple(raw.get("auxiliary_tables", [])),
        sources=sources,
        joins=joins,
        projections=dict(raw.get("projections", {})),
        post_expressions=tuple(raw.get("post_expressions", [])),
        output_columns_from=raw.get("output_columns_from"),
        sub_specs=sub_specs,
        class_path=raw.get("class"),
        source_path=source_path,
    )


def load_from_dict(raw: dict[str, Any], *, pipeline: str, origin: str = "<memory>") -> FormatterSpec:
    """Validate and materialise a spec from an in-memory dict."""
    _validate_raw(raw, origin=origin)
    return _build(raw, pipeline=pipeline, source_path=None)


def load_from_yaml(path: Path, *, pipeline: str) -> FormatterSpec:
    """Load a spec from a YAML file on disk."""
    import yaml  # deferred: keeps this module importable without PyYAML

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise SpecValidationError(
            f"Spec at {path} must be a YAML mapping, got {type(raw).__name__}."
        )
    _validate_raw(raw, origin=str(path))
    return _build(raw, pipeline=pipeline, source_path=path)


def find_and_load(pipeline: str, table: str) -> FormatterSpec:
    """Locate and load a spec for ``(pipeline, table)``.

    Search order:

    1. ``ovid2_pipelines.<pipeline>.formatters.<table>`` exposes ``SPEC``.
    2. YAML at ``<pipelines-root>/<pipeline>/formatters/<table>.yaml``.

    The pipelines root is discovered by importing the ``ovid2_pipelines``
    namespace package. If it is not on ``sys.path``, only in-repo test
    pipelines are visible.

    Args:
        pipeline: Pipeline name (e.g. ``'demo'``).
        table: CDM table name (e.g. ``'condition'``).

    Returns:
        The validated :class:`FormatterSpec`.

    Raises:
        SpecNotFoundError: Neither a Python spec nor a YAML file exists.
    """
    # 1) Python-factory spec.
    module_name = f"ovid2_pipelines.{pipeline}.formatters.{table}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        module = None

    if module is not None:
        raw = getattr(module, "SPEC", None)
        if raw is None:
            raise SpecValidationError(
                f"Module {module_name} exists but does not expose SPEC = spec(...)."
            )
        return load_from_dict(raw, pipeline=pipeline, origin=module_name)

    # 2) YAML spec.
    try:
        pipelines_pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError as exc:
        raise SpecNotFoundError(
            f"No 'ovid2_pipelines.{pipeline}' package on sys.path — cannot find spec for '{table}'."
        ) from exc

    for base in _pipeline_search_dirs(pipelines_pkg):
        candidate = base / "formatters" / f"{table}.yaml"
        if candidate.is_file():
            return load_from_yaml(candidate, pipeline=pipeline)

    raise SpecNotFoundError(f"No formatter spec found for pipeline '{pipeline}' table '{table}'.")


def _pipeline_search_dirs(pipeline_pkg: Any) -> list[Path]:
    """Return every filesystem directory that hosts the pipeline package."""
    result: list[Path] = []
    for path_str in getattr(pipeline_pkg, "__path__", []):
        result.append(Path(path_str))
    return result


# ---------------------------------------------------------------------------
# Python-factory convenience — mirrors the ``spec(...)`` snippet used by
# pipelines that prefer Python dicts over YAML.
# ---------------------------------------------------------------------------


def spec(**kwargs: Any) -> dict[str, Any]:
    """Return a raw spec dict suitable for :func:`load_from_dict`.

    Injects the required schema-version key so callers don't have to
    repeat it. Every other field is validated by the schema.
    """
    payload: dict[str, Any] = {"v": 1}
    payload.update(kwargs)
    return payload
