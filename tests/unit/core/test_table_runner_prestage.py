"""``run_tables(prestage=...)`` stages every tail source once, before the tail."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from ovid2.core.table_runner import prestage_sources, run_tables

pytestmark = pytest.mark.unit


class _Engine:
    """Parallel-capable engine double (``table_worker`` + ``table_concurrency``)."""

    def table_concurrency(self, n_tables: int) -> int:
        return 4

    @contextlib.contextmanager
    def table_worker(self) -> Iterator[None]:
        yield


class _Formatter:
    def __init__(self, globs: list[str]) -> None:
        self.source_globs = globs


def test_prestage_runs_after_reference_and_before_tail() -> None:
    events: list[str] = []
    lock = threading.Lock()
    staged: list[str] = []

    def _stage(glob: str) -> None:
        with lock:
            staged.append(glob)
            events.append(f"stage:{glob}")

    formatters = {
        "vital": _Formatter(["observation.csv", "measurement.csv", "observation_sup.csv"]),
        "lab_result_cm": _Formatter(["*measurement.csv", "*measurement_sup.csv"]),
        "obs_gen": _Formatter(["observation.csv"]),
    }
    sizes = {"measurement.csv": 60, "observation.csv": 52, "observation_sup.csv": 45}
    hook = prestage_sources(
        _Engine(), formatters, _stage, sizes=lambda globs: {g: sizes.get(g, 0) for g in globs}
    )

    def _run(table: str) -> None:
        with lock:
            events.append(f"run:{table}")

    run_tables(
        ["demographic", "vital", "lab_result_cm", "obs_gen"],
        _run,
        engine=_Engine(),
        reference_tables=["demographic"],
        prestage=hook,
    )
    # every distinct glob staged exactly once
    assert sorted(staged) == sorted(
        {
            "observation.csv",
            "measurement.csv",
            "observation_sup.csv",
            "*measurement.csv",
            "*measurement_sup.csv",
        }
    )
    first_stage = min(i for i, e in enumerate(events) if e.startswith("stage:"))
    last_stage = max(i for i, e in enumerate(events) if e.startswith("stage:"))
    assert events.index("run:demographic") < first_stage
    tail_runs = [i for i, e in enumerate(events) if e.startswith("run:") and e != "run:demographic"]
    assert min(tail_runs) > last_stage


def test_prestage_failure_does_not_fail_the_run() -> None:
    def _hook(tables: list[str], concurrency: int) -> None:
        raise RuntimeError("boom")

    ran: list[str] = []
    result = run_tables(["a", "b"], ran.append, engine=_Engine(), prestage=_hook)
    assert sorted(ran) == ["a", "b"] and not result.failed


def test_prestage_skips_failed_glob() -> None:
    def _stage(glob: str) -> None:
        if glob == "missing.csv":
            raise FileNotFoundError(glob)

    hook = prestage_sources(_Engine(), {"t": _Formatter(["missing.csv", "ok.csv"])}, _stage)
    hook(["t"], 2)  # must not raise


def test_driver_prestage_disabled_without_staging_engine() -> None:
    from ovid2.core.table_runner import driver_prestage

    assert driver_prestage(object(), "addr", {}, pipeline="p", batch_id="b") is None
    assert driver_prestage(_Engine(), None, {}, pipeline="p", batch_id="b") is None


def test_formatter_source_globs_default_and_spec() -> None:
    from ovid2.formatters.adapters import AdapterContext
    from ovid2.formatters.engine import _SpecFormatter
    from ovid2.formatters.spec import FormatterSpec, SourceSpec

    spec = FormatterSpec(
        v=1,
        pipeline="demo",
        formatter="obs_gen",
        adapter="semantic_join",
        input_file_name="*observation.csv",
        sources={
            "observation": SourceSpec(name="observation", glob="*observation.csv"),
            "observation_sup": SourceSpec(name="observation_sup", glob="*observation_sup.csv"),
        },
    )
    fmt: Any = _SpecFormatter(spec, AdapterContext.__new__(AdapterContext))
    assert fmt.source_globs == ["*observation.csv", "*observation_sup.csv"]


def test_driver_prestage_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.core.table_runner import driver_prestage

    class _Staging(_Engine):
        def stage_sources_enabled(self) -> bool:
            return True

    monkeypatch.delenv("OVID2_DUCKDB_PRESTAGE", raising=False)
    assert driver_prestage(_Staging(), "addr", {}, pipeline="p", batch_id="b") is None
