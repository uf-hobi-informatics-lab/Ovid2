"""``ovid2 etl`` host mode skips the Spark cluster for non-Spark engines."""

from __future__ import annotations

import argparse
from unittest import mock

import pytest

from ovid2.cli import etl as etl_module

pytestmark = pytest.mark.unit


def _args(engine: str) -> argparse.Namespace:
    return argparse.Namespace(
        command="etl",
        engine=engine,
        input_address=None,
        output_address=None,
    )


@pytest.mark.parametrize("engine", ["snowpark"])
def test_non_spark_engine_runs_single_container(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ovid2.config.settings.SPARK_MODE", "cluster")
    args = _args(engine)
    with (
        mock.patch.object(etl_module, "cleanup_stale_instances"),
        mock.patch.object(etl_module, "submit_with_cluster") as m_cluster,
        mock.patch.object(etl_module, "submit_to_docker", return_value=0) as m_single,
    ):
        assert etl_module._run_host_mode(args) == 0
    m_cluster.assert_not_called()
    m_single.assert_called_once()
    assert args.no_spark_cluster is True


def test_spark_engine_still_starts_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ovid2.config.settings.SPARK_MODE", "cluster")
    args = _args("pyspark")
    with (
        mock.patch.object(etl_module, "cleanup_stale_instances"),
        mock.patch.object(etl_module, "submit_with_cluster", return_value=0) as m_cluster,
        mock.patch.object(etl_module, "submit_to_docker") as m_single,
    ):
        assert etl_module._run_host_mode(args) == 0
    m_cluster.assert_called_once()
    m_single.assert_not_called()
