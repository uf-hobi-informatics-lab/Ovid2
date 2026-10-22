"""Unit tests for ``ovid2 tools copy`` host-mode dispatch.

Regression: ``_run_host_mode`` used to omit ``workers`` from the
attributes it back-fills on the argparse ``Namespace`` before calling
:func:`ovid2.core.docker.submit_with_cluster`. The etl parser defines
``--workers`` and defaults it to 25, but the copy parser deliberately
does not (per the copy-kind ADR: ``workers`` is scrubbed). In cluster
mode, ``submit_with_cluster`` reads ``args.workers`` to size the
per-invocation Spark cluster and raised ``AttributeError`` on every
host-side ``ovid2 tools copy`` invocation — including every ``copy``
job dispatched by the web worker.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ovid2.tools import copy as copy_module


@pytest.fixture(autouse=True)
def _stub_nss_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return static paths from ``ensure_nss_files`` so tests stay hermetic.

    ``build_docker_command`` (exercised by
    :class:`TestExtraMountDockerEmission`) now calls
    :meth:`OvidInstance.ensure_nss_files` **and**
    :meth:`OvidInstance.ensure_directories` (the latter materialises
    the per-invocation Spark work / logs bind sources). Stub both to
    avoid touching ``$OVID2_WORK_DIR``.
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
    """Build a ``Namespace`` matching what the copy argparse emits."""
    base = argparse.Namespace(
        command="tools",  # top-level argparse sets this
        from_address="CHAR_DEMO",
        to_address="disc_input",
        pipeline="demo",
        batch_id="20260610",
        tables=["demographic", "encounter"],
        engine="pyspark",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _instance() -> Any:
    """Build a stand-in OvidInstance object without touching the filesystem."""
    from ovid2.core.isolation import OvidInstance
    with mock.patch("ovid2.core.isolation.OvidInstance.ensure_directories"):
        return OvidInstance(instance_id="20260101-000000-testtt")


def _empty_merged() -> Any:
    """Return an empty :class:`MergedRequirements`."""
    from ovid2.drivers.container import MergedRequirements
    return MergedRequirements()


class TestCollectorIntegration:
    """``main`` must route auto-mount discovery through the D5 collector.

    Regression guard for the Access-copy bug: ``copy`` used the
    disc-only :func:`~ovid2.connections.disc_mounts.build_disc_mounts`,
    which silently dropped every non-disc connection (Access, S3,
    Azure, …). A UCanAccess ``.accdb`` source therefore never got its
    directory bind-mounted into the container and failed with
    ``given file does not exist``. ``copy`` now uses the same
    driver-aware :func:`ovid2.drivers.collector.collect_for_addresses`
    that ``move`` already uses.
    """

    def test_calls_collect_for_addresses(self) -> None:
        args = _mk_args()
        args.extra_mounts = None

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()) as m_col, \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)
        m_col.assert_called_once()
        (addr_list,), kwargs = m_col.call_args
        assert list(addr_list) == ["CHAR_DEMO", "disc_input"]
        assert "explicit_container_paths" in kwargs

    def test_does_not_call_build_disc_mounts(self) -> None:
        """The legacy disc-only helper must no longer be on the copy path."""
        args = _mk_args()
        args.extra_mounts = None

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()), \
             mock.patch("ovid2.connections.disc_mounts.build_disc_mounts") as m_bdm, \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)
        m_bdm.assert_not_called()

    def test_forwards_explicit_container_paths(self, tmp_path: Path) -> None:
        """Explicit ``--extra-mount`` paths flow to the collector as a hint."""
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
            copy_module.main(args)
        _, kwargs = m_col.call_args
        assert kwargs["explicit_container_paths"] == ["/explicit"]

    def test_collector_mounts_reach_parsed_extra_mounts(
        self, tmp_path: Path,
    ) -> None:
        """Driver-side :class:`ExtraMount` values are reshaped to the tools shape.

        This is the exact path an Access source takes: the Access
        driver declares ``ExtraMount(host_path=/…/multum, …)`` and it
        must end up on ``parsed_extra_mounts`` → a ``-v`` flag.
        """
        from ovid2.drivers.container import (
            ExtraMount as DriverMount,
            MergedRequirements,
        )

        (tmp_path / "auto").mkdir()
        merged = MergedRequirements(
            mounts=(
                DriverMount(
                    host_path=str(tmp_path / "auto"),
                    container_path=str(tmp_path / "auto"),
                    mode="ro",
                ),
            ),
        )
        args = _mk_args()
        args.extra_mounts = None

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=merged), \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)
        assert len(args.parsed_extra_mounts) == 1
        m = args.parsed_extra_mounts[0]
        assert m.container == str(tmp_path / "auto")
        assert m.host == str(tmp_path / "auto")
        assert m.mode == "ro"


class TestRunHostModeAttributeBackfill:
    """``_run_host_mode`` must back-fill every etl-shape attribute."""

    def test_backfills_workers_for_cluster_mode(self) -> None:
        """``args.workers`` must exist before ``submit_with_cluster``.

        This is the regression the DEMO job hit — a raw
        ``AttributeError: 'Namespace' object has no attribute 'workers'``
        two seconds into the run, before any container work started.
        """
        args = _mk_args()
        assert not hasattr(args, "workers")

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "cluster"), \
             mock.patch("ovid2.core.docker.submit_with_cluster",
                        return_value=0) as m_swc, \
             mock.patch("ovid2.core.docker.submit_to_docker",
                        return_value=0) as m_std:
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            rc = copy_module.main(args)

        assert rc == 0
        m_swc.assert_called_once()
        m_std.assert_not_called()
        # The Namespace passed to submit_with_cluster must expose
        # workers — even though the copy argparse never defined it.
        passed_args = m_swc.call_args.args[0]
        assert hasattr(passed_args, "workers"), \
            "workers must be back-filled before submit_with_cluster"
        assert passed_args.workers == 25, \
            "default worker count must match the etl parser default"

    def test_backfills_all_etl_shape_attributes(self) -> None:
        """Every attribute ``build_docker_command`` reads must be set."""
        args = _mk_args()
        # None of these are set by the copy argparse.
        for attr in ("workers", "transformations", "sf_database",
                     "sf_account", "sf_schema", "input_address",
                     "output_address"):
            assert not hasattr(args, attr), f"pre-condition: {attr} unset"

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "cluster"), \
             mock.patch("ovid2.core.docker.submit_with_cluster",
                        return_value=0) as m_swc, \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)

        passed = m_swc.call_args.args[0]
        assert passed.workers == 25
        assert passed.transformations == []
        assert passed.sf_database is None
        assert passed.sf_account is None
        assert passed.sf_schema is None
        # The two-token subcommand dispatch key.
        assert passed.command == "tools copy"
        # from/to aliased onto etl-shape names.
        assert passed.input_address == "CHAR_DEMO"
        assert passed.output_address == "disc_input"

    def test_local_mode_also_backfills(self) -> None:
        """Non-cluster mode uses ``submit_to_docker`` — same contract."""
        args = _mk_args()

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.core.docker.submit_with_cluster") as m_swc, \
             mock.patch("ovid2.core.docker.submit_to_docker",
                        return_value=0) as m_std:
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)

        m_std.assert_called_once()
        m_swc.assert_not_called()
        # Even in local mode we back-fill workers so the Namespace is
        # a superset of the etl-parser shape.
        passed = m_std.call_args.args[0]
        assert passed.workers == 25

    def test_explicit_workers_is_preserved(self) -> None:
        """If a caller supplies ``workers`` we must not overwrite it.

        Guards against a future regression where someone flips the
        ``hasattr`` guard to an unconditional assignment.
        """
        args = _mk_args(workers=42)

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "cluster"), \
             mock.patch("ovid2.core.docker.submit_with_cluster",
                        return_value=0) as m_swc, \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)

        assert m_swc.call_args.args[0].workers == 42


class TestExtraMountArgparseHook:
    """``ovid2 tools copy --extra-mount`` is repeatable, validated in ``main``."""

    def _parse(self, argv: list[str]) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="cmd")
        copy_module.register(sub)
        return parser.parse_args(argv)

    def test_flag_is_repeatable(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        ns = self._parse([
            "copy", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "1",
            "--tables", "t1",
            "--extra-mount", f"{tmp_path / 'a'}:/mnt_a",
            "--extra-mount", f"{tmp_path / 'b'}:/mnt_b:ro",
        ])
        assert ns.extra_mounts == [
            f"{tmp_path / 'a'}:/mnt_a",
            f"{tmp_path / 'b'}:/mnt_b:ro",
        ]

    def test_absent_flag_defaults_to_none(self) -> None:
        ns = self._parse([
            "copy", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t1",
        ])
        assert ns.extra_mounts is None

    def test_main_parses_valid_specs_into_namespace(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        ns = self._parse([
            "copy", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t1",
            "--extra-mount", f"{tmp_path / 'out'}:/copy_out",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch.object(copy_module, "_run_host_mode", return_value=0):
            rc = copy_module.main(ns)
        assert rc == 0
        assert len(ns.parsed_extra_mounts) == 1
        assert ns.parsed_extra_mounts[0].container == "/copy_out"
        assert ns.parsed_extra_mounts[0].mode == "rw"

    def test_main_rejects_reserved_container_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        ns = self._parse([
            "copy", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t1",
            "--extra-mount", f"{tmp_path}:/data",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False):
            rc = copy_module.main(ns)
        assert rc == 2
        assert "reserved" in capsys.readouterr().err

    def test_main_rejects_missing_host_path(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        ns = self._parse([
            "copy", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t1",
            "--extra-mount", "/definitely/does/not/exist:/copy_out",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False):
            rc = copy_module.main(ns)
        assert rc == 2
        assert "does not exist" in capsys.readouterr().err

    def test_container_mode_skips_host_validation(
        self, tmp_path: Path,
    ) -> None:
        """Inside the container the mount is already applied — don't re-validate.

        If we re-parsed extra_mounts in container mode we'd fail on
        host-side paths that legitimately do not exist inside the
        container (the whole point is that the operator supplied a
        host path that the ETL image can't see).
        """
        ns = self._parse([
            "copy", "--from", "src", "--to", "dst",
            "--pipeline", "p", "--batch_id", "1", "--tables", "t1",
            "--extra-mount", "/host-only-path:/copy_out",
        ])
        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=True), \
             mock.patch.object(copy_module, "_run_container_mode",
                               return_value=0) as m_cm:
            rc = copy_module.main(ns)
        assert rc == 0
        m_cm.assert_called_once()
        # No parsed_extra_mounts attribute on the container path.
        assert not hasattr(ns, "parsed_extra_mounts")


class TestExtraMountDockerEmission:
    """``build_docker_command`` emits ``-v`` for each parsed extra mount."""

    def test_extra_mount_emitted_for_copy(self, tmp_path: Path) -> None:
        from ovid2.core.docker import build_docker_command
        from ovid2.tools.extra_mounts import ExtraMount

        args = _mk_args()
        args.command = "tools copy"
        args.parsed_extra_mounts = [
            ExtraMount(host=str(tmp_path), container="/copy_out", mode="rw"),
        ]
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(args, _instance())

        assert f"{tmp_path}:/copy_out:rw" in cmd, \
            f"extra mount missing from docker argv: {cmd}"
        # Sanity: the standard mounts are still there.
        assert any(":/data:rw" in tok for tok in cmd)
        assert any(":/app" in tok for tok in cmd)

    def test_extra_mount_not_emitted_for_etl(self, tmp_path: Path) -> None:
        """ETL now honours ``parsed_extra_mounts`` (auto-mount phase).

        Before the disc-mount / path-picker phase the ETL family
        deliberately ignored ``args.parsed_extra_mounts``. That was
        correct at the time (ETL operators had no way to set them).
        With disc auto-mount, ``_run_host_mode`` now populates the
        attribute from :func:`build_disc_mounts`, so the docker
        builder must forward it. The read-only guarantee on ``/data``
        is preserved — only the extra mounts flow through.
        """
        from ovid2.core.docker import build_docker_command
        from ovid2.tools.extra_mounts import ExtraMount

        args = _mk_args()
        # ``_build_etl_argv`` also reads input_address / output_address /
        # transformations / workers / sf_* — supply them so the test
        # actually exercises the mount-emission branch rather than
        # tripping on an unrelated AttributeError.
        args.command = "etl"
        args.input_address = "src"
        args.output_address = "dst"
        args.transformations = ["format"]
        args.workers = 25
        args.sf_database = None
        args.sf_account = None
        args.sf_schema = None
        args.parsed_extra_mounts = [
            ExtraMount(host=str(tmp_path), container="/auto_mount", mode="rw"),
        ]
        with mock.patch("ovid2.core.docker.SPARK_MODE", "local"):
            cmd = build_docker_command(args, _instance())

        # Auto-mount now applied to ETL too.
        assert any(":/auto_mount:rw" in tok for tok in cmd), \
            f"ETL must honour parsed_extra_mounts for disc auto-mount: {cmd}"
        # But /data is still :ro on the etl path — the read-only
        # invariant is preserved.
        data_mounts = [tok for tok in cmd if ":/data:" in tok]
        assert data_mounts and all(t.endswith(":ro") for t in data_mounts)

    def test_extra_mount_included_in_inner_argv(self, tmp_path: Path) -> None:
        """The ``ovid2 tools copy`` argv the ENTRYPOINT sees must round-trip."""
        from ovid2.core.docker import _build_ovid2_argv
        from ovid2.tools.extra_mounts import ExtraMount

        args = _mk_args()
        args.command = "tools copy"
        args.parsed_extra_mounts = [
            ExtraMount(host=str(tmp_path), container="/copy_out", mode="rw"),
        ]
        tokens = _build_ovid2_argv(args)
        assert "--extra-mount" in tokens
        idx = tokens.index("--extra-mount")
        assert tokens[idx + 1] == f"{tmp_path}:/copy_out:rw"


# ---------------------------------------------------------------------------
# Table-spec parsing (LOGICAL[=SOURCE] grammar)
# ---------------------------------------------------------------------------


class TestTableSpecWiring:
    """The ``--tables`` grammar mirrors LOGICAL onto the destination.

    Full grammar edge cases are covered in
    :mod:`tests.unit.tools.test_table_specs`. These tests lock in
    that ``ovid2 tools copy`` uses the parsed form — every entry in
    ``args._resolved_tables`` is a :class:`TableSpec`, the read side
    gets ``file_name`` and the write side gets ``table_name``.
    """

    def test_main_stashes_resolved_tables(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        args = _mk_args(tables=["HASH_TOKEN=*datavant_person_tokens*"])
        args.extra_mounts = None

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False), \
             mock.patch("ovid2.core.isolation.cleanup_stale_instances"), \
             mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.SPARK_MODE", "local"), \
             mock.patch("ovid2.drivers.collector.collect_for_addresses",
                        return_value=_empty_merged()), \
             mock.patch("ovid2.core.docker.submit_to_docker", return_value=0):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            copy_module.main(args)

        assert len(args._resolved_tables) == 1
        spec = args._resolved_tables[0]
        assert spec.table_name == "HASH_TOKEN"
        assert spec.file_name == "*datavant_person_tokens*"

    def test_main_rejects_malformed_spec(
        self, capsys: pytest.CaptureFixture[str],
    ) -> None:
        args = _mk_args(tables=["HASH_TOKEN=", "OK"])
        args.extra_mounts = None

        with mock.patch("ovid2.core.docker.is_inside_docker", return_value=False):
            rc = copy_module.main(args)

        assert rc == 2
        assert "empty SOURCE" in capsys.readouterr().err

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
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_df, \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_df.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            # Route through the real SparkEngineStager so pass-through
            # semantics are exercised (as they would be in production).
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            rc = copy_module._run_container_mode(args)

        assert rc == 0
        # Read: file_name positional (4th), table_name kwarg.
        read_args, read_kwargs = src.read.call_args
        assert read_args[3] == "*datavant_person_tokens*"
        assert read_kwargs.get("table_name") == "HASH_TOKEN"
        # Write: table_name positional (5th).
        write_args, _ = dst.write.call_args
        assert write_args[4] == "HASH_TOKEN"

    def test_container_mode_bare_name_defaults_source_to_logical(
        self, tmp_path: Path,
    ) -> None:
        """Legacy invocation (single-field --tables entries) unchanged."""
        args = _mk_args(tables=["DEMOGRAPHIC"])
        from ovid2.tools.table_specs import parse_table_specs
        args._resolved_tables = parse_table_specs(args.tables)

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_df, \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_df.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            copy_module._run_container_mode(args)

        read_args, _ = src.read.call_args
        write_args, _ = dst.write.call_args
        assert read_args[3] == "DEMOGRAPHIC"
        assert write_args[4] == "DEMOGRAPHIC"

    def test_container_mode_case_preserved_both_sides(
        self, tmp_path: Path,
    ) -> None:
        args = _mk_args(tables=["hash_token=HASH_TOKEN.csv", "ENCOUNTER"])
        from ovid2.tools.table_specs import parse_table_specs
        args._resolved_tables = parse_table_specs(args.tables)

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_df, \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.read.return_value = "df"
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_df.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            copy_module._run_container_mode(args)

        write_names = [c.args[4] for c in dst.write.call_args_list]
        read_files = [c.args[3] for c in src.read.call_args_list]
        # Tables are copied on a concurrent thread pool, so completion
        # order is non-deterministic — compare as multisets. The
        # invariant under test is the LOGICAL→file / LOGICAL→table
        # mapping (case preserved on both sides), not the sequence.
        assert sorted(write_names) == sorted(["hash_token", "ENCOUNTER"])
        assert sorted(read_files) == sorted(["HASH_TOKEN.csv", "ENCOUNTER"])

    def test_container_mode_expands_all_for_expandable_source(
        self, tmp_path: Path,
    ) -> None:
        """``--tables all`` expands via ``source.expand_all_tables()``."""
        args = _mk_args(tables=["all"])
        # ``main`` leaves ``_resolved_tables`` None for the sentinel.
        args._resolved_tables = None

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_df, \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "access"
            src.read.return_value = "df"
            src.expand_all_tables.return_value = ["Drugs", "Ndc", "Rxcui"]
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_df.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            rc = copy_module._run_container_mode(args)

        assert rc == 0
        src.expand_all_tables.assert_called_once()
        write_names = [c.args[4] for c in dst.write.call_args_list]
        # Concurrent copy → completion order is non-deterministic;
        # assert the set of tables written, not the order.
        assert sorted(write_names) == sorted(["Drugs", "Ndc", "Rxcui"])

    def test_container_mode_all_rejected_when_source_cannot_expand(
        self, tmp_path: Path,
    ) -> None:
        """A non-expandable source + ``all`` raises rather than copying nothing."""
        args = _mk_args(tables=["all"])
        args._resolved_tables = None

        with mock.patch("ovid2.core.isolation.OvidInstance") as m_inst, \
             mock.patch("ovid2.config.settings.WORK_DIR", tmp_path), \
             mock.patch("ovid2.drivers.registry.driver_from_address") as m_df, \
             mock.patch("ovid2.core.loaders.load_engine") as m_engine, \
             mock.patch("ovid2.display.bus.JsonlEventBus"):
            m_inst.return_value.instance_id = "20260805-000000-abcdef"
            src = mock.MagicMock()
            src.type_name = "disc"
            src.expand_all_tables.return_value = None
            dst = mock.MagicMock()
            dst.type_name = "disc"
            m_df.side_effect = [src, dst]
            m_engine.return_value.get_session.return_value = "session"
            from ovid2.core.engines.stager import SparkEngineStager
            m_engine.return_value.get_stager.return_value = SparkEngineStager(
                m_engine.return_value,
            )

            with pytest.raises(ValueError, match="cannot expand"):
                copy_module._run_container_mode(args)


class TestConcurrentCopy:
    """The per-table copy loop fans out across a thread pool.

    Regression guard for the single-worker bottleneck: an Access
    ``.accdb`` (and any single-partition source) reads one table on one
    Spark executor, so the serial per-table loop left 24 of 25 workers
    idle. ``_copy_tables_concurrently`` submits several tables'
    ``persist`` actions from independent driver threads so Spark runs
    the single-task jobs in parallel.
    """

    def _stager(self) -> Any:
        """A pass-through stager whose materialise/persist are recorded."""
        stager = mock.MagicMock()
        stager.materialise.side_effect = lambda *a, **kw: ("df", a, kw)
        return stager

    def _specs(self, names: list[str]) -> Any:
        from ovid2.tools.table_specs import TableSpec
        return [TableSpec(table_name=n, file_name=n) for n in names]

    def test_all_tables_copied_under_concurrency(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "4")
        stager = self._stager()
        src = mock.MagicMock()
        dst = mock.MagicMock()
        specs = self._specs([f"t{i}" for i in range(20)])
        args = _mk_args()

        failures = copy_module._copy_tables_concurrently(
            specs, stager, src, dst, args,
        )

        assert failures == 0
        # Every table materialised + persisted exactly once.
        assert stager.materialise.call_count == 20
        assert stager.persist.call_count == 20
        persisted = {c.args[4] for c in stager.persist.call_args_list}
        assert persisted == {f"t{i}" for i in range(20)}

    def test_concurrent_failures_are_counted_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failing table increments the count; others still copy."""
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "4")
        stager = self._stager()

        def _persist(df: Any, driver: Any, pipeline: str, batch_id: str,
                     table_name: str, delimiter: Any = None) -> None:
            if table_name in {"t3", "t7"}:
                raise RuntimeError(f"boom {table_name}")

        stager.persist.side_effect = _persist
        src = mock.MagicMock()
        dst = mock.MagicMock()
        specs = self._specs([f"t{i}" for i in range(10)])
        args = _mk_args()

        failures = copy_module._copy_tables_concurrently(
            specs, stager, src, dst, args,
        )

        assert failures == 2
        # All 10 were attempted despite the 2 failures.
        assert stager.persist.call_count == 10

    def test_concurrency_one_uses_serial_path(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Concurrency 1 keeps the original thread-free serial behaviour."""
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "1")
        stager = self._stager()
        src = mock.MagicMock()
        dst = mock.MagicMock()
        specs = self._specs(["a", "b", "c"])
        args = _mk_args()

        with mock.patch("ovid2.tools.copy.ThreadPoolExecutor") as m_pool:
            failures = copy_module._copy_tables_concurrently(
                specs, stager, src, dst, args,
            )

        assert failures == 0
        # Serial path must NOT construct a thread pool.
        m_pool.assert_not_called()
        # Serial order is preserved.
        persisted = [c.args[4] for c in stager.persist.call_args_list]
        assert persisted == ["a", "b", "c"]

    def test_concurrency_capped_at_table_count(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A single table caps concurrency to 1 → serial, no pool built."""
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "32")
        stager = self._stager()
        src = mock.MagicMock()
        dst = mock.MagicMock()
        specs = self._specs(["only"])
        args = _mk_args()

        with mock.patch("ovid2.tools.copy.ThreadPoolExecutor") as m_pool:
            copy_module._copy_tables_concurrently(specs, stager, src, dst, args)
        m_pool.assert_not_called()

    def test_env_override_parsing(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The concurrency knob honours the env var and clamps sanely."""
        monkeypatch.delenv("OVID2_COPY_TABLE_CONCURRENCY", raising=False)
        assert (
            copy_module._copy_table_concurrency()
            == copy_module._DEFAULT_COPY_TABLE_CONCURRENCY
        )
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "16")
        assert copy_module._copy_table_concurrency() == 16
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "0")
        assert copy_module._copy_table_concurrency() == 1
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "not-a-number")
        assert (
            copy_module._copy_table_concurrency()
            == copy_module._DEFAULT_COPY_TABLE_CONCURRENCY
        )

    def test_emits_per_table_events_for_live_console(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each copied table must emit table.started + table.completed.

        This is what makes a copy run's live console fill its per-table
        roster (the copy has no CDM roster to pre-seed, so these events
        are how the run page discovers the tables). A failing table must
        emit table.failed instead of completed.
        """
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "4")
        stager = self._stager()

        def _persist(df: Any, driver: Any, pipeline: str, batch_id: str,
                     table_name: str, delimiter: Any = None) -> None:
            if table_name == "t2":
                raise RuntimeError("boom t2")

        stager.persist.side_effect = _persist
        src = mock.MagicMock()
        dst = mock.MagicMock()
        specs = self._specs(["t0", "t1", "t2", "t3"])
        args = _mk_args()

        events: list[Any] = []
        bus = mock.MagicMock()
        bus.emit.side_effect = lambda ev: events.append(ev)

        failures = copy_module._copy_tables_concurrently(
            specs, stager, src, dst, args, bus=bus, instance_id="inst-1",
        )

        assert failures == 1
        by_kind: dict[str, set[str]] = {}
        for ev in events:
            by_kind.setdefault(ev.kind, set()).add(ev.payload["table"])
        # Every table announces a start.
        assert by_kind.get("table.started") == {"t0", "t1", "t2", "t3"}
        # The three good tables complete; the failing one fails.
        assert by_kind.get("table.completed") == {"t0", "t1", "t3"}
        assert by_kind.get("table.failed") == {"t2"}

    def test_no_bus_means_no_events(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Omitting the bus keeps the loop event-free (back-compat)."""
        monkeypatch.setenv("OVID2_COPY_TABLE_CONCURRENCY", "2")
        stager = self._stager()
        specs = self._specs(["a", "b"])
        # Should not raise despite no bus.
        failures = copy_module._copy_tables_concurrently(
            specs, stager, mock.MagicMock(), mock.MagicMock(), _mk_args(),
        )
        assert failures == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
