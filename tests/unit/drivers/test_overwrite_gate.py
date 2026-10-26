"""Executor-side overwrite gate (address = exact location).

The output address is the exact write location, so writing into an
output that already holds tables needs ``--overwrite``. Covers:

- :meth:`Driver.existing_tables` (disc: present / empty / missing);
- :func:`ovid2.drivers.overwrite.assert_output_writable`;
- ``ovid2 tools probe-output`` (the web tier's broker);
- ``--overwrite`` on every run CLI and its host → container forwarding.
"""

from __future__ import annotations

import argparse
import json
from argparse import Namespace
from pathlib import Path

import pytest

from ovid2.drivers.disc.driver import DiscDriver
from ovid2.drivers.overwrite import OutputNotEmptyError, assert_output_writable


def _disc(root: Path, sub: str = "") -> DiscDriver:
    return DiscDriver(
        args={"host_path": str(root), "container_path": "/nonexistent-ovid2"},
        secrets={},
        location={"path": sub} if sub else {},
    )


@pytest.mark.unit
def test_existing_tables_disc(tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    assert _disc(tmp_path, "out").existing_tables() == []
    (tmp_path / "out" / "DEMOGRAPHIC.csv").write_text("a\n")
    (tmp_path / "out" / "_SUCCESS").write_text("")
    assert _disc(tmp_path, "out").existing_tables() == ["DEMOGRAPHIC"]
    # A missing output folder is simply empty (it will be created).
    assert _disc(tmp_path, "nope").existing_tables() == []


@pytest.mark.unit
def test_gate_raises_on_occupied_output(tmp_path: Path) -> None:
    (tmp_path / "ENCOUNTER.csv").write_text("a\n")
    with pytest.raises(OutputNotEmptyError) as excinfo:
        assert_output_writable("out", overwrite=False, driver=_disc(tmp_path))
    assert excinfo.value.tables == ["ENCOUNTER"]
    assert "--overwrite" in str(excinfo.value)
    assert assert_output_writable("out", overwrite=True, driver=_disc(tmp_path)) == ["ENCOUNTER"]


@pytest.mark.unit
def test_gate_only_tables_filter(tmp_path: Path) -> None:
    (tmp_path / "ENCOUNTER.csv").write_text("a\n")
    # health writes only dcq_results: an unrelated table does not block.
    assert (
        assert_output_writable(
            "out", overwrite=False, driver=_disc(tmp_path), only_tables=["dcq_results"]
        )
        == []
    )
    (tmp_path / "dcq_results.csv").write_text("a\n")
    with pytest.raises(OutputNotEmptyError):
        assert_output_writable(
            "out", overwrite=False, driver=_disc(tmp_path), only_tables=["DCQ_RESULTS"]
        )


@pytest.mark.unit
def test_gate_unknown_occupancy_never_blocks() -> None:
    class _Opaque:
        type_name = "opaque"

        def existing_tables(self) -> None:
            return None

    assert assert_output_writable("x", overwrite=False, driver=_Opaque()) is None


@pytest.mark.unit
def test_probe_output_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ovid2.cli import tools as tools_cli

    conns = tmp_path / "connections"
    configs = tmp_path / "configs"
    conns.mkdir()
    configs.mkdir()
    monkeypatch.setenv("OVID2_CONNECTIONS_DIR", str(conns))
    monkeypatch.setenv("OVID2_CONFIGS_DIR", str(configs))
    data = tmp_path / "data"
    (data / "out").mkdir(parents=True)
    (data / "out" / "DEATH.csv").write_text("a\n")
    (conns / "d.json").write_text(json.dumps({"type": "disc", "args": {"host_path": str(data)}}))
    (configs / "o.json").write_text(json.dumps({"connection": "d", "location": {"path": "out"}}))

    rc = tools_cli._probe_output_entry(Namespace(address="o"))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert payload == {
        "ok": True,
        "known": True,
        "type": "disc",
        "tables": ["DEATH"],
        "message": None,
    }


def _parser_for(module_name: str) -> argparse.ArgumentParser:
    import importlib

    module = importlib.import_module(module_name)
    root = argparse.ArgumentParser()
    subs = root.add_subparsers(dest="command")
    module.register(subs)
    return root


@pytest.mark.unit
@pytest.mark.parametrize(
    ("module", "argv"),
    [
        (
            "ovid2.cli.etl",
            [
                "etl",
                "--input_address",
                "a",
                "--output_address",
                "b",
                "--pipeline",
                "p",
                "--batch_id",
                "x/y",
                "--overwrite",
            ],
        ),
        (
            "ovid2.cli.health",
            ["health", "--pipeline", "p", "--batch_id", "x/y", "--overwrite"],
        ),
        (
            "ovid2.cli.analysis",
            [
                "analysis",
                "--input_address",
                "a",
                "--output_address",
                "b",
                "--pipeline",
                "p",
                "--batch_id",
                "x",
                "--overwrite",
            ],
        ),
        (
            "ovid2.cli.dq_checks",
            ["dq_checks", "--pipeline", "p", "--batch_id", "x", "--overwrite"],
        ),
    ],
)
def test_run_clis_accept_overwrite(module: str, argv: list[str]) -> None:
    ns = _parser_for(module).parse_args(argv)
    assert ns.overwrite is True


@pytest.mark.unit
@pytest.mark.parametrize("module", ["ovid2.tools.copy", "ovid2.tools.move"])
def test_copy_move_accept_overwrite(module: str) -> None:
    ns = _parser_for(module).parse_args(
        [
            module.rsplit(".", 1)[-1],
            "--from",
            "a",
            "--to",
            "b",
            "--pipeline",
            "p",
            "--batch_id",
            "x",
            "--tables",
            "T",
            "--overwrite",
        ]
    )
    assert ns.overwrite is True


@pytest.mark.unit
@pytest.mark.parametrize("command", ["etl", "health", "tools copy", "tools move"])
def test_overwrite_survives_container_hop(command: str) -> None:
    from ovid2.core import docker as docker_module

    args = Namespace(
        command=command,
        input_address="in",
        output_address="out",
        pipeline="p",
        batch_id="acct/x",
        tables=["T"],
        transformations=["format"],
        engine="pyspark",
        overwrite=True,
    )
    assert docker_module._build_ovid2_argv(args)[-1] == "--overwrite"
    args.overwrite = False
    assert "--overwrite" not in docker_module._build_ovid2_argv(args)
