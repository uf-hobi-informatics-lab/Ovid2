"""Unit tests for the ``ovid2 etl --local`` in-process bypass.

Scope: the Docker-less quick-start entry point. On the host, ``ovid2 etl``
normally dispatches to Docker (``_run_host_mode``). The ``--local`` flag —
and the equivalent ``OVID2_EXECUTION_MODE=inprocess`` env var — must route
the run straight to the in-process pipeline (``_run_container_mode``)
without ever touching Docker, so a developer can run a disc→disc pipeline
on a laptop with only the ``[spark]`` extra + a JDK.

These tests assert the *dispatch decision* (no Docker call), not a real
Spark run — the end-to-end local run is covered by the integration suite
and the manual verification recorded in the CHANGELOG.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ovid2.cli import etl as etl_module


def _mk_args(**overrides: Any) -> argparse.Namespace:
    """Build a ``Namespace`` matching the ETL argparse output."""
    base = argparse.Namespace(
        input_address="disc_input",
        output_address="disc_output",
        pipeline="demo",
        batch_id="20260101",
        tables=["all"],
        transformations=["all"],
        engine="pyspark",
        workers=25,
        sf_database=None,
        sf_account=None,
        sf_schema=None,
        cdm=None,
        stderr_log=False,
        local=False,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestLocalBypass:
    """``--local`` and ``OVID2_EXECUTION_MODE`` route in-process, no Docker."""

    def test_local_flag_runs_in_process_without_docker(self) -> None:
        """``--local`` reaches ``_run_container_mode`` and never calls Docker."""
        args = _mk_args(local=True)

        with (
            mock.patch.object(etl_module, "_run_container_mode", return_value=0) as m_container,
            mock.patch.object(etl_module, "_run_host_mode") as m_host,
            mock.patch.object(etl_module, "is_inside_docker") as m_docker_check,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_container.assert_called_once_with(args)
        m_host.assert_not_called()
        # The bypass short-circuits before the Docker probe is consulted.
        m_docker_check.assert_not_called()

    def test_env_var_runs_in_process_without_docker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``OVID2_EXECUTION_MODE=inprocess`` has the same effect as ``--local``."""
        monkeypatch.setenv("OVID2_EXECUTION_MODE", "inprocess")
        args = _mk_args(local=False)

        with (
            mock.patch.object(etl_module, "_run_container_mode", return_value=0) as m_container,
            mock.patch.object(etl_module, "_run_host_mode") as m_host,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_container.assert_called_once_with(args)
        m_host.assert_not_called()

    def test_env_var_is_case_insensitive_and_trimmed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``  InProcess  `` still enables the bypass."""
        monkeypatch.setenv("OVID2_EXECUTION_MODE", "  InProcess  ")
        args = _mk_args(local=False)
        assert etl_module._wants_inprocess(args) is True

    def test_default_still_dispatches_to_docker_on_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without ``--local``/env, host runs still go through Docker."""
        monkeypatch.delenv("OVID2_EXECUTION_MODE", raising=False)
        args = _mk_args(local=False)

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
            mock.patch.object(etl_module, "_run_container_mode") as m_container,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_host.assert_called_once_with(args)
        m_container.assert_not_called()

    def test_unset_env_defaults_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No flag and no env var → not in-process."""
        monkeypatch.delenv("OVID2_EXECUTION_MODE", raising=False)
        assert etl_module._wants_inprocess(_mk_args(local=False)) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([str(Path(__file__)), "-q"]))
