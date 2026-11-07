"""Unit tests for :mod:`ovid2.config.spark_sizing` (host-aware Spark defaults)."""

from __future__ import annotations

import pytest

from ovid2.config import spark_sizing as sz

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OVID2_HOST_CPUS",
        "OVID2_HOST_MEMORY_GB",
        "OVID2_SPARK_MEMORY_FRACTION",
        "OVID2_SPARK_ALLOW_OVERSUBSCRIBE",
        "OVID2_SPARK_CORES",
        "OVID2_SPARK_LOCAL_DRIVER_MEMORY_GB",
    ):
        monkeypatch.delenv(name, raising=False)


def test_96_core_958g_host_caps_25_workers() -> None:
    # by_cpu = 96 // 8 = 12; by_mem = (958*0.75 - 40) // 60 = 11
    assert sz.max_cluster_workers(cpus=96, memory_gb=958) == 11
    assert sz.effective_worker_count(25, cpus=96, memory_gb=958) == 11


def test_cpu_bound_host() -> None:
    assert sz.max_cluster_workers(cpus=16, memory_gb=4096) == 2


def test_requested_below_cap_is_kept() -> None:
    assert sz.effective_worker_count(3, cpus=96, memory_gb=958) == 3


def test_tiny_host_never_below_one() -> None:
    assert sz.max_cluster_workers(cpus=2, memory_gb=8) == 1


def test_oversubscribe_escape_hatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_SPARK_ALLOW_OVERSUBSCRIBE", "1")
    assert sz.effective_worker_count(25, cpus=96, memory_gb=958) == 25


def test_memory_fraction_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_SPARK_MEMORY_FRACTION", "0.5")
    # (958*0.5 - 40) // 60 = 7
    assert sz.max_cluster_workers(cpus=96, memory_gb=958) == 7


def test_invalid_fraction_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_SPARK_MEMORY_FRACTION", "7")
    assert sz.max_cluster_workers(cpus=96, memory_gb=958) == 11


def test_host_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_HOST_CPUS", "40")
    monkeypatch.setenv("OVID2_HOST_MEMORY_GB", "256")
    assert sz.host_cpu_count() == 40
    assert sz.host_memory_gb() == 256


def test_local_cores_default_is_all_cpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_HOST_CPUS", "48")
    assert sz.local_cores() == 48
    monkeypatch.setenv("OVID2_SPARK_CORES", "12")
    assert sz.local_cores() == 12


@pytest.mark.parametrize(
    ("memory_gb", "expected"),
    [(958, 192), (128, 48), (16, sz.LOCAL_DRIVER_MEMORY_FLOOR_GB)],
)
def test_local_driver_memory(memory_gb: int, expected: int) -> None:
    assert sz.local_driver_memory_gb(memory_gb) == expected


def test_local_driver_memory_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OVID2_SPARK_LOCAL_DRIVER_MEMORY_GB", "64")
    assert sz.local_driver_memory_gb(958) == 64


def test_host_memory_reads_proc() -> None:
    assert sz.host_memory_gb() >= 1
    assert sz.host_cpu_count() >= 1
