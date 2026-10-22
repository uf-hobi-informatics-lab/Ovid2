"""Unit tests for ``ovid2 tools move`` — the driver-native successor to copy.

Same shape as :mod:`tests.unit.tools.test_copy` — argparse hooks,
host-mode dispatch attribute back-fill, extra-mount forwarding — plus
tests specific to the D3-driver differences:

- ``main`` uses :func:`ovid2.drivers.collector.collect_for_addresses`
  (not :func:`~ovid2.connections.disc_mounts.build_disc_mounts`).
- ``build_docker_command`` recognises ``tools move`` and mounts
  ``/data`` read-write (copy-family semantics).
- ``_build_ovid2_argv`` dispatches ``tools move`` through
  :func:`_build_tools_move_argv`, which emits ``--strict-types`` when
  the flag is set on the Namespace.
- The container-mode entry:
  - resolves both addresses via
    :func:`ovid2.drivers.registry.driver_from_address`;
  - calls
    :func:`ovid2.drivers.type_stamping.validate_route_types` before
    the loop;
  - emits ``table_started`` / ``table_completed`` / ``table_failed``
    events on the JSONL bus (same schema as the ETL family).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ovid2.tools import move as move_module


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_nss_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return static paths from ``ensure_nss_files`` for hermetic tests.

    Same fixture as :mod:`tests.unit.tools.test_copy` — the docker
    argv builder now touches the nss overlay and the Spark work/logs
    bind sources; stub both to avoid touching ``$OVID2_WORK_DIR``.
    """
    from ovid2.core.isolation import OvidInstance

    monkeypatch.setattr(
        OvidInstance,
        "ensure_nss_files",
        lambda self, *a, **kw: (Path("/tmp/passwd"), Path("/tmp/group")),
    )
    monkeypatch.setattr(
        OvidInstance,
        "ensure_directories",
        lambda self: None,
    )


def _mk_args(**overrides: Any) -> argparse.Namespace:
    """Build a Namespace matching what the move argparse emits."""
    base = argparse.Namespace(
        command="tools",
        from_address="src_addr",
        to_address="dst_addr",
        pipeline="acme",
        batch_id="20260610",
        tables=["demographic", "encounter"],
        engine="pyspark",
        strict_types=False,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _instance() -> Any:
    """Build a stand-in OvidInstance without touching the filesystem."""
    from ovid2.core.isolation import OvidInstance
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        return OvidInstance(instance_id="20260101-000000-testtt")


# ---------------------------------------------------------------------------
# Host-mode attribute back-fill (mirrors test_copy.py)
# ---------------------------------------------------------------------------


class TestRunHostModeAttributeBackfill:
    """``_run_host_mode`` must back-fill every etl-shape attribute."""

    def test_backfills_workers_for_cluster_mode(self) -> None:
        args = _mk_args()
        assert not hasattr(args, "workers")

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "cluster"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()), \
             mock.patch("ovid2.core.docker.submit_with_cluster",
                        return_value=0) as m_swc, \
             mock.patch("ovid2.core.docker.submit_to_docker",
                        return_value=0) as m_std:
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            rc = move_module.main(args)

        assert rc == 0
        m_swc.assert_called_once()
        m_std.assert_not_called()
        passed = m_swc.call_args.args[0]
        assert passed.workers == 25
        assert passed.command == "tools move"
        assert passed.input_address == "src_addr"
        assert passed.output_address == "dst_addr"

    def test_local_mode_uses_submit_to_docker(self) -> None:
        args = _mk_args()

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()), \
             mock.patch("ovid2.core.docker.submit_with_cluster") as m_swc, \
             mock.patch("ovid2.core.docker.submit_to_docker",
                        return_value=0) as m_std:
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            move_module.main(args)
        m_std.assert_called_once()
        m_swc.assert_not_called()

    def test_backfills_transformations_and_sf_defaults(self) -> None:
        args = _mk_args()

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()), \
             mock.patch("ovid2.core.docker.submit_to_docker",
                        return_value=0) as m_std:
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            move_module.main(args)
        passed = m_std.call_args.args[0]
        assert passed.transformations == []
        assert passed.sf_database is None
        assert passed.sf_account is None
        assert passed.sf_schema is None


def _empty_merged() -> Any:
    """Return an empty :class:`MergedRequirements`."""
    from ovid2.drivers.container import MergedRequirements
    return MergedRequirements()


# ---------------------------------------------------------------------------
# Collector integration
# ---------------------------------------------------------------------------


