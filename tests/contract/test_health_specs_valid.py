"""Contract: every registered health-check spec is valid and runnable.

Enforces the data-health invariants:

- every YAML spec under ``resources/health/checks/`` validates against
  ``resources/schemas/health_check.v1.json``;
- every spec names an operator that is actually registered in
  :mod:`ovid2.transformations.quality.ops`;
- the check-spec ``part`` values are drawn from the known set;
- ids are unique.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"
_CHECKS_DIR = _SRC / "resources" / "health" / "checks"
_SCHEMA = _SRC / "resources" / "schemas" / "health_check.v1.json"


def _spec_files() -> list[Path]:
    return sorted(_CHECKS_DIR.glob("*.yaml"))


@pytest.mark.contract
def test_health_checks_dir_exists() -> None:
    assert _CHECKS_DIR.is_dir(), f"missing health checks dir: {_CHECKS_DIR}"
    assert _spec_files(), "no health-check specs found"


@pytest.mark.contract
def test_health_specs_validate_against_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    for path in _spec_files():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.validate(raw, schema)


@pytest.mark.contract
def test_every_spec_operator_is_registered() -> None:
    from ovid2.transformations.quality.ops import operator_names

    known = operator_names()
    offenders: list[str] = []
    for path in _spec_files():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw.get("operator") not in known:
            offenders.append(f"{path.name}: operator '{raw.get('operator')}'")
    assert not offenders, "specs reference unknown operators:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_spec_ids_unique() -> None:
    ids: list[str] = []
    for path in _spec_files():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        ids.append(str(raw["id"]))
    assert len(ids) == len(set(ids)), f"duplicate check ids: {ids}"


@pytest.mark.contract
def test_registry_loads_all_specs() -> None:
    from ovid2.transformations.quality.registry import load_all_specs

    specs = load_all_specs()
    assert len(specs) == len(_spec_files())
