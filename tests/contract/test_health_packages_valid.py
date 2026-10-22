"""Contract: every registered health-check package (EDC/DC report cycle)
is valid and references only known checks.

Enforces the cycle-package invariants:

- every YAML manifest under ``resources/health/packages/`` validates
  against ``resources/schemas/health_package.v1.json``;
- every check id listed in a package resolves to a registered check
  spec (matching the spec's ``id`` or ``name``);
- package ids are unique;
- the registry loads every manifest and orders them most-recent first.

Packages may *share* checks (the same check id can appear in several
packages), so ids across packages are deliberately **not** required to
be disjoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

_SRC = Path(__file__).resolve().parents[2] / "src" / "ovid2"
_PACKAGES_DIR = _SRC / "resources" / "health" / "packages"
_CHECKS_DIR = _SRC / "resources" / "health" / "checks"
_SCHEMA = _SRC / "resources" / "schemas" / "health_package.v1.json"


def _package_files() -> list[Path]:
    return sorted(_PACKAGES_DIR.glob("*.yaml"))


def _known_check_ids() -> set[str]:
    known: set[str] = set()
    for path in sorted(_CHECKS_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        known.add(str(raw["id"]))
        if raw.get("name"):
            known.add(str(raw["name"]))
    return known


@pytest.mark.contract
def test_packages_dir_exists() -> None:
    assert _PACKAGES_DIR.is_dir(), f"missing health packages dir: {_PACKAGES_DIR}"
    assert _package_files(), "no health-check packages found"


@pytest.mark.contract
def test_packages_validate_against_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    for path in _package_files():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.validate(raw, schema)


@pytest.mark.contract
def test_package_check_ids_resolve() -> None:
    known = _known_check_ids()
    offenders: list[str] = []
    for path in _package_files():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        for check_id in raw.get("checks", []):
            if str(check_id) not in known:
                offenders.append(f"{path.name}: unknown check '{check_id}'")
    assert not offenders, "packages reference unknown checks:\n" + "\n".join(offenders)


@pytest.mark.contract
def test_package_ids_unique() -> None:
    ids = [str(yaml.safe_load(p.read_text(encoding="utf-8"))["id"]) for p in _package_files()]
    assert len(ids) == len(set(ids)), f"duplicate package ids: {ids}"


@pytest.mark.contract
def test_registry_loads_all_packages_most_recent_first() -> None:
    from ovid2.transformations.quality.registry import load_all_packages

    packages = load_all_packages()
    assert len(packages) == len(_package_files())
    released = [p.released for p in packages]
    assert released == sorted(released, reverse=True), "packages must be ordered most-recent first"
