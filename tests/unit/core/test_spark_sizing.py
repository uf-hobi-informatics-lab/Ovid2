"""Unit tests for host-aware sizing (DuckDB container shape)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_duckdb_container_sizing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """75% of host RAM, all CPUs, Workers = concurrent tables."""
    from ovid2.config import spark_sizing

    for var in ("OVID2_DUCKDB_CPUS", "OVID2_DUCKDB_MEMORY_FRACTION"):
        monkeypatch.delenv(var, raising=False)
    s = spark_sizing.duckdb_container_sizing(25, cpus=96, memory_gb=958)
    assert s == {
        "memory_gb": 718,
        "cpus": 96,
        "threads": 96,
        "table_concurrency": 25,
        "engine_memory_gb": 610,
    }
    assert (
        spark_sizing.duckdb_container_sizing(None, cpus=8, memory_gb=64)["table_concurrency"] == 8
    )


@pytest.mark.unit
def test_duckdb_container_sizing_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env overrides narrow CPUs and the RAM fraction."""
    from ovid2.config import spark_sizing

    monkeypatch.setenv("OVID2_DUCKDB_CPUS", "32")
    monkeypatch.setenv("OVID2_DUCKDB_MEMORY_FRACTION", "0.5")
    s = spark_sizing.duckdb_container_sizing(4, cpus=96, memory_gb=1000)
    assert s["cpus"] == 32 and s["memory_gb"] == 500 and s["table_concurrency"] == 4
