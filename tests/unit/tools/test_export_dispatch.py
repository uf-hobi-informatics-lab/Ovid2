"""Unit tests: where ``ovid2 tools export-table`` runs (host vs container)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from ovid2.tools import table_ops


def _args(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "connection": "conn",
        "path": "x/T.csv",
        "fmt": "csv",
        "delimiter": ",",
        "header_case": "asis",
        "limit": None,
        "out": "/tmp/x.csv",
        "in_process": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture(autouse=True)
def _on_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ovid2.core.docker.is_inside_docker", lambda: False)
    monkeypatch.delenv("OVID2_EXPORT_MAX_ROWS", raising=False)


@pytest.mark.unit
def test_unbounded_export_goes_to_container() -> None:
    assert table_ops._export_runs_in_container(_args(limit=None)) is True


@pytest.mark.unit
def test_bounded_small_export_stays_on_host() -> None:
    assert table_ops._export_runs_in_container(_args(limit=500)) is False


@pytest.mark.unit
def test_over_cap_export_goes_to_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_EXPORT_MAX_ROWS", "1000")
    assert table_ops._export_runs_in_container(_args(limit=1001)) is True


@pytest.mark.unit
def test_in_process_flag_and_in_container_run_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    assert table_ops._export_runs_in_container(_args(in_process=True)) is False
    monkeypatch.setattr("ovid2.core.docker.is_inside_docker", lambda: True)
    assert table_ops._export_runs_in_container(_args()) is False


@pytest.mark.unit
def test_host_mode_dispatches_scoped_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The container argv carries the export, the artifact dir rw, and no Spark cluster."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "ovid2.config.connections.load_connection_config",
        lambda name: {"type": "disc", "args": {"host_path": str(tmp_path)}},
    )
    monkeypatch.setattr("ovid2.core.isolation.cleanup_stale_instances", lambda: None)

    def _submit(args: argparse.Namespace, instance: object) -> int:
        from ovid2.core.docker import _build_ovid2_argv

        seen["argv"] = _build_ovid2_argv(args)
        seen["mounts"] = [m.as_docker_v() for m in args.parsed_extra_mounts]
        seen["no_cluster"] = args.no_spark_cluster
        return 0

    monkeypatch.setattr("ovid2.core.docker.submit_to_docker", _submit)
    out = tmp_path / "exports" / "job.csv"
    rc = table_ops._export_entry(_args(out=str(out)))
    assert rc == 0
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == ["tools", "export-table", "conn"]
    assert "--limit" not in argv
    assert f"{out.parent}:{out.parent}:rw" in seen["mounts"]  # type: ignore[operator]
    assert f"{tmp_path}:{tmp_path}:ro" in seen["mounts"]  # type: ignore[operator]
    assert seen["no_cluster"] is True
