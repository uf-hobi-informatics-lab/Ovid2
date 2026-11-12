"""Unit tests — user-defined transforms (pipeline-builder A4).

Covers the expression allow-list, ``derive_column``, per-step parameter
validation, the user library search path, the assignments file, and the
``transform`` pipeline stage running on DuckDB (engine-portable path).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ovid2.config.sql_expr_guard import (
    SqlExprRejected,
    check_expression,
    ensure_safe_expression,
    for_engine_family,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- guard


@pytest.mark.parametrize(
    "expr",
    [
        "reflect('java.util.UUID', 'randomUUID')",
        "java_method('java.lang.Runtime', 'getRuntime')",
        "(select 1)",
        "x IN (select a from t)",
        "my_udf(x)",
        "a; drop table t",
        "sum(x)",
        "row_number() over (order by x)",
        "rand()",
        "input_file_name()",
        "transform(arr, x -> x + 1)",
    ],
)
def test_guard_rejects_unsafe(expr: str) -> None:
    assert not check_expression(expr).ok
    with pytest.raises(SqlExprRejected):
        ensure_safe_expression(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "upper(trim(SEX))",
        "coalesce(nullif(trim(SEX), ''), 'UN')",
        "CASE WHEN AGE > 0 THEN 'a' ELSE NULL END",
        "AGE >= 0 AND AGE <= 120",
        "to_date(BIRTH_DATE, 'yyyy-MM-dd')",
        "date_format(ADMIT_DATE, 'yyyyMMdd')",
        "cast(AGE as int) + 1",
        "lpad(ZIP, 5, '0')",
        "substring(ZIP, 1, 5)",
        "x RLIKE '^[0-9]+$'",
    ],
)
def test_guard_accepts_row_expressions(expr: str) -> None:
    result = check_expression(expr)
    assert result.ok, result.errors


def test_guard_reports_columns() -> None:
    assert check_expression("concat(A, '-', B)").columns == ("A", "B")


def test_snowpark_dialect_transpile() -> None:
    assert for_engine_family("nvl(A, 'x')", "spark") == "nvl(A, 'x')"
    assert "COALESCE" in for_engine_family("nvl(A, 'x')", "snowpark").upper()


# ---------------------------------------------------------------- params


def test_step_params_validated_against_schema() -> None:
    from ovid2.transforms.engine import validate_step_params

    assert validate_step_params("derive_column", {"column": "X", "expr": "1"}) == []
    errs = validate_step_params("derive_column", {"column": "X"})
    assert errs and "expr" in errs[0]
    assert validate_step_params("to_upper", {"columns": "SEX"})  # not an array
    assert validate_step_params("nope", {}) == ["unknown transform op 'nope'"]


# ---------------------------------------------------------------- DuckDB


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


def _run(engine: Any, df: Any, steps: list[dict[str, Any]]) -> Any:
    from ovid2.transforms import load_from_dict, run

    spec = load_from_dict({"v": 1, "steps": steps}, name="t")
    return run(
        df,
        spec,
        session=engine.get_session(),
        functions=engine.get_functions(),
        engine_family="duckdb",
    )


def test_derive_column_on_duckdb(duck: Any) -> None:
    df = duck.create_dataframe([(" m ",), ("",), (None,)], "SEX string")
    out = _run(
        duck,
        df,
        [{"op": "derive_column", "column": "SEX", "expr": "coalesce(nullif(trim(SEX), ''), 'UN')"}],
    )
    assert sorted(r["SEX"] for r in out.collect()) == ["UN", "UN", "m"]


def test_derive_column_rejects_reflect_before_running(duck: Any) -> None:
    from ovid2.transforms import TransformSpecValidationError

    df = duck.create_dataframe([("a",)], "X string")
    with pytest.raises(SqlExprRejected):
        _run(duck, df, [{"op": "derive_column", "column": "Y", "expr": "reflect('a','b')"}])
    with pytest.raises(TransformSpecValidationError, match="step 1"):
        _run(duck, df, [{"op": "derive_column", "column": "Y"}])


def test_filter_expr_guarded(duck: Any) -> None:
    df = duck.create_dataframe([(5,), (200,)], "AGE int")
    out = _run(duck, df, [{"op": "filter_expr", "expr": "AGE >= 0 AND AGE <= 120"}])
    assert [r["AGE"] for r in out.collect()] == [5]
    with pytest.raises(SqlExprRejected):
        _run(duck, df, [{"op": "filter_expr", "expr": "AGE IN (select 1)"}])


# ---------------------------------------------------------------- library


def test_user_library_is_searched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.transforms.spec import find_spec, load_spec, user_library_dir

    monkeypatch.setenv("OVID2_TRANSFORMS_DIR", str(tmp_path))
    assert user_library_dir() == tmp_path
    (tmp_path / "my_udf.yaml").write_text(
        "v: 1\nsteps:\n  - op: derive_column\n    column: X\n    expr: upper(X)\n",
        encoding="utf-8",
    )
    assert find_spec("my_udf") == tmp_path / "my_udf.yaml"
    assert load_spec("my_udf").steps[0].op == "derive_column"


# ---------------------------------------------------------------- assignments


def test_assignments_parse_and_order() -> None:
    from ovid2.transforms.assignments import (
        AssignmentsError,
        parse_assignments,
        table_assignments,
    )

    parsed = parse_assignments(
        {
            "v": 1,
            "tables": {
                "*": [{"spec": "clean_whitespace_and_nulls"}],
                "demographic": [
                    {"steps": [{"op": "to_upper", "columns": ["SEX"]}]},
                    {"spec": "off", "enabled": False},
                ],
            },
        }
    )
    chain = table_assignments(parsed, "demographic")
    assert [a.label for a in chain] == ["clean_whitespace_and_nulls", "inline (1 steps)"]
    assert [a.label for a in table_assignments(parsed, "encounter")] == [
        "clean_whitespace_and_nulls"
    ]
    with pytest.raises(AssignmentsError):
        parse_assignments({"v": 1, "tables": {"x": [{"spec": "a", "steps": []}]}})


def test_transform_stage_runs_assignments_on_duckdb(
    duck: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ovid2.transformations import transform as stage_mod
    from ovid2.transforms import assignments as amod

    parsed = amod.parse_assignments(
        {
            "v": 1,
            "tables": {
                "demographic": [
                    {
                        "steps": [
                            {"op": "derive_column", "column": "SEX", "expr": "upper(trim(SEX))"}
                        ]
                    }
                ]
            },
        }
    )
    monkeypatch.setattr(amod, "load_assignments", lambda pipeline: parsed)
    df = duck.create_dataframe([(" f ",)], "SEX string")
    out = stage_mod.TransformStageTransformer().transform(
        df,
        {"table_name": "demographic", "pipeline": "demo", "engine": duck, "spark": None},
    )
    assert [r["SEX"] for r in out.collect()] == ["F"]
    # Unassigned table → untouched (same object, no-op).
    assert (
        stage_mod.TransformStageTransformer().transform(
            df, {"table_name": "encounter", "pipeline": "demo", "engine": duck}
        )
        is df
    )


def test_apply_transforms_bridge_uses_active_engine(duck: Any) -> None:
    """The mapper bridge no longer hard-codes Spark (runs on DuckDB)."""
    from ovid2.cdm.registry import get_cdm
    from ovid2.mappers.base import MapOpContext
    from ovid2.mappers.ops.apply_transforms import ApplyTransformsOp

    df = duck.create_dataframe([(" x ",)], "A string")
    ctx = MapOpContext(
        spark=duck.get_session(),  # type: ignore[arg-type]
        pipeline="demo",
        table="demographic",
        cdm=get_cdm("pcornet_7_0"),
    )
    out = ApplyTransformsOp().apply(
        df, {"steps": [{"op": "derive_column", "column": "A", "expr": "upper(trim(A))"}]}, ctx
    )
    assert [r["A"] for r in out.collect()] == ["X"]


def test_derive_column_keeps_existing_column_case(duck: Any) -> None:
    df = duck.create_dataframe([("f",)], "SEX string")
    out = _run(duck, df, [{"op": "derive_column", "column": "sex", "expr": "upper(SEX)"}])
    assert out.columns == ["SEX"]
    assert [r["SEX"] for r in out.collect()] == ["F"]
