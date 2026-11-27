"""Host-aware Spark sizing (pure, web-safe — no pyspark import).

The pre-2026-10 defaults hard-coded a 25-worker × 8-core × 60 GiB
cluster. On a 96-core / ~1 TiB host that declares 200 cores (2×
CPU oversubscription) and 1.5 TiB of container memory limits (more
than physical RAM), so executors thrash and the kernel may OOM-kill
workers. This module derives safe defaults from the host instead:

* **Cluster mode** keeps the proven per-worker shape (8 cores, 56 GiB
  Spark memory, 60 GiB container limit, 48+8 GiB executor) and caps
  the *number* of workers so the cluster fits the host
  (:func:`effective_worker_count`).
* **Local mode** uses every core (``local[N]`` with ``N = cpu_count``)
  and a driver heap sized from host RAM (:func:`local_driver_memory_gb`)
  because the driver *is* the executor there.

Every knob is overridable via environment variable; operators who
really want oversubscription set ``OVID2_SPARK_ALLOW_OVERSUBSCRIBE=1``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: Per-worker shape (unchanged from the historical defaults).
WORKER_CORES: int = 8
WORKER_MEMORY_GB: int = 56
WORKER_MEMORY_LIMIT_GB: int = 60
#: Driver container footprint in cluster mode (32g heap + 4g overhead + 4g slack).
CLUSTER_DRIVER_FOOTPRINT_GB: int = 40
#: Fraction of host RAM the whole Spark deployment may claim.
DEFAULT_MEMORY_FRACTION: float = 0.75
#: Hard cap on the local-mode driver heap (very large heaps hurt GC).
LOCAL_DRIVER_MEMORY_CAP_GB: int = 192
#: Floor for the local-mode driver heap.
LOCAL_DRIVER_MEMORY_FLOOR_GB: int = 8


def _env_int(name: str) -> int | None:
    """Return a positive int from ``$name`` or ``None`` when unset/invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r", name, raw)
        return None
    return value if value > 0 else None


