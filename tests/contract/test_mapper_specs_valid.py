"""Contract: every pipeline mapper spec validates against the JSON schema (P10).

Walks every ``pipelines/ovid2_pipelines/*/mappers/*.yaml`` file discovered
on ``sys.path`` and validates it against the mapper-spec schema. Also
covers Python ``SPEC = {...}`` module form.

The test is skipped when no pipeline ships a ``mappers/`` directory yet
(as of P10 landing, no pipeline has migrated). It exists so that the
first mapper spec authored anywhere in the tree is validated
automatically.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

from ovid2.mappers.spec import (
    MapperSpecValidationError,
    load_from_dict,
    load_from_yaml,
)


def _discover_pipeline_packages() -> list[tuple[str, Path]]:
    """Return ``(pipeline_name, package_path)`` for every ovid2_pipelines entry."""
    try:
        root = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []

    results: list[tuple[str, Path]] = []
    for info in pkgutil.iter_modules(root.__path__, prefix="ovid2_pipelines."):
        if not info.ispkg:
            continue
        pipeline_name = info.name.rsplit(".", 1)[-1]
        try:
            pkg = importlib.import_module(info.name)
        except Exception:
            continue
        for base in getattr(pkg, "__path__", []):
            results.append((pipeline_name, Path(base)))
    return results


@pytest.mark.contract
def test_every_mapper_yaml_validates() -> None:
    """Every ``mappers/<table>.yaml`` under a pipeline package must validate."""
    pipelines = _discover_pipeline_packages()
    checked = 0
    failures: list[str] = []

    for pipeline, base in pipelines:
        mappers_dir = base / "mappers"
        if not mappers_dir.is_dir():
            continue
        for yaml_path in mappers_dir.glob("*.yaml"):
            checked += 1
            try:
                load_from_yaml(yaml_path, pipeline=pipeline)
            except MapperSpecValidationError as exc:
                failures.append(f"{yaml_path}: {exc}")

    assert not failures, "Mapper YAML validation failures:\n" + "\n".join(failures)
    # Sentinel: this test always passes when no specs exist. Once specs
    # ship, ``checked`` becomes positive and any regression fails.
    _ = checked


@pytest.mark.contract
def test_every_python_mapper_spec_validates() -> None:
    """Every ``mappers/<table>.py`` with a ``SPEC = {...}`` must validate."""
    pipelines = _discover_pipeline_packages()
    failures: list[str] = []

    for pipeline, _base in pipelines:
        try:
            mappers_pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}.mappers")
        except ModuleNotFoundError:
            continue

        for info in pkgutil.iter_modules(getattr(mappers_pkg, "__path__", [])):
            if info.ispkg:
                continue
            try:
                module = importlib.import_module(f"ovid2_pipelines.{pipeline}.mappers.{info.name}")
            except Exception as exc:
                failures.append(f"{pipeline}.mappers.{info.name}: import failed: {exc}")
                continue
            raw = getattr(module, "SPEC", None)
            if raw is None:
                continue
            try:
                load_from_dict(raw, pipeline=pipeline, origin=module.__name__)
            except MapperSpecValidationError as exc:
                failures.append(f"{module.__name__}: {exc}")

    assert not failures, "Python mapper spec validation failures:\n" + "\n".join(failures)