class TestCollectorIntegration:
    """``main`` must route auto-mount discovery through the D5 collector.

    That's the whole point of ``tools move`` vs ``tools copy``: the
    unified driver-based collector, not the disc-only helper.
    """

    def test_calls_collect_for_addresses(self) -> None:
        args = _mk_args()

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()) as m_col, \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            move_module.main(args)
        m_col.assert_called_once()
        (addr_list,), kwargs = m_col.call_args
        assert list(addr_list) == ["src_addr", "dst_addr"]
        assert "explicit_container_paths" in kwargs

    def test_forwards_explicit_container_paths(self, tmp_path: Path) -> None:
        """Explicit ``--extra-mount`` paths flow to the collector as a hint.

        The collector uses them to drop auto-mounts that would
        collide with an operator-supplied override — same policy the
        legacy ``build_disc_mounts`` already enforces.
        """
        (tmp_path / "a").mkdir()
        args = _mk_args(extra_mounts=[f"{tmp_path / 'a'}:/explicit"])

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()) as m_col, \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            move_module.main(args)
        _, kwargs = m_col.call_args
        assert kwargs["explicit_container_paths"] == ["/explicit"]

    def test_collector_mounts_reach_parsed_extra_mounts(
        self, tmp_path: Path,
    ) -> None:
        """Driver-side :class:`ExtraMount` values are reshaped to the tools shape."""
        from ovid2.drivers.container import (
            ExtraMount as DriverMount,
            MergedRequirements,
        )

        (tmp_path / "auto").mkdir()
        merged = MergedRequirements(
            mounts=(
                DriverMount(
                    host_path=str(tmp_path / "auto"),
                    container_path="/auto",
                    mode="rw",
                ),
            ),
        )
        args = _mk_args()

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=merged), \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            move_module.main(args)
        assert len(args.parsed_extra_mounts) == 1
        m = args.parsed_extra_mounts[0]
        assert m.container == "/auto"
        assert m.host == str(tmp_path / "auto")
        assert m.mode == "rw"


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


