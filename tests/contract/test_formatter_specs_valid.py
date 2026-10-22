"""Contract: every discoverable formatter spec is schema-valid.

Scans every ``ovid2_pipelines/*/formatters/*.yaml`` reachable from the
current ``sys.path`` and validates each against the bundled JSON
schema. Also loads every module of the form
``ovid2_pipelines.<pipeline>.formatters.<table>`` that exposes ``SPEC``
and validates it too.

During Phase 4 the only pipeline on the path is the fixture
``testpipeline``; once real pipelines are migrated (P7) they are
covered automatically.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sys
from importlib import resources
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

from jsonschema import Draft202012Validator


def _schema() -> dict:
    ref = resources.files("ovid2.resources.schemas") / "formatter_spec.v1.json"
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _pipeline_dirs() -> list[Path]:
    """Every filesystem directory that hosts ``ovid2_pipelines.*`` subpackages."""
    try:
        import ovid2_pipelines  # noqa: F401
    except ModuleNotFoundError:
        return []
    module = sys.modules["ovid2_pipelines"]
    return [Path(p) for p in getattr(module, "__path__", [])]


def _iter_pipeline_specs() -> list[tuple[str, str, dict, str]]:
    """Return every discoverable spec as ``(pipeline, table, raw, origin)``."""
    found: list[tuple[str, str, dict, str]] = []
    for root in _pipeline_dirs():
        for pipeline_dir in sorted(
            p for p in root.iterdir() if p.is_dir() and (p / "__init__.py").is_file()
        ):
            pipeline = pipeline_dir.name
            formatters_dir = pipeline_dir / "formatters"
            if not formatters_dir.is_dir():
                continue
            for yaml_path in sorted(formatters_dir.glob("*.yaml")):
                import yaml

                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
                found.append((pipeline, yaml_path.stem, raw, str(yaml_path)))
    return found


def _iter_python_specs() -> list[tuple[str, str, dict, str]]:
    """Return every ``SPEC``-exposing module under ``ovid2_pipelines.<p>.formatters``."""
    try:
        pipelines_pkg = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []

    found: list[tuple[str, str, dict, str]] = []
    for _, pipeline_name, is_pkg in pkgutil.iter_modules(pipelines_pkg.__path__, "ovid2_pipelines."):
        if not is_pkg:
            continue
        try:
            formatters_pkg = importlib.import_module(f"{pipeline_name}.formatters")
        except ModuleNotFoundError:
            continue
        for _, module_name, is_sub in pkgutil.iter_modules(
            formatters_pkg.__path__, f"{pipeline_name}.formatters."
        ):
            if is_sub:
                continue
            module = importlib.import_module(module_name)
            raw = getattr(module, "SPEC", None)
            if raw is None:
                continue
            pipeline = pipeline_name.rsplit(".", 1)[-1]
            table = module_name.rsplit(".", 1)[-1]
            found.append((pipeline, table, raw, module_name))
    return found


_ALL_SPECS = _iter_pipeline_specs() + _iter_python_specs()


@pytest.mark.contract
@pytest.mark.parametrize(
    ("pipeline", "table", "raw", "origin"),
    _ALL_SPECS,
    ids=[f"{p}/{t}" for p, t, _, _ in _ALL_SPECS] if _ALL_SPECS else ["<none-discovered>"],
)
def test_pipeline_spec_validates(pipeline: str, table: str, raw: dict, origin: str) -> None:
    """Every YAML / Python spec on the search path validates against the schema."""
    if not raw:
        pytest.skip("No specs on the search path yet — pipelines not migrated.")

    validator = Draft202012Validator(_schema())
    errors = sorted(validator.iter_errors(raw), key=lambda e: e.path)
    assert not errors, (
        f"Spec {pipeline}/{table} at {origin} failed schema validation:\n"
        + "\n".join(
            f"  {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors
        )
    )
