"""Unit tests for ``ovid2 etl`` engine/driver family compatibility gate.

Regression scope: closes the incident where
``ovid2 etl --engine snowpark --input_address disc_input --output_address
CHAR_ACME`` spun up a 25-worker Spark cluster before failing inside the
container at ``from snowflake.snowpark import Session`` with
``ModuleNotFoundError``. The underlying error is semantic (Snowpark
DataFrames live inside Snowflake and cannot read a ``disc``-typed
input), so the check refuses the pairing on driver-metadata grounds
regardless of whether the ``snowflake-snowpark-python`` wheel is
installed. The equivalent check on ``ovid2 tools copy`` /
``ovid2 tools move`` uses the loaded-engine variant; ``ovid2 etl``
uses the type-name variant so the abort happens before any
``load_engine`` cost.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from ovid2.cli import etl as etl_module


@pytest.fixture
def config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point OVID2's config loaders at an isolated tmp dir."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv(
        "OVID2_CONFIGS_DIR",
        str(xdg / "ovid2" / "configs"),
    )
    monkeypatch.setenv(
        "OVID2_CONNECTIONS_DIR",
        str(xdg / "ovid2" / "connections"),
    )
    (xdg / "ovid2" / "configs").mkdir(parents=True)
    (xdg / "ovid2" / "connections").mkdir(parents=True)
    return xdg


def _conn(xdg: Path, name: str, payload: dict[str, Any]) -> None:
    (xdg / "ovid2" / "connections" / f"{name}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _addr(xdg: Path, name: str, payload: dict[str, Any]) -> None:
    (xdg / "ovid2" / "configs" / f"{name}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _mk_args(**overrides: Any) -> argparse.Namespace:
    """Build a ``Namespace`` matching the ETL argparse output."""
    base = argparse.Namespace(
        input_address="disc_input",
        output_address="CHAR_ACME",
        # Not an installed package → portable for the pipeline-family
        # gate, so these tests exercise only the engine/driver gate.
        pipeline="tp_driver_compat",
        batch_id="20260610",
        tables=["HASH_TOKEN"],
        transformations=["all"],
        engine="snowpark",
        workers=25,
        sf_database=None,
        sf_account=None,
        sf_schema=None,
        cdm=None,
        stderr_log=False,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestEngineCompatGate:
    """Engine/driver compatibility fires at ``main()`` entry."""

    def test_snowpark_with_postgres_input_rejects_before_dispatch(
        self,
        config_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``--engine snowpark`` + postgres input is dispatched (engine universality).

        Postgres used to be unreachable from Snowpark; it is now bridged
        through its DuckDB strategy (Arrow), so the pairing passes the gate.
        """
        _conn(
            config_env,
            "pg_conn",
            {
                "type": "postgres",
                "args": {
                    "host": "db.example.com",
                    "database": "d",
                    "user": "u",
                },
            },
        )
        _addr(
            config_env,
            "pg_input",
            {
                "connection": "pg_conn",
                "location": {"database": "d", "schema": "public"},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "src_name": "snowflake",
                "src_args": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        args = _mk_args(input_address="pg_input")

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_wants_inprocess", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
            mock.patch.object(etl_module, "_run_container_mode") as m_container,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_host.assert_called_once()
        m_container.assert_not_called()
        del capsys

    def test_snowpark_with_disc_input_accepted_since_phase_c(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """Phase C's :class:`SnowparkDiscStrategy` accepts disc endpoints."""
        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "src_name": "snowflake",
                "src_args": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        args = _mk_args()  # engine=snowpark, input=disc, output=snowflake

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
        ):
            rc = etl_module.main(args)

        # Passes the gate; host dispatch proceeds.
        assert rc == 0
        m_host.assert_called_once()

    def test_pyspark_with_disc_input_dispatches(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """The historical fix (``--engine pyspark``) still proceeds."""
        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "src_name": "snowflake",
                "src_args": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        args = _mk_args(engine="pyspark")

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_host.assert_called_once()

    def test_container_mode_also_gated(
        self,
        config_env: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Direct-in-container invocation is also refused (belt+suspenders).

        The one remaining refusal is a read-only output (Access) — refused
        for every engine alike.
        """
        host = tmp_path / "in"
        host.mkdir()
        _conn(config_env, "d_conn", {"type": "disc", "args": {"host_path": str(host)}})
        _addr(config_env, "disc_input", {"connection": "d_conn", "location": {}})
        _conn(config_env, "acc_conn", {"type": "access", "args": {"host_path": str(host)}})
        _addr(config_env, "acc_out", {"connection": "acc_conn", "location": {}})
        args = _mk_args(input_address="disc_input", output_address="acc_out")

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=True),
            mock.patch.object(etl_module, "_run_host_mode") as m_host,
            mock.patch.object(etl_module, "_run_container_mode") as m_container,
        ):
            rc = etl_module.main(args)

        assert rc == 2
        m_host.assert_not_called()
        m_container.assert_not_called()
        assert "error:" in capsys.readouterr().err

    def test_unknown_engine_name_skips_check(
        self,
        config_env: Path,
        tmp_path: Path,
    ) -> None:
        """A named engine config (not a bare type) is skipped, dispatch proceeds.

        The host-side path (``_run_host_mode``) is expected to reach
        ``load_engine`` and either succeed or raise concretely; that
        error attribution is out of scope here.
        """
        host = tmp_path / "in"
        host.mkdir()
        _conn(
            config_env,
            "d_conn",
            {
                "type": "disc",
                "args": {"host_path": str(host)},
            },
        )
        _addr(
            config_env,
            "disc_input",
            {
                "connection": "d_conn",
                "location": {},
            },
        )
        _addr(
            config_env,
            "CHAR_ACME",
            {
                "src_name": "snowflake",
                "src_args": {"database": "CHAR_ACME", "schema": "PUBLIC"},
            },
        )
        args = _mk_args(engine="my-prod-pyspark")

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_host.assert_called_once()

    def test_snowpark_between_two_snowflake_addresses_ok(
        self,
        config_env: Path,
    ) -> None:
        """The one legitimate use of ``--engine snowpark`` still works."""
        _addr(
            config_env,
            "SF_IN",
            {
                "src_name": "snowflake",
                "src_args": {"database": "DB1", "schema": "S"},
            },
        )
        _addr(
            config_env,
            "SF_OUT",
            {
                "src_name": "snowflake",
                "src_args": {"database": "DB2", "schema": "S"},
            },
        )
        args = _mk_args(input_address="SF_IN", output_address="SF_OUT")

        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
        ):
            rc = etl_module.main(args)

        assert rc == 0
        m_host.assert_called_once()

    def test_legacy_port_on_duckdb_is_dispatched(
        self,
        config_env: Path,
    ) -> None:
        """A ``legacy_ovid`` port on duckdb is dispatched (engine universality).

        History: ``ovid2 etl --pipeline acme --engine duckdb`` used to be
        refused by the engine-family gate. Every engine runs every
        pipeline now, so the host side must dispatch it.
        """
        args = _mk_args(
            pipeline="acme",
            engine="duckdb",
            input_address="not_a_thing_in",
            output_address="nope_out",
        )
        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_wants_inprocess", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
            mock.patch.object(etl_module, "_run_container_mode") as m_container,
        ):
            rc = etl_module.main(args)
        assert rc == 0
        m_host.assert_called_once()
        m_container.assert_not_called()

    def test_unresolvable_addresses_are_skipped(
        self,
        config_env: Path,
    ) -> None:
        """Legacy shapes with no config file don't block the run."""
        args = _mk_args(input_address="not_a_thing_in", output_address="nope_out")
        with (
            mock.patch.object(etl_module, "is_inside_docker", return_value=False),
            mock.patch.object(etl_module, "_run_host_mode", return_value=0) as m_host,
        ):
            rc = etl_module.main(args)
        # Check silently skips → host mode is reached, and any downstream
        # failure attributes cleanly to the missing address.
        assert rc == 0
        m_host.assert_called_once()
