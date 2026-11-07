"""DuckDB engine: table workers (parallel tables on private cursors)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine

pytestmark = pytest.mark.unit


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(
        args={"threads": 4, "memory_limit": "1GB", "temp_directory": str(tmp_path / "duck")}
    )
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def _in_worker(engine: DuckDBEngine, fn: Any) -> Any:
    """Run ``fn()`` on a fresh thread inside ``engine.table_worker()``."""
    out: dict[str, Any] = {}

    def _target() -> None:
        try:
            with dsl.use_engine(engine), engine.table_worker():
                out["value"] = fn()
        except BaseException as exc:  # surfaced to the test thread
            out["error"] = exc

    t = threading.Thread(target=_target)
    t.start()
    t.join(30)
    if "error" in out:
        raise out["error"]
    return out["value"]


def test_table_concurrency_defaults_and_overrides(
    engine: DuckDBEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OVID2_HOST_CPUS", "8")
    monkeypatch.delenv("OVID2_DUCKDB_TABLE_CONCURRENCY", raising=False)
    assert engine.table_concurrency(20) == 8
    assert engine.table_concurrency(3) == 3
    monkeypatch.setenv("OVID2_DUCKDB_TABLE_CONCURRENCY", "2")
    assert engine.table_concurrency(20) == 2
    engine.args["table_concurrency"] = "1"
    assert engine.table_concurrency(20) == 1


def test_worker_uses_private_cursor(engine: DuckDBEngine) -> None:
    root = engine.connection()
    worker_conn = _in_worker(engine, engine.connection)
    assert worker_conn is not root
    assert engine.connection() is root


def test_reference_view_rebinds_onto_worker_cursor(engine: DuckDBEngine, tmp_path: Path) -> None:
    """A reference table built on the root connection joins inside a worker."""
    src = tmp_path / "demographic.csv"
    src.write_text("patid,sex\n1,F\n2,M\n")
    demo = engine.read_csv(str(src))
    demo = engine.materialise(demo, "ref_demographic")
    engine.register_view("demographic", demo)

    def _work() -> list[tuple[Any, ...]]:
        fact = engine.create_dataframe([("1", "x"), ("3", "y")], "patid string, v string")
        ref = engine.view("demographic")
        joined = fact.join(ref, fact["patid"] == ref["patid"], "inner").select(
            fact["v"], ref["sex"]
        )
        return [tuple(r) for r in joined.collect()]

    assert _in_worker(engine, _work) == [("x", "F")]


def test_lazy_file_view_rebinds_by_sql(engine: DuckDBEngine, tmp_path: Path) -> None:
    src = tmp_path / "harvest.csv"
    src.write_text("refresh_date\n2026-01-01\n")
    engine.register_view("harvest", engine.read_csv(str(src)))
    rows = _in_worker(engine, lambda: [tuple(r) for r in engine.view("harvest").collect()])
    assert rows == [("2026-01-01",)]


def test_arrow_backed_view_is_exported_once(engine: DuckDBEngine) -> None:
    """``createDataFrame`` frames cannot be re-bound by SQL; they are exported."""
    engine.register_view("provider", engine.create_dataframe([("p1",)], "providerid string"))
    rows = [
        _in_worker(engine, lambda: [tuple(r) for r in engine.view("provider").collect()])
        for _ in range(3)
    ]
    assert rows == [[("p1",)]] * 3
    assert len(engine._rebind_targets) == 1


def test_worker_views_are_private(engine: DuckDBEngine) -> None:
    """Formatter source views registered by two parallel tables don't collide."""
    engine.register_view("demographic", engine.create_dataframe([("d",)], "x string"))

    def _work(tag: str) -> Any:
        def _inner() -> Any:
            engine.create_dataframe([(tag,)], "x string").createOrReplaceTempView("src")
            return [tuple(r) for r in engine.view("src").collect()]

        return _inner

    assert _in_worker(engine, _work("a")) == [("a",)]
    assert _in_worker(engine, _work("b")) == [("b",)]
    assert "src" not in engine.view_names()


def test_lookup_table_registered_per_connection(engine: DuckDBEngine) -> None:
    fn = engine.get_functions()
    mapping = {str(i): f"v{i}" for i in range(400)}

    def _work() -> list[tuple[Any, ...]]:
        df = engine.create_dataframe([("7",)], "k string")
        out = df.select(fn.create_map(*[fn.lit(x) for kv in mapping.items() for x in kv])[df["k"]])
        return [tuple(r) for r in out.collect()]

    root_rows = _work()
    assert root_rows == [("v7",)]
    assert _in_worker(engine, _work) == [("v7",)]


def test_parallel_checkpoints_of_same_plan_build_once(engine: DuckDBEngine, tmp_path: Path) -> None:
    src = tmp_path / "common.csv"
    src.write_text("v\n1\n2\n")
    built: list[str] = []
    real = engine.materialise

    def _counting(frame: Any, name: str | None = None) -> Any:
        built.append(name or "")
        return real(frame, name)

    engine.materialise = _counting  # type: ignore[method-assign]
    barrier = threading.Barrier(4, timeout=10)

    def _work() -> list[tuple[Any, ...]]:
        barrier.wait()
        df = engine.read_csv(str(src))
        ckpt = engine.checkpoint(df, "common")
        return sorted(tuple(r) for r in ckpt.collect())

    results: list[Any] = []
    lock = threading.Lock()

    def _thread() -> None:
        value = _in_worker(engine, _work)
        with lock:
            results.append(value)

    threads = [threading.Thread(target=_thread) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [[("1",), ("2",)]] * 4
    assert built == ["common"]


def test_parallel_writes_count_rows_and_track_stages(engine: DuckDBEngine, tmp_path: Path) -> None:
    from ovid2.core.engines.duckdb_stager import write_frame_local

    def _write(i: int) -> None:
        def _inner() -> None:
            rel = engine.connection().sql(
                f"SELECT range::VARCHAR AS x FROM range({1000 * (i + 1)})"
            )
            write_frame_local(
                engine,
                engine.wrap(rel),
                str(tmp_path / "out"),
                f"t{i}",
                fmt="csv",
                delimiter="|",
                single_file=True,
            )

        _in_worker(engine, _inner)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert engine.rows_written_total == 1000 + 2000 + 3000 + 4000
    for i in range(4):
        lines = (tmp_path / "out" / f"t{i}.csv").read_text().splitlines()
        assert lines[0] == "x" and len(lines) == 1000 * (i + 1) + 1
    assert engine.current_stage is None


def test_current_stage_joins_concurrent_stages(engine: DuckDBEngine) -> None:
    ready = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        engine.current_stage = "write lab_result_cm"
        ready.set()
        release.wait(5)
        engine.current_stage = None

    t = threading.Thread(target=_hold)
    t.start()
    ready.wait(5)
    engine.current_stage = "checkpoint obs_clin"
    assert set(str(engine.current_stage).split("; ")) == {
        "write lab_result_cm",
        "checkpoint obs_clin",
    }
    release.set()
    t.join()
    engine.current_stage = None
    assert engine.current_stage is None


def test_release_checkpoints_is_per_thread(engine: DuckDBEngine) -> None:
    engine._checkpoints().append(engine.create_dataframe([("a",)], "x string"))
    assert _in_worker(engine, engine.release_checkpoints) == 0
    assert engine.release_checkpoints() == 1
