"""Contract: the shrink-only ratchet file is well-formed and every ratchet is enforced."""

from __future__ import annotations

import json
from pathlib import Path

#: Every ratchet must be checked by some contract test (name → owner test).
_OWNERS: dict[str, str] = {
    "pcornet_literals": "test_no_pcornet_in_generic.py",
    "ovid2001_suppressions": "test_no_collect_in_src.py",
    "pyspark_tree_runtime_importers": "test_no_pyspark_outside_engine.py",
    "cross_pipeline_imports": "test_no_runtime_pyspark_in_shared.py",
}


def test_ratchet_file_is_consistent() -> None:
    """Each entry has ``total == sum(files)`` and a known owner test."""
    path = Path(__file__).parent / "data" / "shrink_only.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == set(_OWNERS), (set(data), set(_OWNERS))
    for name, entry in data.items():
        files = entry["files"]
        assert entry["total"] == sum(files.values()), name
        assert all(isinstance(v, int) and v >= 0 for v in files.values()), name
        owner = Path(__file__).parent / _OWNERS[name]
        assert f'"{name}"' in owner.read_text(encoding="utf-8"), f"{name} not enforced by {owner}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