def _env_float(name: str, default: float) -> float:
    """Return a float in ``(0, 1]`` from ``$name`` or ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if 0.0 < value <= 1.0 else default


def host_cpu_count() -> int:
    """Return usable CPUs (respects ``$OVID2_HOST_CPUS`` and CPU affinity)."""
    override = _env_int("OVID2_HOST_CPUS")
    if override:
        return override
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def host_memory_gb() -> int:
    """Return host RAM in GiB (respects ``$OVID2_HOST_MEMORY_GB``).

    Reads ``MemTotal`` from ``/proc/meminfo``; falls back to
    ``sysconf`` and finally to a conservative 64 GiB.
    """
    override = _env_int("OVID2_HOST_MEMORY_GB")
    if override:
        return override
    meminfo = Path("/proc/meminfo")
    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemTotal:"):
                kib = int(line.split()[1])
                return max(1, kib // (1024 * 1024))
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return max(1, (pages * page_size) // (1024**3))
    except (AttributeError, OSError, ValueError):
        return 64


def max_cluster_workers(cpus: int | None = None, memory_gb: int | None = None) -> int:
    """Return the largest worker count that fits the host.

    ``min(cpus // WORKER_CORES, (budget - driver) // WORKER_MEMORY_LIMIT_GB)``
    where ``budget = memory_gb * OVID2_SPARK_MEMORY_FRACTION`` (default
    0.75). Never below 1.
    """
    cpus = cpus if cpus is not None else host_cpu_count()
    memory_gb = memory_gb if memory_gb is not None else host_memory_gb()
    fraction = _env_float("OVID2_SPARK_MEMORY_FRACTION", DEFAULT_MEMORY_FRACTION)
    budget = int(memory_gb * fraction) - CLUSTER_DRIVER_FOOTPRINT_GB
    by_cpu = cpus // WORKER_CORES
    by_mem = budget // WORKER_MEMORY_LIMIT_GB
    return max(1, min(by_cpu, by_mem))


def effective_worker_count(
    requested: int, *, cpus: int | None = None, memory_gb: int | None = None
) -> int:
    """Clamp a requested worker count to what the host can run.

    Args:
        requested: Worker count asked for (``--workers``; default 25).
        cpus: Host CPU override (tests).
        memory_gb: Host RAM override (tests).

    Returns:
        ``requested`` when ``OVID2_SPARK_ALLOW_OVERSUBSCRIBE=1`` or it
        already fits, else :func:`max_cluster_workers`.
    """
    requested = max(1, int(requested))
    if os.environ.get("OVID2_SPARK_ALLOW_OVERSUBSCRIBE", "").strip() in ("1", "true", "yes"):
        return requested
    cap = max_cluster_workers(cpus, memory_gb)
    if requested > cap:
        logger.info(
            "Capping Spark workers %d -> %d to fit host (%s CPUs, %s GiB); "
            "set OVID2_SPARK_ALLOW_OVERSUBSCRIBE=1 to override.",
            requested,
            cap,
            cpus if cpus is not None else host_cpu_count(),
            memory_gb if memory_gb is not None else host_memory_gb(),
        )
        return cap
    return requested


def local_cores() -> int:
    """Return the ``local[N]`` core count (``$OVID2_SPARK_CORES`` or all CPUs)."""
    return _env_int("OVID2_SPARK_CORES") or host_cpu_count()


def local_driver_memory_gb(memory_gb: int | None = None) -> int:
    """Return the local-mode driver heap in GiB.

    ``$OVID2_SPARK_LOCAL_DRIVER_MEMORY_GB`` wins; otherwise half of the
    host's Spark budget, clamped to
    ``[LOCAL_DRIVER_MEMORY_FLOOR_GB, LOCAL_DRIVER_MEMORY_CAP_GB]``.
    """
    override = _env_int("OVID2_SPARK_LOCAL_DRIVER_MEMORY_GB")
    if override:
        return override
    memory_gb = memory_gb if memory_gb is not None else host_memory_gb()
    fraction = _env_float("OVID2_SPARK_MEMORY_FRACTION", DEFAULT_MEMORY_FRACTION)
    candidate = int(memory_gb * fraction / 2)
    return max(LOCAL_DRIVER_MEMORY_FLOOR_GB, min(LOCAL_DRIVER_MEMORY_CAP_GB, candidate))


_MEMORY_UNITS_MB: dict[str, float] = {
    "k": 1 / 1024,
    "m": 1,
    "g": 1024,
    "t": 1024 * 1024,
}


def _parse_memory_mb(raw: str) -> int | None:
    """Parse a Spark memory string (``48g``, ``512m``, ``8192``) into MiB.

    A bare number is MiB (Spark's default unit for these keys). Returns
    ``None`` when the value cannot be parsed.
    """
    text = str(raw).strip().lower().removesuffix("b")
    if not text:
        return None
    unit = text[-1]
    number = text[:-1] if unit in _MEMORY_UNITS_MB else text
    factor = _MEMORY_UNITS_MB.get(unit, 1)
    try:
        return int(float(number) * factor)
    except ValueError:
        return None


def fit_executor_to_worker(
    conf: dict[str, str],
    *,
    worker_cores: int = WORKER_CORES,
    worker_memory_gb: int = WORKER_MEMORY_GB,
) -> tuple[dict[str, str], list[str]]:
    """Shrink the executor request so one executor fits on one worker.

    A Spark standalone master never places an executor bigger than a
    worker. If ``spark.executor.cores`` or the executor memory is larger
    than that, the app sits in ``WAITING`` ("requires more resource than
    any of Workers could have") and every stage shows ``(0 + 0)``. This
    happens when an engine config sets, say, ``spark.executor.cores=32``
    on a cluster whose workers have 8 cores each.

    Args:
        conf: Merged Spark config (not mutated).
        worker_cores: Cores each worker offers.
        worker_memory_gb: Spark memory each worker offers (GiB).

    Returns:
        ``(fitted_conf, messages)``. ``messages`` names each value that
        was lowered; it is empty when the request already fits.
    """
    fitted = dict(conf)
    messages: list[str] = []

    cores_raw = fitted.get("spark.executor.cores")
    if cores_raw is not None:
        try:
            cores = int(str(cores_raw).strip())
        except ValueError:
            cores = None
        if cores is not None and cores > worker_cores:
            fitted["spark.executor.cores"] = str(worker_cores)
            messages.append(
                f"spark.executor.cores {cores} -> {worker_cores} "
                f"(each worker has {worker_cores} cores)"
            )

    worker_mb = worker_memory_gb * 1024
    heap_mb = _parse_memory_mb(fitted.get("spark.executor.memory", "") or "")
    overhead_mb = _parse_memory_mb(fitted.get("spark.executor.memoryOverhead", "") or "") or 0
    if heap_mb is not None and heap_mb + overhead_mb > worker_mb:
        asked_overhead_mb = overhead_mb
        if overhead_mb >= worker_mb // 2:
            overhead_mb = max(384, worker_mb // 8)
            fitted["spark.executor.memoryOverhead"] = f"{overhead_mb}m"
        new_heap_mb = max(512, worker_mb - overhead_mb)
        fitted["spark.executor.memory"] = f"{new_heap_mb}m"
        messages.append(
            f"spark.executor.memory {heap_mb}m (+{asked_overhead_mb}m overhead) -> "
            f"{new_heap_mb}m (each worker has {worker_memory_gb} GiB)"
        )
    return fitted, messages


#: Fraction of host RAM one DuckDB driver container may claim.
DUCKDB_DEFAULT_MEMORY_FRACTION: float = 0.75
#: Share of the container cap handed to DuckDB's ``memory_limit``
#: (the rest is Python / Arrow / page-cache headroom).
DUCKDB_ENGINE_MEMORY_SHARE: float = 0.85


def duckdb_container_sizing(
    *,
    cpus: int | None = None,
    memory_gb: int | None = None,
) -> dict[str, int]:
    """Return the resource shape of a DuckDB driver container.

    DuckDB runs in-process, so one container is the whole run. Its size
    comes from the host, not from Spark's driver-heap cap: the container
    may use ``$OVID2_DUCKDB_MEMORY_FRACTION`` (default 0.75) of host RAM
    and every CPU (``$OVID2_DUCKDB_CPUS`` narrows it). Tables run one at
    a time, so the running table gets every thread and the whole memory
    limit; there is no per-job *Workers* knob for DuckDB.

    Args:
        cpus: Host CPU override (tests).
        memory_gb: Host RAM override (tests).

    Returns:
        ``{"memory_gb", "cpus", "threads", "engine_memory_gb"}``.
    """
    host_cpus = cpus if cpus is not None else host_cpu_count()
    host_mem = memory_gb if memory_gb is not None else host_memory_gb()
    use_cpus = min(host_cpus, _env_int("OVID2_DUCKDB_CPUS") or host_cpus)
    fraction = _env_float("OVID2_DUCKDB_MEMORY_FRACTION", DUCKDB_DEFAULT_MEMORY_FRACTION)
    container_gb = max(4, int(host_mem * fraction))
    engine_gb = max(1, int(container_gb * DUCKDB_ENGINE_MEMORY_SHARE))
    return {
        "memory_gb": container_gb,
        "cpus": use_cpus,
        "threads": use_cpus,
        "engine_memory_gb": engine_gb,
    }


__all__ = [
    "CLUSTER_DRIVER_FOOTPRINT_GB",
    "DUCKDB_DEFAULT_MEMORY_FRACTION",
    "WORKER_CORES",
    "WORKER_MEMORY_GB",
    "WORKER_MEMORY_LIMIT_GB",
    "duckdb_container_sizing",
    "effective_worker_count",
    "fit_executor_to_worker",
    "host_cpu_count",
    "host_memory_gb",
    "local_cores",
    "local_driver_memory_gb",
    "max_cluster_workers",
]