class TestArgparse:
    """The ``ovid2 tools move`` argparse shape."""

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        move_module.register(sub)
        return parser.parse_args(argv)

    def test_required_flags(self) -> None:
        ns = self._parse([
            "move", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "b", "--tables", "t1",
        ])
        assert ns.from_address == "src"
        assert ns.to_address == "dst"
        assert ns.pipeline == "p"
        assert ns.batch_id == "b"
        assert ns.tables == ["t1"]
        assert ns.engine == "pyspark"
        assert ns.strict_types is False

    def test_strict_types_flag(self) -> None:
        ns = self._parse([
            "move", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "b", "--tables", "t1",
            "--strict-types",
        ])
        assert ns.strict_types is True

    def test_extra_mount_flag_is_repeatable(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        ns = self._parse([
            "move", "--from", "s", "--to", "d",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t",
            "--extra-mount", f"{tmp_path / 'a'}:/mnt_a",
            "--extra-mount", f"{tmp_path / 'b'}:/mnt_b:ro",
        ])
        assert ns.extra_mounts == [
            f"{tmp_path / 'a'}:/mnt_a",
            f"{tmp_path / 'b'}:/mnt_b:ro",
        ]

    def test_missing_required_fails(self) -> None:
        with pytest.raises(SystemExit):
            self._parse(["move", "--from", "src"])


class TestExtraMountValidation:
    """``main`` validates ``--extra-mount`` specs before dispatch."""

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        move_module.register(sub)
        return parser.parse_args(argv)

    def test_rejects_reserved_container_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        ns = self._parse([
            "move", "--from", "s", "--to", "d",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t",
            "--extra-mount", f"{tmp_path}:/data",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False):
            rc = move_module.main(ns)
        assert rc == 2
        assert "reserved" in capsys.readouterr().err

    def test_rejects_missing_host_path(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        ns = self._parse([
            "move", "--from", "s", "--to", "d",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t",
            "--extra-mount", "/nope/nowhere:/copy_out",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False):
            rc = move_module.main(ns)
        assert rc == 2
        assert "does not exist" in capsys.readouterr().err

    def test_container_mode_skips_host_validation(self) -> None:
        ns = self._parse([
            "move", "--from", "s", "--to", "d",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t",
            "--extra-mount", "/host-only:/copy_out",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=True), \
             mock.patch.object(move_module, "_run_container_mode",
                               return_value=0) as m_cm:
            rc = move_module.main(ns)
        assert rc == 0
        m_cm.assert_called_once()
        assert not hasattr(ns, "parsed_extra_mounts")


# ---------------------------------------------------------------------------
# Docker argv + is_copy gate
# ---------------------------------------------------------------------------


class TestDockerArgv:
    """``build_docker_command`` and ``_build_ovid2_argv`` know about move."""

    def test_data_mounted_rw_for_move(self, tmp_path: Path) -> None:
        """``/data`` must be ``rw`` for ``tools move`` (copy-family semantics)."""
        from ovid2.core.docker import build_docker_command

        args = _mk_args()
        args.command = "tools move"
        args.parsed_extra_mounts = []
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(args, _instance())

        data_mounts = [tok for tok in cmd if ":/data:" in tok]
        assert data_mounts
        assert all(t.endswith(":rw") for t in data_mounts)

    def test_build_tools_move_argv_shape(self) -> None:
        from ovid2.core.docker import _build_ovid2_argv

        args = _mk_args()
        args.command = "tools move"
        args.parsed_extra_mounts = []
        tokens = _build_ovid2_argv(args)
        assert tokens[:2] == ["tools", "move"]
        assert "--from" in tokens
        assert tokens[tokens.index("--from") + 1] == "src_addr"
        assert "--to" in tokens
        assert tokens[tokens.index("--to") + 1] == "dst_addr"
        assert "--engine" in tokens
        # strict_types defaults False → flag NOT present
        assert "--strict-types" not in tokens

    def test_build_tools_move_argv_forwards_strict_types(self) -> None:
        from ovid2.core.docker import _build_ovid2_argv

        args = _mk_args(strict_types=True)
        args.command = "tools move"
        args.parsed_extra_mounts = []
        tokens = _build_ovid2_argv(args)
        assert "--strict-types" in tokens

    def test_build_tools_move_argv_forwards_extra_mount(
        self, tmp_path: Path,
    ) -> None:
        from ovid2.core.docker import _build_ovid2_argv
        from ovid2.tools.extra_mounts import ExtraMount

        args = _mk_args()
        args.command = "tools move"
        args.parsed_extra_mounts = [
            ExtraMount(host=str(tmp_path), container="/mv_out", mode="rw"),
        ]
        tokens = _build_ovid2_argv(args)
        assert "--extra-mount" in tokens
        assert tokens[tokens.index("--extra-mount") + 1] == f"{tmp_path}:/mv_out:rw"


# ---------------------------------------------------------------------------
# Driver-mount → tool-mount reshape
# ---------------------------------------------------------------------------


class TestDriverMountReshape:
    """`_driver_mount_to_tool_mount` converts the driver-side dataclass."""

    def test_reshape_ok(self, tmp_path: Path) -> None:
        from ovid2.drivers.container import ExtraMount as DriverMount

        d = DriverMount(host_path=str(tmp_path), container_path="/mv", mode="rw")
        got = move_module._driver_mount_to_tool_mount(d)
        assert got is not None
        assert got.host == str(tmp_path)
        assert got.container == "/mv"
        assert got.mode == "rw"

    def test_reshape_returns_none_on_bad_path(self) -> None:
        from ovid2.drivers.container import ExtraMount as DriverMount

        d = DriverMount(host_path="", container_path="/x", mode="rw")
        assert move_module._driver_mount_to_tool_mount(d) is None

    def test_reshape_returns_none_when_host_missing(self) -> None:
        """A non-existent host path is rejected by parse_extra_mount."""
        from ovid2.drivers.container import ExtraMount as DriverMount

        d = DriverMount(
            host_path="/nope/nowhere", container_path="/x", mode="rw",
        )
        assert move_module._driver_mount_to_tool_mount(d) is None


# ---------------------------------------------------------------------------
# Container-mode driver resolution + event emission
# ---------------------------------------------------------------------------


class TestContainerModeDriverResolution:
    """The container-mode loop routes reads/writes through drivers."""

    def test_uses_driver_from_address(self, tmp_path: Path) -> None:
        """``_run_container_mode`` must call ``driver_from_address`` for each address."""
        args = _mk_args(tables=["T1"])

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None), \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_dfa.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            rc = move_module._run_container_mode(args)

        assert rc == 0
        assert m_dfa.call_count == 2
        src.read.assert_called_once()
        dst.write.assert_called_once()

    def test_advisory_type_check_called(self, tmp_path: Path) -> None:
        args = _mk_args(tables=["T1"])

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None) as m_pi, \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock(); src.type_name = "disc"
            dst = mock.MagicMock(); dst.type_name = "disc"
            m_dfa.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            move_module._run_container_mode(args)

        m_pi.assert_called_once()
        _, kw = m_pi.call_args
        assert kw.get("strict") is False
        assert kw.get("from_address") == "src_addr"
        assert kw.get("to_address") == "dst_addr"

    def test_strict_types_flag_propagates(self, tmp_path: Path) -> None:
        args = _mk_args(tables=["T1"], strict_types=True)

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        side_effect=ValueError("mismatch")) as m_pi, \
             mock.patch("ovid2.core.loaders.load_engine"), \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            m_dfa.side_effect = [mock.MagicMock(), mock.MagicMock()]

            rc = move_module._run_container_mode(args)

        assert rc == 1
        m_pi.assert_called_once()
        _, kw = m_pi.call_args
        assert kw.get("strict") is True

    def test_failure_counted_but_run_continues(self, tmp_path: Path) -> None:
        """When a table read/write raises, the loop moves on but the run fails."""
        args = _mk_args(tables=["T1", "T2"])

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None), \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.side_effect = [RuntimeError("boom"), "df2"]
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_dfa.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            rc = move_module._run_container_mode(args)

        assert rc == 1  # at least one failure
        assert src.read.call_count == 2  # second table still attempted
        dst.write.assert_called_once()  # only the successful one

    def test_address_resolution_failure_returns_2(self, tmp_path: Path) -> None:
        """Missing address → exit 2, no engine touched."""
        args = _mk_args(tables=["T1"])

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address",
                        side_effect=FileNotFoundError("bad")), \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None), \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            rc = move_module._run_container_mode(args)

        assert rc == 2
        m_engine.assert_not_called()


