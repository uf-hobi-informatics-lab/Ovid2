"""Contract: every route JSON on disk uses a supported kind (C1).

Introduced by phase C1. Enforces the semantic that:

- Every ``$XDG_CONFIG_HOME/ovid2/routes/*.json`` has a ``kind`` value
  in :data:`ovid2.web.models.PIPELINE_KINDS` after normalisation
  (defaults to ``etl`` when missing; the deprecated alias ``cdm`` is
  accepted and normalised to ``etl`` for backwards compatibility with
  pre-refactor routes).
- When ``kind='etl'`` and ``cdm`` is set, it must reference a CDM
  registered in :mod:`ovid2.cdm.registry`.

The contract is quiet when no route files exist — CI / dev laptops
without any curated routes still pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ovid2.cdm import registered_cdms
from ovid2.web.models import PIPELINE_KINDS, normalize_kind


def _route_search_paths() -> list[Path]:
    """Return the ordered list of directories that may contain route JSONs."""
    paths: list[Path] = []
    override = os.environ.get("OVID2_PIPELINES_DIR")
    if override:
        paths.append(Path(override))
    xdg_cfg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    paths.append(Path(xdg_cfg) / "ovid2" / "pipelines")
    paths.append(Path.cwd() / "pipelines")
    paths.append(Path("/etc/ovid2/pipelines"))
    return paths


def _iter_route_files() -> list[Path]:
    """Return every route JSON discoverable on the search path."""
    seen: set[str] = set()
    result: list[Path] = []
    for directory in _route_search_paths():
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.suffix == ".json" and entry.stem not in seen:
                seen.add(entry.stem)
                result.append(entry)
    return result


@pytest.mark.contract
def test_route_kind_is_valid() -> None:
    """Every route file has ``kind`` in the allowed enum."""
    offenders: list[str] = []
    for path in _iter_route_files():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        kind = normalize_kind(payload.get("kind"))
        if kind not in PIPELINE_KINDS:
            offenders.append(f"{path}: kind={kind!r}")
    assert not offenders, "Route kind violations:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_route_cdm_is_registered() -> None:
    """Every route with a non-null ``cdm`` references a registered CDM."""
    known = set(registered_cdms())
    offenders: list[str] = []
    for path in _iter_route_files():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cdm = payload.get("cdm")
        if cdm and cdm not in known:
            offenders.append(f"{path}: cdm={cdm!r} (registered: {sorted(known)})")
    assert not offenders, "Route CDM references not registered:\n  " + "\n  ".join(offenders)


@pytest.mark.contract
def test_route_kinds_enum_matches_expectations() -> None:
    """Guard against accidentally widening PIPELINE_KINDS without an ADR update.

    The set is intentionally frozen here so that any addition triggers a
    tripwire and the author is forced to update the C1 ADR (or a
    superseding delta) plus this test. See
    the adr-copy-job-kind design note for the ``copy``
    kind rationale (supersedes the C1 "no passthrough kind" stance).
    ``move`` was added post-M1 to expose the driver-native successor to
    ``copy`` on the web + MCP surfaces. The ``cdm`` kind was renamed to
    ``etl`` in the pipeline-centric panel refactor (``cdm`` remains an
    accepted input alias via :func:`normalize_kind`); see
    the adr-pipeline-centric-panel design note. ``health``
    was added for the PCORnet EDC / data-health checks; see
    the adr-health-checks-edc-port design note.
    """
    assert set(PIPELINE_KINDS) == {
        "etl",
        "analysis",
        "dq_checks",
        "health",
        "copy",
        "move",
    }, (
        "PIPELINE_KINDS changed. Update the ADR (see the C1 ADR + any "
        "delta ADRs) and this test if the change is intentional."
    )
