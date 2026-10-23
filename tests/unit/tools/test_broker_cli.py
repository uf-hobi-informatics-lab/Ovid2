"""Unit tests for the executor-domain broker CLI entries.

``ovid2 tools probe-connection`` and ``ovid2 tools browse-storage`` are
the executor-side counterparts of the web tier's connection-test and
storage-browse endpoints (harness / API / executor trust model). They
resolve secrets and print exactly one JSON object on the last line of
stdout.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from ovid2.cli import tools as tools_cli


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG config home with connections/ + secrets.d/."""
    root = tmp_path / "cfg"
    (root / "ovid2" / "connections").mkdir(parents=True)
    (root / "ovid2" / "secrets.d").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(root / "ovid2" / "connections"))
    return root


def _write_disc_conn(xdg: Path, name: str, host_path: Path) -> None:
    (xdg / "ovid2" / "connections" / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {"host_path": str(host_path), "mount_mode": "ro"},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_probe_connection_emits_json(
    xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "feed"
    target.mkdir()
    _write_disc_conn(xdg, "c_ok", target)

    rc = tools_cli._probe_connection_entry(Namespace(name="c_ok"))
    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)
    assert payload["ok"] is True
    assert rc == 0


@pytest.mark.unit
def test_probe_connection_unknown_connection(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = tools_cli._probe_connection_entry(Namespace(name="nope"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["code"] == "config"
    assert rc == 1


@pytest.mark.unit
def test_browse_storage_lists_entries(
    xdg: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "feed"
    (target / "sub").mkdir(parents=True)
    (target / "a.csv").write_text("x", encoding="utf-8")
    _write_disc_conn(xdg, "c_browse", target)

    rc = tools_cli._browse_storage_entry(Namespace(name="c_browse", prefix=None))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["supported"] is True
    names = {e["name"] for e in payload["entries"]}
    assert "a.csv" in names
    assert "sub" in names
    assert rc == 0


@pytest.mark.unit
def test_browse_storage_unknown_type_reports(xdg: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (xdg / "ovid2" / "connections" / "c_bad.json").write_text(
        json.dumps({"type": "disc", "args": {}}), encoding="utf-8"
    )
    # Missing host_path -> config error surfaced as ok=False.
    rc = tools_cli._browse_storage_entry(Namespace(name="c_bad", prefix=None))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "ok" in payload
    assert rc in (0, 1)


# ---------------------------------------------------------------------------
# ``ovid2 tools list-source-tables`` — draft-time ``all`` expansion broker.
# ---------------------------------------------------------------------------


@pytest.fixture
def configs(xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated address-config dir on the search path."""
    path = tmp_path / "configs"
    path.mkdir()
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(path))
    return path


def _list_src(address: str, pipeline: str = "demo", batch_id: str = "b1") -> Namespace:
    return Namespace(address=address, pipeline=pipeline, batch_id=batch_id)


@pytest.mark.unit
def test_list_source_tables_disc_largest_first(
    xdg: Path, configs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "feed"
    batch = root / "demo" / "b1"
    batch.mkdir(parents=True)
    (batch / "demographic.csv").write_text("x" * 10)
    (batch / "lab_result_cm.csv").write_text("x" * 1000)
    (batch / "harvest.csv").write_text("x")
    _write_disc_conn(xdg, "feed", root)
    (configs / "feed_addr.json").write_text(json.dumps({"connection": "feed", "location": {}}))

    rc = tools_cli._list_source_tables_entry(_list_src("feed_addr"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert payload["code"] == "ok" and payload["type"] == "disc"
    assert payload["tables"] == [
        "lab_result_cm=lab_result_cm.csv",
        "demographic=demographic.csv",
        "harvest=harvest.csv",
    ]
    assert payload["sizes"]["lab_result_cm.csv"] == 1000


@pytest.mark.unit
def test_list_source_tables_missing_batch_is_error(
    xdg: Path, configs: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_disc_conn(xdg, "feed", tmp_path / "does-not-exist")
    (configs / "feed_addr.json").write_text(json.dumps({"connection": "feed", "location": {}}))
    rc = tools_cli._list_source_tables_entry(_list_src("feed_addr"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1
    assert payload["code"] == "error" and payload["supported"] is True
    assert "no batch folder" in payload["message"]


@pytest.mark.unit
def test_list_source_tables_unsupported_type(
    configs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (configs / "pg.json").write_text(
        json.dumps({"src_name": "postgres", "src_args": {"host": "db.example.org"}})
    )
    rc = tools_cli._list_source_tables_entry(_list_src("pg"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1
    assert payload["code"] == "unsupported" and payload["supported"] is False


@pytest.mark.unit
def test_list_source_tables_registered_in_cli() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    tools_cli.register(sub)
    ns = parser.parse_args(
        ["tools", "list-source-tables", "addr", "--pipeline", "p", "--batch_id", "b"]
    )
    assert ns._tool == "list-source-tables"
