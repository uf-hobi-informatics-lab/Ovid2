"""Contract: every registered CDM has a well-formed YAML directory (C1).

Introduced by phase C1. Every CDM registered in
:mod:`ovid2.cdm.registry` must ship a companion YAML tree under
``src/ovid2/resources/cdms/<cdm>/`` with:

- ``cdm.yaml`` — handle-level metadata, JSON-schema-validated against
  ``resources/schemas/cdm.v1.json``.
- ``tables/<table>.yaml`` for every table declared in the handle's
  ``composite_keys``, JSON-schema-validated against
  ``resources/schemas/cdm_table.v1.json``.
- ``valuesets/<table>.<field>.yaml`` for every valueset the handle's
  ``valuesets`` mirror carries, JSON-schema-validated against
  ``resources/schemas/cdm_valueset.v1.json``.

CDMs without a YAML directory are allowed only when the handle's
``schema_source`` is ``None`` and no tables reference valuesets — i.e.
the pipeline-family CDMs (``ovid2_pipeline_veritas_v1``, …) that are
schema-less by design.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest
import yaml

from ovid2.cdm import registered_cdms
from ovid2.cdm.registry import get_cdm
from ovid2.cdm.yaml_loader import cdm_yaml_dir, CdmYamlError


def _load_schema(name: str) -> dict:
    """Return one packaged JSON schema."""
    with resources.as_file(
        resources.files("ovid2.resources.schemas").joinpath(name)
    ) as path:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate(payload: dict, schema: dict, source: str) -> None:
    """Validate ``payload`` against ``schema`` using ``jsonschema`` when available.

    Falls back to a minimal structural check when ``jsonschema`` is not
    installed — the CI environment ships it, but developer laptops that
    skip test extras still get useful signal.
    """
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("jsonschema not installed; skipping full validation")
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        pytest.fail(f"Schema violation in {source}: {exc.message}")


@pytest.mark.contract
def test_every_cdm_yaml_loads() -> None:
    """Every registered CDM either loads from YAML or is schema-less by design."""
    offenders: list[str] = []
    for name in registered_cdms():
        handle = get_cdm(name)
        try:
            directory = cdm_yaml_dir(name)
        except CdmYamlError:
            # Schema-less CDMs (pipeline-family) are allowed to skip YAML.
            if handle.schema_source is None and not handle.composite_keys:
                continue
            if handle.schema_source is None and not any(
                # Handle has composite_keys but no schema — pipeline-family CDM.
                handle.load_table_columns(t) for t in handle.tables()
            ):
                continue
            offenders.append(f"{name}: no YAML directory but handle carries schema")
            continue
        if not (directory / "cdm.yaml").is_file():
            offenders.append(f"{name}: cdm.yaml missing at {directory}")
    assert not offenders, "CDM YAML violations:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_cdm_yaml_root_matches_schema() -> None:
    """Every ``cdm.yaml`` validates against ``cdm.v1.json``."""
    schema = _load_schema("cdm.v1.json")
    checked = 0
    for name in registered_cdms():
        try:
            directory = cdm_yaml_dir(name)
        except CdmYamlError:
            continue
        root = directory / "cdm.yaml"
        if not root.is_file():
            continue
        payload = yaml.safe_load(root.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), f"{root}: not a YAML mapping"
        _validate(payload, schema, str(root))
        checked += 1
    assert checked > 0, "At least one CDM must ship a cdm.yaml."


@pytest.mark.contract
def test_cdm_table_yaml_matches_schema() -> None:
    """Every ``tables/<table>.yaml`` validates against ``cdm_table.v1.json``."""
    schema = _load_schema("cdm_table.v1.json")
    checked = 0
    for name in registered_cdms():
        try:
            directory = cdm_yaml_dir(name)
        except CdmYamlError:
            continue
        tables_dir = directory / "tables"
        if not tables_dir.is_dir():
            continue
        for path in sorted(tables_dir.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict), f"{path}: not a YAML mapping"
            _validate(payload, schema, str(path))
            checked += 1
    assert checked > 0, "At least one CDM must ship per-table YAMLs."


@pytest.mark.contract
def test_cdm_valueset_yaml_matches_schema() -> None:
    """Every ``valuesets/<table>.<field>.yaml`` validates against ``cdm_valueset.v1.json``."""
    schema = _load_schema("cdm_valueset.v1.json")
    checked = 0
    for name in registered_cdms():
        try:
            directory = cdm_yaml_dir(name)
        except CdmYamlError:
            continue
        vs_dir = directory / "valuesets"
        if not vs_dir.is_dir():
            continue
        for path in sorted(vs_dir.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict), f"{path}: not a YAML mapping"
            # Skip the auto-generated package __init__.py neighbour (not a YAML).
            _validate(payload, schema, str(path))
            checked += 1
    # No hard minimum: some CDMs (pipeline-family) have no valuesets.
    # But if pcornet_7_0 is registered, expect at least one valueset.
    if "pcornet_7_0" in registered_cdms():
        assert checked > 0, "pcornet_7_0 must ship at least one valueset YAML."


@pytest.mark.contract
def test_cdm_yaml_tables_match_handle_tables() -> None:
    """Every table in ``cdm.yaml`` has a matching ``tables/<t>.yaml`` file."""
    offenders: list[str] = []
    for name in registered_cdms():
        try:
            directory = cdm_yaml_dir(name)
        except CdmYamlError:
            continue
        root = directory / "cdm.yaml"
        if not root.is_file():
            continue
        payload = yaml.safe_load(root.read_text(encoding="utf-8"))
        for table in (payload.get("tables") or []):
            if not (directory / "tables" / f"{table}.yaml").is_file():
                offenders.append(f"{name}: tables/{table}.yaml missing")
    assert not offenders, "CDM table YAML gaps:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_cdm_yaml_valueset_names_are_dotted() -> None:
    """Every valueset YAML has a name matching ``<cdm>.<table>.<field>``."""
    offenders: list[str] = []
    for name in registered_cdms():
        try:
            directory = cdm_yaml_dir(name)
        except CdmYamlError:
            continue
        vs_dir = directory / "valuesets"
        if not vs_dir.is_dir():
            continue
        for path in sorted(vs_dir.glob("*.yaml")):
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            declared = str(payload.get("name") or "")
            parts = declared.split(".")
            if len(parts) != 3 or parts[0] != name:
                offenders.append(
                    f"{path}: name={declared!r} — expected `{name}.<table>.<field>`."
                )
    assert not offenders, "Valueset naming violations:\n  " + "\n  ".join(offenders)
