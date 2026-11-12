"""Analysis transformers persist Report Studio aggregates (run on DuckDB)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def duck() -> Iterator[Any]:
    pytest.importorskip("duckdb")
    from ovid2.core.engines import dsl
    from ovid2.core.engines.duckdb import DuckDBEngine

    eng = DuckDBEngine(args={"threads": 1, "memory_limit": "512MB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


def test_monthly_and_distinct_records_build_an_analysis_source(
    duck: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ovid2.reports.artifacts import analysis_artifact_path, read_analysis_records
    from ovid2.reports.sources import ADAPTERS, validate_source
    from ovid2.transformations.analysis.distinct_values import RawDistinctValuesTransformer
    from ovid2.transformations.analysis.monthly_counts import MonthlyCountsTransformer

    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path))
    iid = "20260101-000000-abc123"
    df = duck.create_dataframe(
        [
            ("1", "2024-01-03", "AV"),
            ("2", "2024-01-20", "AV"),
            ("3", "2024-02-11", "IP"),
            ("4", None, None),
        ],
        "PATID string, ADMIT_DATE string, ENC_TYPE string",
    )
    ctx = {"table_name": "ENCOUNTER", "instance_id": iid, "pipeline": "demo"}
    assert MonthlyCountsTransformer().transform(df, ctx) is df
    assert RawDistinctValuesTransformer().transform(df, ctx) is df

    recs = read_analysis_records(analysis_artifact_path(iid, tmp_path))
    monthly = next(r for r in recs if r["type"] == "monthly")
    assert monthly["column"] == "ADMIT_DATE"
    assert {r["month"]: r["records"] for r in monthly["rows"]} == {"2024-01": 2, "2024-02": 1}
    distinct = next(r for r in recs if r["type"] == "distinct" and r["column"] == "ENC_TYPE")
    assert (
        distinct["stage"] == "raw" and distinct["total_rows"] == 4 and distinct["n_distinct"] == 3
    )
    assert distinct["null_rows"] == 1
    assert {t["value"]: t["records"] for t in distinct["top"]} == {"AV": 2, "IP": 1, None: 1}
    assert not any(r["column"] == "PATID" for r in recs), "ID columns are never profiled"

    doc = ADAPTERS["analysis"].build(recs, instance_id=iid, pipeline="demo")
    validate_source(doc)
    assert doc["datasets"]["monthly"]["rows"]


def test_append_is_best_effort_without_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ovid2.reports.artifacts import append_analysis_record

    monkeypatch.setenv("OVID2_WORK_DIR", str(tmp_path))
    append_analysis_record("", {"type": "monthly"})
    assert not list(tmp_path.iterdir())
