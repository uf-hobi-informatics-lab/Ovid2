"""Unit tests — fixer_configs.yaml loader + ``migrate-fixers`` (builder A5)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ovid2.config.fixer_configs import (
    FixerConfigsError,
    dump_fixer_yaml,
    load_fixer_configs,
    validate_fixer_document,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Scaffold a throwaway example_pcornet clone on an extra namespace path."""
    import ovid2_pipelines

    from ovid2.tools.new_pipeline import scaffold_pipeline

    root = tmp_path / "ovid2_pipelines"
    root.mkdir()
    scaffold_pipeline(
        "fxclone", source_pipeline="example_pcornet", cdm="pcornet_7_0", pipelines_root=root
    )
    monkeypatch.setattr(ovid2_pipelines, "__path__", [*ovid2_pipelines.__path__, str(root)])
    yield "fxclone"
    for mod in [m for m in sys.modules if m.startswith("ovid2_pipelines.fxclone")]:
        del sys.modules[mod]


def test_validate_rejects_unknown_fix_and_key() -> None:
    errs = validate_fixer_document(
        {
            "v": 1,
            "fixer_chains": {"encounter": [{"fix": "nope"}, {"fix": "patid_orphans", "zz": "a"}]},
        }
    )
    assert any("unknown fix 'nope'" in e for e in errs)
    assert any("unrecognised key 'zz'" in e for e in errs)


def test_round_trip_and_precedence(clone: str) -> None:
    from ovid2.tools.migrate_fixers import MigrateFixersError, migrate_fixers

    chains_py, multi_py, source = load_fixer_configs(clone)
    assert source == "python" and chains_py
    path = migrate_fixers(clone)
    chains_y, multi_y, source_y = load_fixer_configs(clone)
    assert source_y == "yaml"
    assert chains_y == chains_py and multi_y == multi_py
    with pytest.raises(MigrateFixersError, match="already has"):
        migrate_fixers(clone)
    path.write_text("v: 1\nfixer_chains:\n  encounter:\n    - {fix: bogus}\n", encoding="utf-8")
    with pytest.raises(FixerConfigsError):
        load_fixer_configs(clone)


def test_dump_is_flow_style() -> None:
    text = dump_fixer_yaml({"demographic": [{"fix": "race_eth", "src1": "RACE"}]}, [])
    assert "    - {fix: race_eth, src1: RACE}" in text
