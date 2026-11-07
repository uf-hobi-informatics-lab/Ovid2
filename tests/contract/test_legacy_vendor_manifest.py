"""Contract: the vendored legacy code matches its manifest.

``pipelines/legacy_ovid_vendor/MANIFEST.json`` pins the sha256 of every
legacy-Ovid file in the parity port's scope. The vendored copies must be
byte-identical to the manifest (no hand edits). When the authoritative
legacy tree is present on the host (``<legacy-ovid>`` or
``$OVID2_LEGACY_ROOT``) the test additionally reports drift — legacy
changed since the port was generated — as a failure, so the port is
re-synced (``scripts/legacy_port/vendor_legacy.py``) before parity is
claimed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "pipelines" / "legacy_ovid_vendor"


def _manifest() -> dict[str, str]:
    path = _VENDOR / "MANIFEST.json"
    if not path.is_file():
        pytest.skip("no legacy vendor manifest")
    return dict(json.loads(path.read_text(encoding="utf-8"))["files"])


@pytest.mark.contract
def test_vendored_files_match_manifest() -> None:
    manifest = _manifest()
    bad: list[str] = []
    for rel, sha in manifest.items():
        vendored = _VENDOR / rel
        if not vendored.is_file():
            continue  # hashed-only (formatters / dictionaries live in pipeline packages)
        if hashlib.sha256(vendored.read_bytes()).hexdigest() != sha:
            bad.append(rel)
    assert not bad, "vendored legacy files edited (must stay verbatim):\n" + "\n".join(bad)


@pytest.mark.contract
def test_legacy_tree_has_not_drifted() -> None:
    root = Path(os.environ.get("OVID2_LEGACY_ROOT", "<legacy-ovid>"))
    if not (root / "partners").is_dir():
        pytest.skip(f"legacy tree not present at {root}")
    manifest = _manifest()
    drift = [
        rel
        for rel, sha in manifest.items()
        if (root / rel).is_file() and hashlib.sha256((root / rel).read_bytes()).hexdigest() != sha
    ]
    assert not drift, (
        "legacy Ovid changed since the port was generated — re-run "
        "scripts/legacy_port/vendor_legacy.py + gen_pipelines.py + gen_legacy_cdms.py:\n"
        + "\n".join(drift[:50])
    )
