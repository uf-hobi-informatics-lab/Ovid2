"""Contract: every packaged semantic YAML is well-formed (S3).

Introduced by phase S3. Every YAML file under
``src/ovid2/resources/semantic/`` must:

- **Vocabularies** (``vocabularies/<name>.yaml``) — validate against
  ``resources/schemas/semantic_handle.v1.json``, construct a valid
  :class:`~ovid2.semantic.handle.VocabularyHandle`, and register in
  the semantic layer.
- **Bindings** (``bindings/<cdm>.yaml``) — validate against
  ``resources/schemas/semantic_bindings.v1.json``, reference a
  registered CDM in :mod:`ovid2.cdm.registry`, reference a
  registered vocabulary in :mod:`ovid2.semantic.registry`, and every
  row's ``(cdm, table)`` must exist in the target CDM.

Additive over the phase-S1 semantic contract tests
(``test_semantic_registry_complete.py``,
``test_vocabulary_source_exclusive.py``,
``test_semantic_isolation.py``) — those still run against the same
registry the loader populates.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest
import yaml

import ovid2.semantic as semantic
from ovid2.cdm import registered_cdms
from ovid2.cdm.registry import get_cdm
from ovid2.semantic.vocabularies._yaml_loader import (
    _bindings_dir,
    _vocabularies_dir,
    build_binding,
    build_vocabulary,
)


def _load_schema(name: str) -> dict:
    """Return one packaged JSON schema."""
    with resources.as_file(resources.files("ovid2.resources.schemas").joinpath(name)) as path:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def _validate(payload: dict, schema: dict, source: str) -> None:
    """Validate ``payload`` against ``schema`` using ``jsonschema``."""
    try:
        import jsonschema  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("jsonschema not installed; skipping full validation")
    try:
        jsonschema.validate(instance=payload, schema=schema)
    except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
        pytest.fail(f"Schema violation in {source}: {exc.message}")


@pytest.mark.contract
def test_every_vocabulary_yaml_validates() -> None:
    """Every ``resources/semantic/vocabularies/*.yaml`` matches the schema."""
    schema = _load_schema("semantic_handle.v1.json")
    directory = _vocabularies_dir()
    if not directory.is_dir():
        pytest.skip("no vocabularies directory")
    yamls = sorted(directory.glob("*.yaml"))
    assert yamls, "no vocabulary YAML files found under resources/semantic/vocabularies/"
    for path in yamls:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        _validate(payload, schema, str(path))


@pytest.mark.contract
def test_every_vocabulary_yaml_constructs() -> None:
    """Every vocabulary YAML builds a valid :class:`VocabularyHandle`."""
    directory = _vocabularies_dir()
    if not directory.is_dir():
        pytest.skip("no vocabularies directory")
    offenders: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            handle = build_vocabulary(payload)
        except (ValueError, KeyError) as exc:
            offenders.append(f"{path.name}: {exc}")
            continue
        if not handle.name:
            offenders.append(f"{path.name}: empty name")
    assert not offenders, "Vocabulary YAMLs failed to construct:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_every_vocabulary_yaml_is_registered() -> None:
    """Every vocabulary YAML ends up in the semantic registry."""
    directory = _vocabularies_dir()
    if not directory.is_dir():
        pytest.skip("no vocabularies directory")
    registered = set(semantic.registered("vocabulary"))
    missing: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        name = str(payload.get("name") or "")
        if name and name not in registered:
            missing.append(f"{path.name} (name={name!r})")
    assert not missing, (
        "Vocabulary YAMLs not registered — did semantic/__init__ run the loader?\n  "
        + "\n  ".join(missing)
    )


@pytest.mark.contract
def test_every_binding_yaml_validates() -> None:
    """Every ``resources/semantic/bindings/*.yaml`` matches the schema."""
    schema = _load_schema("semantic_bindings.v1.json")
    directory = _bindings_dir()
    if not directory.is_dir():
        pytest.skip("no bindings directory")
    yamls = sorted(directory.glob("*.yaml"))
    assert yamls, "no binding YAML files found under resources/semantic/bindings/"
    for path in yamls:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        _validate(payload, schema, str(path))


@pytest.mark.contract
def test_every_binding_references_registered_cdm() -> None:
    """Each binding YAML's ``cdm`` field points at a registered CDM."""
    directory = _bindings_dir()
    if not directory.is_dir():
        pytest.skip("no bindings directory")
    known = set(registered_cdms())
    offenders: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        cdm = str(payload.get("cdm") or "")
        if cdm not in known:
            offenders.append(f"{path.name}: cdm={cdm!r} not registered")
    assert not offenders, "Binding YAMLs reference unknown CDMs:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_every_binding_references_registered_vocabulary() -> None:
    """Each binding row's ``vocabulary`` is present in the semantic registry."""
    directory = _bindings_dir()
    if not directory.is_dir():
        pytest.skip("no bindings directory")
    known_vocabs = set(semantic.registered("vocabulary"))
    offenders: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        cdm = str(payload.get("cdm") or "")
        for row in payload.get("bindings") or []:
            if not isinstance(row, dict):
                continue
            try:
                binding = build_binding(cdm, row)
            except (ValueError, KeyError) as exc:
                offenders.append(f"{path.name}: bad row {row!r}: {exc}")
                continue
            if binding.vocabulary not in known_vocabs:
                offenders.append(
                    f"{path.name}: {binding.table}.{binding.column} references "
                    f"unregistered vocabulary {binding.vocabulary!r}"
                )
    assert not offenders, "Binding YAMLs reference unregistered vocabularies:\n  " + "\n  ".join(
        offenders
    )


@pytest.mark.contract
def test_every_binding_targets_a_real_column() -> None:
    """Each binding's ``(cdm, table, column)`` must exist on the CDM handle.

    Uses ``handle.load_table_columns(table)`` to enumerate real columns
    for the CDM, and fails when a binding points at a column the CDM
    doesn't declare.
    """
    directory = _bindings_dir()
    if not directory.is_dir():
        pytest.skip("no bindings directory")
    offenders: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        cdm = str(payload.get("cdm") or "")
        try:
            handle = get_cdm(cdm)
        except ValueError:
            continue  # already flagged by test_every_binding_references_registered_cdm
        for row in payload.get("bindings") or []:
            if not isinstance(row, dict):
                continue
            try:
                binding = build_binding(cdm, row)
            except (ValueError, KeyError):
                continue  # already flagged
            try:
                columns = {c.upper() for c in handle.load_table_columns(binding.table)}
            except (KeyError, ValueError, FileNotFoundError):
                offenders.append(f"{path.name}: table {binding.table!r} not on {cdm!r}")
                continue
            if binding.column not in columns:
                offenders.append(
                    f"{path.name}: {binding.table}.{binding.column} not a column "
                    f"of {cdm}.{binding.table}"
                )
    assert not offenders, "Binding YAMLs point at nonexistent CDM columns:\n  " + "\n  ".join(
        offenders
    )
