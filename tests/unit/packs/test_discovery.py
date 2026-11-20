"""Unit tests — ``ovid2.packs.discovery`` (sources, activation, validation)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from ovid2.packs import active_packs, all_packs, clear_cache, get_pack, pack_dirs
from ovid2.packs.discovery import pack_for_check, pack_for_fixer
from tests.unit.packs.conftest import make_pack, write_yaml

pytestmark = pytest.mark.unit


def _by_id() -> dict[str, object]:
    return {p.id: p for p in all_packs()}


def test_builtin_pcornet_is_always_active(pack_env: Path) -> None:
    info = get_pack("pcornet_7_0")
    assert info is not None
    assert info.builtin and info.active and info.enabled
    assert info.content_dir("checks_dir") is None  # core content, no extra dirs
    assert pack_for_check("1.01") == "pcornet_7_0"
    assert pack_for_fixer("race_eth") == "pcornet_7_0"
    assert pack_for_check("health.null_rate") == ""


def test_folder_pack_discovered_but_disabled_by_default(pack_env: Path) -> None:
    make_pack(pack_env, "acme")
    clear_cache()
    info = get_pack("acme")
    assert info is not None and info.origin == "folder"
    assert not info.enabled and not info.active and not info.error
    assert [p.id for p in active_packs()] == ["pcornet_7_0"]
    assert pack_dirs("checks_dir") == []


@pytest.mark.parametrize("how", ["env_id", "env_star", "packs_yaml"])
def test_folder_pack_activation(pack_env: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    make_pack(pack_env, "acme")
    if how == "env_id":
        monkeypatch.setenv("OVID2_PACKS_ENABLED", "other, acme")
    elif how == "env_star":
        monkeypatch.setenv("OVID2_PACKS_ENABLED", "*")
    else:
        from ovid2.config.paths import config_dir

        write_yaml(config_dir() / "packs.yaml", {"enabled": ["acme"]})
    clear_cache()
    info = get_pack("acme")
    assert info is not None and info.active
    dirs = pack_dirs("checks_dir")
    assert [(i.id, d.name) for i, d in dirs] == [("acme", "checks")]
    assert info.cdm_dir("acme") == pack_env / "acme" / "cdms" / "acme"


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"requires_ovid2": ">=99.0"}, "requires ovid2 >=99.0"),
        ({"requires_ovid2": "~=1"}, "cannot parse"),
        ({"provides": {"cdms": ["other_cdm"]}}, "must be named 'acme'"),
        ({"provides": {"cdms": ["acme_x"]}}, "missing cdms/acme_x/cdm.yaml"),
        ({"provides": {"parts": [{"id": "safety", "label": "S"}]}}, "part 'safety'"),
        ({"provides": {"operators_module": "ops"}}, "only installed (pip) packs may ship code"),
        (
            {"provides": {"fixers": {"fix": {"summary": "x"}}, "fixers_module": "f"}},
            "fixer 'fix' must be named acme_<name>",
        ),
    ],
)
def test_folder_pack_validation_errors(
    pack_env: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], error: str
) -> None:
    make_pack(pack_env, "acme", **kwargs)  # type: ignore[arg-type]
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "*")
    clear_cache()
    info = get_pack("acme")
    assert info is not None and info.enabled
    assert error in info.error
    assert not info.active


def test_reserved_id_mismatch_and_broken_manifest(
    pack_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_pack(pack_env, "core", provides={})
    make_pack(pack_env, "renamed", provides={})
    write_yaml(
        pack_env / "renamed" / "pack.yaml", {"v": 1, "id": "other", "version": "1", "label": "x"}
    )
    (pack_env / "broken").mkdir()
    (pack_env / "broken" / "pack.yaml").write_text("v: 1\n", encoding="utf-8")
    (pack_env / "not_a_pack").mkdir()  # no pack.yaml: ignored
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "*")
    clear_cache()
    packs = _by_id()
    assert "reserved" in packs["core"].error  # type: ignore[attr-defined]
    assert "does not match pack id" in packs["other"].error  # type: ignore[attr-defined]
    broken = packs["broken"]
    assert broken.manifest is None and broken.error and not broken.active  # type: ignore[attr-defined]
    assert "not_a_pack" not in packs


def test_members_on_non_builtin_is_a_warning(
    pack_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_pack(pack_env, "acme", provides={"members": {"checks": ["1.01"]}})
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "acme")
    clear_cache()
    info = get_pack("acme")
    assert info is not None and info.active
    assert any("members" in w for w in info.warnings)
    assert pack_for_check("1.01") == "pcornet_7_0"  # a folder pack cannot claim core content


def test_folder_pack_cannot_shadow_builtin(pack_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_pack(pack_env, "pcornet_7_0", provides={})
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "*")
    clear_cache()
    dupes = [p for p in all_packs() if p.id == "pcornet_7_0"]
    assert [p.origin for p in dupes] == ["builtin", "folder"]
    assert "already provided by builtin" in dupes[1].error
    assert get_pack("pcornet_7_0").builtin  # type: ignore[union-attr]


def test_content_dir_refuses_escape(pack_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pack = make_pack(pack_env, "acme", provides={"checks_dir": "checks"})
    (pack / "checks").rmdir() if not any((pack / "checks").iterdir()) else None
    # Point checks/ at a directory outside the pack via a symlink.
    outside = pack_env.parent / "outside"
    outside.mkdir()
    import shutil

    shutil.rmtree(pack / "checks")
    (pack / "checks").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "acme")
    clear_cache()
    assert get_pack("acme").content_dir("checks_dir") is None  # type: ignore[union-attr]


def test_installed_pack_discovered_via_entry_point(
    pip_pack: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = pip_pack("acme")
    info = get_pack("acme")
    assert info is not None
    assert info.origin == "installed" and info.may_ship_code
    assert info.module == "ovid2_pack_acme"
    assert info.distribution == "ovid2-pack-acme 0.1.0"
    assert info.root == pkg and not info.enabled
    monkeypatch.setenv("OVID2_PACKS_ENABLED", "acme")
    clear_cache()
    assert get_pack("acme").active  # type: ignore[union-attr]
    import sys

    assert "ovid2_pack_acme" not in sys.modules, "discovery must not import pack code"


def test_installed_entry_point_name_mismatch(pip_pack: Callable[..., Path]) -> None:
    pip_pack("acme", entry_name="wrong")
    info = get_pack("acme")
    assert info is not None
    assert "entry point name 'wrong' does not match pack id 'acme'" in info.error


def test_as_dict_is_json_ready(pack_env: Path) -> None:
    import json

    make_pack(pack_env, "acme", domain="trials", homepage="https://example.org")
    clear_cache()
    out = get_pack("acme").as_dict()  # type: ignore[union-attr]
    json.dumps(out)
    assert out["domain"] == "trials" and out["origin"] == "folder"
    assert out["provides"]["checks_dir"] == "checks"