# ---------------------------------------------------------------------------
# Table-spec parsing (LOGICAL[=SOURCE] grammar)
# ---------------------------------------------------------------------------


class TestTableSpecWiring:
    """The ``--tables`` grammar mirrors LOGICAL onto the destination.

    The parser lives in :mod:`ovid2.tools.table_specs`; its edge cases
    are covered exhaustively there. These tests exist to lock in that
    ``ovid2 tools move`` actually uses the parsed form — every entry
    in ``args._resolved_tables`` is a :class:`TableSpec`, the read
    side gets ``file_name`` and the write side gets ``table_name``.
    """

    def test_main_stashes_resolved_tables(self) -> None:
        args = _mk_args(tables=["HASH_TOKEN=*datavant_person_tokens*"])

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()), \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            move_module.main(args)

        assert len(args._resolved_tables) == 1
        spec = args._resolved_tables[0]
        assert spec.table_name == "HASH_TOKEN"
        assert spec.file_name == "*datavant_person_tokens*"

    def test_main_rejects_malformed_spec(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = _mk_args(tables=["=missing_logical"])

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False):
            rc = move_module.main(args)

        assert rc == 2
        assert "empty LOGICAL" in capsys.readouterr().err

    def test_container_mode_read_uses_file_name_write_uses_table_name(
        self, tmp_path: Path,
    ) -> None:
        """Read side must receive ``file_name``; write side ``table_name``."""
        args = _mk_args(tables=["HASH_TOKEN=*datavant_person_tokens*"])
        # Populate ``_resolved_tables`` the way ``main`` would.
        from ovid2.tools.table_specs import parse_table_specs
        args._resolved_tables = parse_table_specs(args.tables)

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None), \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_dfa.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            rc = move_module._run_container_mode(args)

        assert rc == 0
        # Read: 4th positional is ``file_name`` (source-side lookup).
        read_args, read_kwargs = src.read.call_args
        assert read_args[3] == "*datavant_person_tokens*"
        # And ``table_name`` kwarg is the LOGICAL half.
        assert read_kwargs.get("table_name") == "HASH_TOKEN"
        # Write: 5th positional is ``table_name`` (destination stem).
        write_args, _ = dst.write.call_args
        assert write_args[4] == "HASH_TOKEN"

    def test_container_mode_bare_name_defaults_source_to_logical(
        self, tmp_path: Path,
    ) -> None:
        """Legacy invocation (single field per --tables entry) is untouched."""
        args = _mk_args(tables=["DEMOGRAPHIC"])
        from ovid2.tools.table_specs import parse_table_specs
        args._resolved_tables = parse_table_specs(args.tables)

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None), \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_dfa.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            move_module._run_container_mode(args)

        read_args, _ = src.read.call_args
        write_args, _ = dst.write.call_args
        assert read_args[3] == "DEMOGRAPHIC"
        assert write_args[4] == "DEMOGRAPHIC"

    def test_container_mode_case_preserved(self, tmp_path: Path) -> None:
        """Destination stem case matches LOGICAL verbatim (both directions)."""
        args = _mk_args(tables=["hash_token=HASH_TOKEN.csv", "ENCOUNTER"])
        from ovid2.tools.table_specs import parse_table_specs
        args._resolved_tables = parse_table_specs(args.tables)

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_dfa, \
             mock.patch("ovid2.drivers.type_stamping.validate_route_types",
                        return_value=None), \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_dfa.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            move_module._run_container_mode(args)

        # Second positional (write) — table_name is 5th positional
        write_call_names = [c.args[4] for c in dst.write.call_args_list]
        assert write_call_names == ["hash_token", "ENCOUNTER"]
        read_call_files = [c.args[3] for c in src.read.call_args_list]
        assert read_call_files == ["HASH_TOKEN.csv", "ENCOUNTER"]


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
