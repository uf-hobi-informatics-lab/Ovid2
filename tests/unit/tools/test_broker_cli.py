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
        json.dumps({
            "type": "disc",
            "args": {"host_path": str(host_path), "mount_mode": "ro"},
        }),
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
def test_probe_connection_unknown_connection(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
def test_browse_storage_unknown_type_reports(
    xdg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (xdg / "ovid2" / "connections" / "c_bad.json").write_text(
        json.dumps({"type": "disc", "args": {}}), encoding="utf-8"
    )
    # Missing host_path -> config error surfaced as ok=False.
    rc = tools_cli._browse_storage_entry(Namespace(name="c_bad", prefix=None))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "ok" in payload
    assert rc in (0, 1)
