"""Contract: legacy `{src_name, src_args}` address configs continue to load.

W7 introduces the ``{connection, location}`` shape, but promises via
the ADR that the legacy shape stays supported for at least one
release. This test locks that promise in — every ``configs/*.json``
file that ships in the repo must still parse via
:func:`ovid2.config.addresses.load_address_config`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"


@pytest.mark.contract
def test_legacy_address_configs_still_load() -> None:
    """Every legacy JSON config in the repo continues to parse cleanly."""
    if not REPO_CONFIGS_DIR.is_dir():
        pytest.skip("Repo has no top-level configs/ directory.")

    from ovid2.config import addresses as _addresses

    legacy_files = [
        entry
        for entry in REPO_CONFIGS_DIR.iterdir()
        if entry.is_file()
        and entry.suffix == ".json"
        and not entry.stem.endswith("_sample")
    ]
    if not legacy_files:
        pytest.skip("No non-sample configs in the repo — nothing to lock in.")

    offenders: list[str] = []
    for path in sorted(legacy_files):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            offenders.append(f"{path.name}: JSON decode error {exc}")
            continue
        if "connection" in data:
            continue  # already migrated
        # Legacy configs must expose both src_name and src_args after
        # parsing — the shim relies on those keys to walk the old code
        # path.
        if "src_name" not in data or "src_args" not in data:
            offenders.append(
                f"{path.name}: missing src_name/src_args (not a legacy shape)"
            )
            continue
        # ``load_address_config`` sets ``_shape='legacy'`` on old files.
        # Run it through the loader search path via a temporary env
        # override so this test does not depend on the operator's XDG
        # home.
        import os
        prev = os.environ.get("OVID2_CONFIGS_DIR")
        os.environ["OVID2_CONFIGS_DIR"] = str(REPO_CONFIGS_DIR)
        try:
            cfg = _addresses.load_address_config(path.stem)
        finally:
            if prev is None:
                os.environ.pop("OVID2_CONFIGS_DIR", None)
            else:
                os.environ["OVID2_CONFIGS_DIR"] = prev
        if cfg.get("_shape") != "legacy":
            offenders.append(
                f"{path.name}: expected _shape='legacy', got {cfg.get('_shape')!r}"
            )

    assert not offenders, (
        "W7 contract broken: legacy address configs must keep loading:\n"
        + "\n".join(offenders)
    )
