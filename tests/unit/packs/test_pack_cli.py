"""Unit tests — ``ovid2 tools pack`` (real argv through ``ovid2.cli.__main__.main``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ovid2.cli.__main__ import main
from ovid2.packs import clear_cache
from tests.unit.packs.conftest import make_pack, write_yaml

pytestmark = pytest.mark.unit


def _json(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


def test_list_and_show(pack_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_pack(pack_env, "acme")
    clear_cache()
    assert main(["tools", "pack", "list"]) == 0
    listed = {p["id"]: p for p in _json(capsys)}  # type: ignore[union-attr]
    assert listed["pcornet_7_0"]["active"] is True
    assert listed["acme"]["origin"] == "folder" and listed["acme"]["active"] is False

    assert main(["tools", "pack", "show", "pcornet_7_0"]) == 0
    shown = _json(capsys)
    assert "1.01" in shown["checks"] and "20r1" in shown["packages"]  # type: ignore[index]

    assert main(["tools", "pack", "show", "nope"]) == 1
    assert "unknown pack" in capsys.readouterr().err


def test_enable_disable_round_trip(pack_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from ovid2.config.paths import config_dir
    from ovid2.packs import get_pack

    make_pack(pack_env, "acme")
    assert main(["tools", "pack", "enable", "acme"]) == 0
    assert main(["tools", "pack", "enable", "acme"]) == 0  # idempotent
    path = config_dir() / "packs.yaml"
    assert yaml.safe_load(path.read_text())["enabled"] == ["acme"]
    assert path.stat().st_mode & 0o777 == 0o644
    clear_cache()
    assert get_pack("acme").active  # type: ignore[union-attr]

    assert main(["tools", "pack", "disable", "acme"]) == 0
    assert yaml.safe_load(path.read_text())["enabled"] == []
    clear_cache()
    assert not get_pack("acme").active  # type: ignore[union-attr]
    capsys.readouterr()


@pytest.mark.parametrize("python", [False, True])
def test_scaffold_then_validate(
    tmp_path: Path, pack_env: Path, capsys: pytest.CaptureFixture[str], python: bool
) -> None:
    argv = ["tools", "pack", "scaffold", "demo_pack", "--out-dir", str(tmp_path), "--domain", "x"]
    assert main([*argv, "--python"] if python else argv) == 0
    root = tmp_path / ("ovid2-pack-demo-pack/ovid2_pack_demo_pack" if python else "demo_pack")
    assert (root / "pack.yaml").stat().st_mode & 0o777 == 0o644
    if python:
        pyproject = (tmp_path / "ovid2-pack-demo-pack" / "pyproject.toml").read_text()
        assert '[project.entry-points."ovid2.packs"]' in pyproject
        assert (root / "operators.py").is_file()
    capsys.readouterr()
    assert main(["tools", "pack", "validate", str(root)]) == 0
    result = _json(capsys)
    assert result["ok"] is True, result  # type: ignore[index]
    # A second scaffold into the same place is refused.
    assert main([*argv, "--python"] if python else argv) == 1
    assert "scaffold failed" in capsys.readouterr().err


def test_scaffold_rejects_bad_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tools", "pack", "scaffold", "Bad-Id", "--out-dir", str(tmp_path)]) == 1
    assert "invalid pack id" in capsys.readouterr().err


def test_validate_reports_errors(
    tmp_path: Path, pack_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pack = make_pack(
        tmp_path,
        "acme",
        checks=[
            {
                "id": "wrong.prefix",
                "name": "X",
                "part": "nowhere",
                "description": "d",
                "operator": "no_such_operator",
                "source_tables": ["SUBJECT"],
                "remediation": {"fixers": ["no_such_fixer"], "transforms": ["no_such_op"]},
            }
        ],
        packages=[
            {"id": "acme.p", "label": "P", "released": "2026-01-01", "checks": ["missing.check"]}
        ],
    )
    write_yaml(
        pack / "cdms" / "acme" / "cdm.yaml",
        {"v": 1, "name": "other", "version": "1", "tables": ["ghost"]},
    )
    assert main(["tools", "pack", "validate", str(pack)]) == 1
    errors = "\n".join(_json(capsys)["errors"])  # type: ignore[index]
    for needle in (
        "must start with 'acme.'",
        "part 'nowhere'",
        "operator 'no_such_operator' is not registered",
        "remediation fixer 'no_such_fixer'",
        "remediation transform 'no_such_op'",
        "unknown check 'missing.check'",
    ):
        assert needle in errors, (needle, errors)


def test_validate_missing_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tools", "pack", "validate", str(tmp_path)]) == 1
    assert _json(capsys)["ok"] is False  # type: ignore[index]


def test_bare_pack_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tools", "pack"]) == 2
    assert "scaffold" in capsys.readouterr().err
