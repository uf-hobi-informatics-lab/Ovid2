"""Unit tests for :mod:`ovid2.config.pipeline_packages`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ovid2.config import pipeline_packages


def _make_tree(root: Path) -> Path:
    (root / "ovid2_pipelines" / "demo").mkdir(parents=True)
    (root / "ovid2_pipelines" / "demo" / "__init__.py").write_text("")
    return root


@pytest.mark.unit
def test_explicit_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``OVID2_PIPELINE_PACKAGES_DIR`` is returned verbatim when it exists."""
    tree = _make_tree(tmp_path / "pkgs")
    monkeypatch.setenv("OVID2_PIPELINE_PACKAGES_DIR", str(tree))
    assert pipeline_packages.find_pipeline_packages_dir() == tree


@pytest.mark.unit
def test_legacy_env_ignored_when_it_only_holds_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A JSON-config-only ``OVID2_PIPELINES_DIR`` falls through to the walk-up."""
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "x.json").write_text("{}")
    monkeypatch.delenv("OVID2_PIPELINE_PACKAGES_DIR", raising=False)
    monkeypatch.setenv("OVID2_PIPELINES_DIR", str(configs))
    found = pipeline_packages.find_pipeline_packages_dir()
    assert found != configs


@pytest.mark.unit
def test_legacy_env_used_when_it_holds_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OVID2_PIPELINES_DIR`` still works for the packages tree (back-compat)."""
    tree = _make_tree(tmp_path / "legacy")
    monkeypatch.delenv("OVID2_PIPELINE_PACKAGES_DIR", raising=False)
    monkeypatch.setenv("OVID2_PIPELINES_DIR", str(tree))
    assert pipeline_packages.find_pipeline_packages_dir() == tree


@pytest.mark.unit
def test_repo_checkout_walk_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """From a source checkout the repo ``pipelines/`` directory is found."""
    monkeypatch.delenv("OVID2_PIPELINE_PACKAGES_DIR", raising=False)
    monkeypatch.delenv("OVID2_PIPELINES_DIR", raising=False)
    found = pipeline_packages.find_pipeline_packages_dir()
    assert found is not None
    assert (found / "ovid2_pipelines").is_dir()


@pytest.mark.unit
def test_ensure_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling twice inserts the directory exactly once."""
    tree = _make_tree(tmp_path / "pkgs")
    monkeypatch.setenv("OVID2_PIPELINE_PACKAGES_DIR", str(tree))
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert pipeline_packages.ensure_pipeline_packages_on_path() == tree
    assert pipeline_packages.ensure_pipeline_packages_on_path() == tree
    assert sys.path.count(str(tree)) == 1


@pytest.mark.unit
def test_cli_pipeline_list_sees_repo_packages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ovid2 pipeline list`` discovers the repo's packages with no PYTHONPATH."""
    from ovid2.cli.__main__ import main

    monkeypatch.delenv("OVID2_PIPELINE_PACKAGES_DIR", raising=False)
    monkeypatch.delenv("OVID2_PIPELINES_DIR", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert main(["pipeline", "list"]) == 0
    out = capsys.readouterr().out
    assert "example_pcornet" in out
