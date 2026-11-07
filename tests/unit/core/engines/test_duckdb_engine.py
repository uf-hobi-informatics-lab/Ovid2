"""DuckDB engine: DataFrame / Column / functions semantics (Spark parity).

Runs in-process (no JVM). Every expectation below is the value PySpark
produces for the same expression with ``spark.sql.ansi.enabled=false``
and ``timeParserPolicy=LEGACY`` (the production settings); the
Spark-side parity is exercised end-to-end by
``tests/integration/test_engine_dsl_parity.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("duckdb")

from ovid2.core.engines import dsl
from ovid2.core.engines.duckdb import DuckDBEngine

pytestmark = pytest.mark.unit


@pytest.fixture()
def engine() -> Iterator[DuckDBEngine]:
    eng = DuckDBEngine(args={"threads": 2, "memory_limit": "1GB"})
    dsl.set_active_engine(eng)
    yield eng
    eng.stop_session()
    dsl.set_active_engine(None)


@pytest.fixture()
def fn(engine: DuckDBEngine) -> Any:
    return engine.get_functions()


def _df(engine: DuckDBEngine, rows: list[tuple[Any, ...]], schema: str) -> Any:
    return engine.create_dataframe(rows, schema)


def _vals(df: Any) -> list[tuple[Any, ...]]:
    return [tuple(r) for r in df.collect()]


# ---------------------------------------------------------------- engine


def test_registration_and_family(engine: DuckDBEngine) -> None:
    from ovid2.config.engines import engine_family, list_engine_types
    from ovid2.core.engines.registry import load_engine_class
    from ovid2.core.engines.stager import stager_class_for_engine_type

    assert "duckdb" in list_engine_types()
    assert engine_family("duckdb") == "duckdb"
    assert load_engine_class("duckdb") is DuckDBEngine
    assert stager_class_for_engine_type("duckdb").__name__ == "DuckDBEngineStager"
    assert engine.session_is_spark is False
    assert engine.test().ok


def test_lazy_until_action(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1",)], "a string")
    # Building a plan over a missing column only fails at bind time on
    # projection, which DuckDB does eagerly; a *filter* on a valid
    # column stays lazy (no execution until collect).
    out = df.filter(fn.col("a") == "1").withColumn("b", fn.upper(fn.col("a")))
    assert out.columns == ["a", "b"]
    assert _vals(out) == [("1", "1")]


def test_metrics_and_cancel_never_raise(engine: DuckDBEngine) -> None:
    sample = engine.get_metrics()
    assert sample.engine_family == "duckdb"
    engine.cancel_running_queries()


# ---------------------------------------------------------------- column ops


def test_string_vs_number_comparison_is_lenient(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("5",), ("abc",), (None,), ("-1.5",)], "v string")
    # Spark (ANSI off): string > int casts to number; garbage -> NULL.
    assert _vals(df.select((fn.col("v") > 0).alias("g"))) == [(True,), (None,), (None,), (False,)]
    assert _vals(df.select((fn.col("v") + 1).alias("p"))) == [(6.0,), (None,), (None,), (-0.5,)]


def test_when_otherwise_and_isin(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("A",), ("B",), (None,)], "v string")
    expr = fn.when(fn.col("v") == "A", "x").when(fn.col("v").isin("B", "C"), "y").otherwise("z")
    assert _vals(df.select(expr.alias("o"))) == [("x",), ("y",), ("z",)]
    assert _vals(df.filter(~fn.col("v").isin(["A"]))) == [("B",)]


def test_null_literal_is_varchar(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1",)], "v string").select(fn.lit(None).alias("n"), fn.col("v"))
    assert dict(df.dtypes)["n"] == "VARCHAR"


def test_integral_cast_truncates_like_spark(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1.9",), ("-1.9",), (" 12 ",), ("x",)], "v string")
    assert _vals(df.select(fn.col("v").cast("int").alias("i"))) == [(1,), (-1,), (12,), (None,)]


def test_substring_pos_zero_and_negative(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("abcdef",)], "v string")
    out = df.select(
        fn.substring(fn.col("v"), 0, 2).alias("a"), fn.substring(fn.col("v"), -2, 5).alias("b")
    )
    assert _vals(out) == [("ab", "ef")]


def test_regexp_replace_java_backrefs(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("abc",)], "v string")
    assert _vals(df.select(fn.regexp_replace(fn.col("v"), "(b)", "<$1>").alias("r"))) == [
        ("a<b>c",)
    ]
    assert _vals(df.select(fn.regexp_extract(fn.col("v"), "(z)", 1).alias("r"))) == [("",)]


def test_dates(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("2023-4-5",), ("04/15/2023",), ("bad",), (None,)], "d string")
    out = df.select(
        fn.date_format(fn.to_date(fn.col("d"), "yyyy-MM-dd"), "yyyy-MM-dd").alias("iso"),
        fn.date_format(fn.to_date(fn.col("d"), "MM/dd/yyyy"), "M/d/yyyy").alias("us"),
    )
    assert _vals(out) == [("2023-04-05", None), (None, "4/15/2023"), (None, None), (None, None)]
    diff = _df(engine, [("2023-04-15", "2023-04-01 23:00:00")], "a string, b string")
    assert _vals(diff.select(fn.datediff(fn.col("a"), fn.col("b")).alias("d"))) == [(14,)]


def test_stable_hash_is_non_negative_string(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("a", None), ("a", None)], "x string, y string")
    (h1,), (h2,) = _vals(df.select(fn.stable_hash(fn.col("x"), fn.col("y")).alias("h")))
    assert h1 == h2 and h1.isdigit() and int(h1) < 2**63


def test_map_from_dict_mixed_types(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("M",), ("fn",), ("X",)], "s string")
    expr = fn.map_from_dict({"M": "8507", "fn": 8532}, fn.col("s"), default="0")
    assert _vals(df.select(expr.alias("c"))) == [("8507",), ("8532",), ("0",)]


@pytest.mark.parametrize("size", [2, 500])
def test_create_map_lookup_small_and_table_backed(engine: DuckDBEngine, fn: Any, size: int) -> None:
    """``create_map(...)[key]``: inline CASE (small) and lookup table (large)."""
    mapping = {f"k{i}": f"v{i}" for i in range(size)}
    pairs = [x for kv in mapping.items() for x in (fn.lit(kv[0]), fn.lit(kv[1]))]
    df = _df(engine, [("k0",), ("k1",), ("zz",), (None,)], "s string")
    # Flat varargs and the single-list form (utils.geocoding) both work.
    for m in (fn.create_map(*pairs), fn.create_map(pairs)):
        out = _vals(df.select(m[fn.col("s")].alias("o")))
        assert out == [("v0",), ("v1",), (None,), (None,)]
    # A coalesce fallback (translate_by_dict pass-through) composes.
    out = _vals(df.select(fn.coalesce(fn.create_map(*pairs)[fn.col("s")], fn.col("s")).alias("o")))
    assert out == [("v0",), ("v1",), ("zz",), (None,)]


def test_create_map_lookup_table_is_shared(engine: DuckDBEngine, fn: Any) -> None:
    pairs = [(f"k{i}", f"v{i}") for i in range(200)]
    first = engine.register_lookup_table(pairs, stringify=False)
    assert engine.register_lookup_table(pairs, stringify=False) == first
    assert engine.register_lookup_table(pairs[:100], stringify=False) != first


def test_create_map_rejects_non_literals(engine: DuckDBEngine, fn: Any) -> None:
    from ovid2.core.engines.protocols import EngineOperationUnsupported

    with pytest.raises(EngineOperationUnsupported):
        fn.create_map(fn.col("a"), fn.lit("x"))
    with pytest.raises(ValueError):
        fn.create_map(fn.lit("a"))


def test_monotonically_increasing_id_unique(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [(str(i),) for i in range(50)], "s string")
    ids = [r[0] for r in _vals(df.select(fn.monotonically_increasing_id().alias("i")))]
    assert len(set(ids)) == 50 and min(ids) >= 0


def test_year_parses_strings_like_spark(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("2023-04-15",), ("1900-01-01",), ("bad",), (None,)], "s string")
    assert _vals(df.select(fn.year(fn.col("s")).alias("y"))) == [
        (2023,),
        (1900,),
        (None,),
        (None,),
    ]


def test_dsl_window_dispatches_to_duckdb(engine: DuckDBEngine) -> None:
    df = _df(engine, [("a", "1"), ("a", "2"), ("b", "3")], "g string, v string")
    w = dsl.Window.partitionBy("g").orderBy(dsl.col("v").desc())
    out = df.withColumn("rn", dsl.row_number().over(w)).filter(dsl.col("rn") == 1)
    assert sorted(_vals(out.select("g", "v"))) == [("a", "2"), ("b", "3")]


def test_initcap_and_concat_ws(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("hELLO wORLD", None)], "a string, b string")
    assert _vals(df.select(fn.initcap(fn.col("a")).alias("i"))) == [("Hello World",)]
    assert _vals(df.select(fn.concat_ws("|", fn.col("a"), fn.col("b")).alias("c"))) == [
        ("hELLO wORLD",)
    ]
    assert _vals(df.select(fn.concat(fn.col("a"), fn.col("b")).alias("c"))) == [(None,)]


def test_split_explode(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1", "a, b"), ("2", None)], "id string, v string")
    out = df.withColumn(
        "v",
        fn.explode(
            fn.when(fn.col("v").isNotNull(), fn.split(fn.col("v"), ",")).otherwise(
                fn.array(fn.col("v"))
            )
        ),
    ).withColumn("v", fn.trim(fn.col("v")))
    assert _vals(out) == [("1", "a"), ("1", "b"), ("2", None)]


def test_expr_and_select_expr_transpile_spark_sql(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("2023-4-5", "x")], "d string, v string")
    out = df.selectExpr(
        "coalesce(date_format(to_date(d, 'yyyy-MM-dd'), 'yyyy'), '') as y",
        "CASE WHEN v RLIKE '^x' THEN 'hit' END as r",
        "''",
    )
    assert _vals(out)[0][:2] == ("2023", "hit")
    assert _vals(df.select(fn.expr("upper(v) AS u"))) == [("X",)]


# ---------------------------------------------------------------- DataFrame ops


def test_join_qualifiers_survive_filters_and_aggregates(engine: DuckDBEngine, fn: Any) -> None:
    rel = _df(
        engine,
        [
            ("1", "2", "2020", "Maps to"),
            ("1", "3", "2021", "Maps to"),
            ("4", "5", "2020", "Maps to"),
        ],
        "concept_id_1 string, concept_id_2 string, valid_start_date string, relationship_id string",
    )
    rel.createOrReplaceTempView("concept_relationship")
    rel = (
        engine.get_session()
        .table("concept_relationship")
        .filter(fn.col("relationship_id") == "Maps to")
    )
    latest = (
        rel.groupBy("concept_id_1")
        .agg(fn.max("valid_start_date").alias("m"))
        .select(fn.col("concept_id_1").alias("g"), fn.col("m"))
    )
    out = rel.join(
        latest,
        (rel["concept_id_1"] == latest["g"]) & (rel["valid_start_date"] == latest["m"]),
        "inner",
    ).select(rel["concept_id_1"].alias("a"), rel["concept_id_2"].alias("b"))
    assert sorted(_vals(out)) == [("1", "3"), ("4", "5")]


def test_self_join_and_using_join(engine: DuckDBEngine, fn: Any) -> None:
    a = _df(engine, [("1", "x"), ("2", "y")], "id string, v string")
    b = _df(engine, [("1", "p")], "id string, w string")
    assert _vals(a.join(b, "id", "left").orderBy("id")) == [("1", "x", "p"), ("2", "y", None)]
    assert _vals(a.join(b, ["id"], "outer").orderBy("id")) == [("1", "x", "p"), ("2", "y", None)]
    assert _vals(a.join(a.filter(fn.col("v") == "x"), "id")) == [("1", "x", "x")]


def test_semi_anti_and_sql_string_join(engine: DuckDBEngine, fn: Any) -> None:
    a = _df(engine, [("1",), ("2",)], "id string").alias("a")
    b = _df(engine, [("1",)], "k string").alias("b")
    assert _vals(a.join(b, fn.col("id") == fn.col("k"), "left_semi")) == [("1",)]
    assert _vals(a.join(b, fn.col("id") == fn.col("k"), "left_anti")) == [("2",)]
    assert _vals(a.join(b, "a.id = b.k", "inner").select("a.id")) == [("1",)]


def test_union_by_name_widens_and_fills(engine: DuckDBEngine) -> None:
    a = _df(engine, [("1", 2)], "x string, n int")
    b = _df(engine, [("z", "3")], "y string, n string")
    out = engine.union_by_name(a, b, allow_missing_columns=True)
    assert out.columns == ["x", "n", "y"]
    assert sorted(_vals(out), key=str) == sorted([("1", "2", None), (None, "3", "z")], key=str)
    with pytest.raises(ValueError):
        a.unionByName(b)


def test_drop_duplicates_distinct_and_order(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1", "a"), ("1", "b"), (None, "c")], "k string, v string")
    assert len(_vals(engine.drop_duplicates(df, ["k"]))) == 2
    assert _vals(df.select("k").distinct().orderBy("k")) == [(None,), ("1",)]
    assert _vals(df.orderBy(fn.col("k").desc()).select("k")) == [("1",), ("1",), (None,)]


def test_with_column_replaces_in_place_case_insensitive(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1", "a")], "ID string, v string")
    out = df.withColumn("id", fn.lit("9"))
    assert out.columns == ["id", "v"] and _vals(out) == [("9", "a")]
    assert df.withColumnRenamed("v", "w").columns == ["ID", "w"]
    assert df.toDF("p", "q").columns == ["p", "q"]
    assert df.drop("V").columns == ["ID"]


def test_temp_view_is_a_plan_snapshot(engine: DuckDBEngine, fn: Any) -> None:
    session = engine.get_session()
    _df(engine, [("1",)], "v string").createOrReplaceTempView("t")
    # Re-registering ``t`` from a plan that reads ``t`` must not recurse.
    session.table("t").withColumn("v", fn.lit("2")).createOrReplaceTempView("t")
    assert _vals(session.table("t")) == [("2",)]
    assert session.catalog.tableExists("T")


def test_checkpoint_is_reused_and_released(engine: DuckDBEngine, fn: Any) -> None:
    df = _df(engine, [("1",)], "v string").withColumn("w", fn.lit("x"))
    c1 = engine.checkpoint(df, "common_a")
    c2 = engine.checkpoint(df, "common_b")
    assert c1 is c2  # identical plan -> one materialisation
    assert _vals(c1) == [("1", "x")]


def test_checkpoint_reused_for_independently_built_identical_plans(
    engine: DuckDBEngine, fn: Any, tmp_path: Path
) -> None:
    """Regression: the PCORnet->OMOP ``common`` fan-in is *rebuilt* per domain.

    Each DuckFrame takes a fresh ``__ov<N>`` alias, so the SQL of two
    identical plans differed and the cache never hit (measurement /
    observation / procedure_occurrence each re-materialised ``common``).
    """
    src = tmp_path / "src.csv"
    src.write_text("v\n1\n2\n")
    session = engine.get_session()

    def _build() -> Any:
        df = session.read.csv(str(src), header=True)
        return df.withColumn("w", fn.lit("x")).filter(fn.col("v") == "1")

    c1 = engine.checkpoint(_build(), "common_a")
    c2 = engine.checkpoint(_build(), "common_b")
    assert c1 is c2
    assert _vals(c1) == [("1", "x")]
    # Different plans must still get different checkpoints.
    other = session.read.csv(str(src), header=True).withColumn("w", fn.lit("y"))
    assert engine.checkpoint(other, "common_c") is not c1


def test_plan_key_alpha_renames_aliases_only() -> None:
    from ovid2.core.engines.duckdb import _plan_key

    class _F:
        def __init__(self, sql: str) -> None:
            self._sql = sql

        def sql_query(self) -> str:
            return self._sql

    a = _F("SELECT __ov3.a FROM (SELECT * FROM t) AS __ov3 JOIN u AS __ovq9 ON __ov3.k = __ovq9.k")
    b = _F("SELECT __ov8.a FROM (SELECT * FROM t) AS __ov8 JOIN u AS __ovq1 ON __ov8.k = __ovq1.k")
    c = _F("SELECT __ov8.a FROM (SELECT * FROM t) AS __ov8 JOIN u AS __ovq1 ON __ovq1.k = __ov8.k")
    assert _plan_key(a) == _plan_key(b)  # type: ignore[arg-type]
    assert _plan_key(a) != _plan_key(c)  # type: ignore[arg-type]


def test_cgroup_memory_cap_bounds_default_limit(tmp_path: Path) -> None:
    from ovid2.core.engines.duckdb import _cgroup_memory_gb

    f = tmp_path / "memory.max"
    f.write_text(f"{200 * 1024**3}\n")
    assert _cgroup_memory_gb(str(f)) == 200
    f.write_text("max\n")
    assert _cgroup_memory_gb(str(f)) is None
    assert _cgroup_memory_gb(str(tmp_path / "absent")) is None


def test_write_csv_file_row_count_from_copy(engine: DuckDBEngine, tmp_path: Path) -> None:
    """Rows come from COPY's result; a NUL-wrapped line break is one row, not two."""
    from ovid2.core.engines.duckdb_stager import write_csv_file

    df = _df(engine, [("1", "a\nb"), ("2", "c")], "k string, v string")
    target = tmp_path / "out.csv"
    assert write_csv_file(engine, df, str(target), delimiter=",") == 2
    data = target.read_bytes()
    assert data.startswith(b"k,v\n")
    assert b"\x00a\nb\x00" in data


def test_create_dataframe_dict_rows_sorted_like_spark(engine: DuckDBEngine) -> None:
    df = engine.get_session().createDataFrame([{"b": "2", "a": "1"}])
    assert df.columns == ["a", "b"]


def test_dsl_dispatches_to_active_engine(engine: DuckDBEngine) -> None:
    from ovid2.core.engines.dsl import col, lit, when

    df = _df(engine, [("1",)], "v string")
    assert _vals(df.select(when(col("v") == "1", lit("y")).otherwise("n").alias("o"))) == [("y",)]


# ---------------------------------------------------------------- disc I/O


def test_disc_csv_contract_roundtrip(engine: DuckDBEngine, tmp_path: Path, fn: Any) -> None:
    from ovid2.drivers.disc.driver import DiscDriver

    src = tmp_path / "in"
    src.mkdir()
    (src / "T.txt").write_bytes(b' a \ta\t\t"q d"\n 1 \t\t"x""y"\t4\textra\n2\n\xff\xfe\t\t\t\n')
    d = DiscDriver(args={"host_path": str(src)}, location={"format": "csv"})
    stager = engine.get_stager()
    df = stager.materialise(d, "p", "b", "T*")
    # Spark header rules: trimmed, duplicate names suffixed with their
    # index, empty -> _c<i>; values trimmed, '' -> NULL, ragged padded.
    assert df.columns == ["a0", "a1", "_c2", "q d"]
    rows = _vals(df)
    assert rows[0] == ("1", None, 'x"y', "4")
    assert rows[1] == ("2", None, None, None)
    out = DiscDriver(args={"host_path": str(tmp_path / "out")}, location={"format": "csv"})
    stager.persist(df.withColumn("n", fn.lit(1.5)), out, "p", "b", "t")
    text = (tmp_path / "out" / "t.csv").read_bytes().decode("utf-8")
    assert text.splitlines()[0] == "a0\ta1\t_c2\tq d\tn"
    assert text.splitlines()[1] == '1\t\tx"y\t4\t1.5'


def test_disc_csv_writer_quoting_matches_spark(engine: DuckDBEngine, tmp_path: Path) -> None:
    from ovid2.core.engines.duckdb_stager import write_csv_file

    df = engine.create_dataframe(
        [("plain", "", None, "a\tb", "a\nb", 1e20, True)],
        "c0 string, c1 string, c2 string, c3 string, c4 string, d double, b boolean",
    )
    target = tmp_path / "t.csv"
    write_csv_file(engine, df, str(target), delimiter="\t")
    # Recorded from Spark 4.1 ``drivers/disc/spark.write_dataframe``.
    body = target.read_bytes().split(b"\n", 1)[1]
    assert body == b'plain\t""\t\t\x00a\tb\x00\t\x00a\nb\x00\t1.0E20\ttrue\n'


@pytest.mark.parametrize(
    ("value", "java"),
    [
        (1.5, "1.5"),
        (12.0, "12.0"),
        (1e20, "1.0E20"),
        (1e7, "1.0E7"),
        (9999999.0, "9999999.0"),
        (0.001, "0.001"),
        (0.0001, "1.0E-4"),
        (123456789.123, "1.23456789123E8"),
        (-0.07, "-0.07"),
        (float("nan"), "NaN"),
    ],
)
def test_double_rendering_matches_java(engine: DuckDBEngine, value: float, java: str) -> None:
    from ovid2.core.engines.duckdb_stager import _shortest_double_text

    conn = engine.connection()
    _shortest_double_text(conn)
    assert conn.execute("SELECT __ovid2_java_double(?::DOUBLE)", [value]).fetchone()[0] == java


def test_spark_header_names() -> None:
    from ovid2.core.engines.duckdb_stager import spark_header_names

    assert spark_header_names([" a ", "A", "", None, "b"]) == ["a0", "A1", "_c2", "_c3", "b"]


def test_stager_supports_every_driver_type() -> None:
    from ovid2.core.engines.duckdb_stager import DuckDBEngineStager
    from ovid2.drivers.registry import DRIVERS

    assert DuckDBEngineStager.supported_driver_types() == frozenset(DRIVERS)


def test_requires_family_gate(engine: DuckDBEngine) -> None:
    from ovid2.core.engines.protocols import EngineOperationUnsupported
    from ovid2.core.pipeline import _assert_formatter_family
    from ovid2.formatters.base import BaseFormatter

    class _SparkOnly(BaseFormatter):
        requires_family = "spark"
        table_name = "t"  # type: ignore[assignment]
        input_file_name = "t"  # type: ignore[assignment]

        def format(self, df: Any) -> Any:
            return df

    with pytest.raises(EngineOperationUnsupported):
        _assert_formatter_family(engine, _SparkOnly())


def test_read_csv_frame_falls_back_when_parallel_scanner_refuses(
    engine: DuckDBEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (ACME ``visit_occurrence.csv``, 2026-11-01).

    DuckDB's parallel CSV scanner raises ``NotImplementedException: The
    Parallel CSV Reader currently does not support a full read on this
    file`` on some valid files; the read must retry single-threaded
    instead of failing the table.
    """
    from ovid2.core.engines import duckdb_stager

    src = tmp_path / "visit.csv"
    src.write_text('"id","v"\n"1","a"\n"2","b"\n')
    real = duckdb_stager.read_csv_relation
    calls: list[bool] = []

    def _refusing(conn: Any, path: Any, **kw: Any) -> Any:
        calls.append(bool(kw.get("parallel", True)))
        if kw.get("parallel", True):
            raise RuntimeError(
                "Not implemented Error: The Parallel CSV Reader currently does not support a "
                "full read on this file. To correctly parse this file, please run with the "
                "single threaded error (i.e., parallel = false)"
            )
        return real(conn, path, **kw)

    monkeypatch.setattr(duckdb_stager, "read_csv_relation", _refusing)
    frame = duckdb_stager.read_csv_frame(engine, str(src), delimiter=",", name="visit")
    # parallel strict -> parallel lenient -> serial lenient.
    assert calls == [True, True, False]
    assert sorted(_vals(frame)) == [("1", "a"), ("2", "b")]
    # The winning rung is remembered: a second table reading the same
    # source goes straight to the serial parse (no failed attempts).
    calls.clear()
    duckdb_stager.read_csv_frame(engine, str(src), delimiter=",", name="visit")
    assert calls == [False]


def _write_csv(path: Path, text: str) -> str:
    path.write_bytes(text.encode("utf-8"))
    return str(path)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Quoted line breaks: accepted by the parallel *strict* scanner.
        ('a,b,c\n1,2,3\n"x\ny",5,6\n', [("1", "2", "3"), ("x\ny", "5", "6")]),
        # Short row: strict rejects -> lenient pads with NULL (Spark parity).
        ("a,b,c\n1,2\n4,5,6\n", [("1", "2", None), ("4", "5", "6")]),
        # Long row: truncated to the header width in both modes.
        ("a,b,c\n1,2,3,4\n", [("1", "2", "3")]),
        # Stray quote inside an unquoted value.
        ('a,b,c\n1,5" x,3\n', [("1", '5" x', "3")]),
        # Text after a closing quote: strict rejects -> lenient repairs.
        ('a,b,c\n"ab"cd,2,3\n', [("abc", "2", "3")]),
        # Short row *and* quoted line break: parallel refuses -> serial.
        ('a,b,c\n1,2\n"x\ny",5,6\n', [("1", "2", None), ("x\ny", "5", "6")]),
    ],
)
def test_read_csv_frame_scanner_ladder_matches_lenient(
    engine: DuckDBEngine, tmp_path: Path, body: str, expected: list[tuple[Any, ...]]
) -> None:
    """Whichever scanner rung wins, rows equal the lenient (Spark-parity) parse."""
    from ovid2.core.engines import duckdb_stager

    src = _write_csv(tmp_path / "s.csv", body)
    frame = duckdb_stager.read_csv_frame(engine, src, delimiter=",", name="s")
    assert sorted(_vals(frame), key=repr) == sorted(expected, key=repr)


def test_read_csv_frame_uses_parallel_strict_for_quoted_newlines(
    engine: DuckDBEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rectangular file with quoted line breaks stays on the parallel scanner."""
    from ovid2.core.engines import duckdb_stager

    src = _write_csv(tmp_path / "q.csv", 'a,b\n"x\ny",1\n"z",2\n')
    real = duckdb_stager.read_csv_relation
    calls: list[tuple[bool, bool]] = []

    def _spy(conn: Any, path: Any, **kw: Any) -> Any:
        calls.append((bool(kw.get("parallel", True)), bool(kw.get("strict", False))))
        return real(conn, path, **kw)

    monkeypatch.setattr(duckdb_stager, "read_csv_relation", _spy)
    duckdb_stager.read_csv_frame(engine, src, delimiter=",", name="q")
    assert calls == [(True, True)]


def test_read_csv_frame_skips_strict_when_staging_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lazy (unstaged) strict read would fail at write time, past the fallback."""
    from ovid2.core.engines import duckdb_stager

    eng = DuckDBEngine(args={"threads": 2, "stage_sources": "false"})
    try:
        src = _write_csv(tmp_path / "l.csv", "a,b,c\n1,2\n")
        frame = duckdb_stager.read_csv_frame(eng, src, delimiter=",", name="l")
        assert _vals(frame) == [("1", "2", None)]
    finally:
        eng.stop_session()


def test_read_csv_relation_single_threaded_option(engine: DuckDBEngine, tmp_path: Path) -> None:
    from ovid2.core.engines.duckdb_stager import read_csv_relation

    src = tmp_path / "x.csv"
    src.write_text('"a","b"\n"1","multi\nline"\n')
    rel = read_csv_relation(engine.connection(), str(src), parallel=False)
    assert rel.fetchall() == [("1", "multi\nline")]


def test_parallel_refusal_detector() -> None:
    from ovid2.core.engines.duckdb_stager import _is_parallel_scanner_refusal

    assert _is_parallel_scanner_refusal(
        RuntimeError("The Parallel CSV Reader currently ... (i.e., parallel = false)")
    )
    assert not _is_parallel_scanner_refusal(RuntimeError("Invalid unicode (byte sequence)"))


# ---------------------------------------------------------- CSV parity (ACME run)


def test_headerless_csv_gets_spark_c_names(engine: DuckDBEngine, tmp_path: Path) -> None:
    """header=false -> ``_c0.._cN`` like Spark (list_remover reads)."""
    p = tmp_path / "list.csv"
    p.write_text('1234-5,x\n"5678-9",y\n', encoding="utf-8")
    df = engine.get_session().read.load(
        str(p), format="csv", inferSchema="false", header="false", quote='"'
    )
    assert df.columns == ["_c0", "_c1"]
    assert _vals(df) == [("1234-5", "x"), ("5678-9", "y")]


def test_header_with_space_before_quote_is_unquoted(engine: DuckDBEngine, tmp_path: Path) -> None:
    """``"a", "b"`` headers (ACME external_meds) carry no stray quotes."""
    p = tmp_path / "em.csv"
    p.write_text('"externalmedid", "extmed_source" \n"1","OD"\n', encoding="utf-8")
    df = engine.get_session().read.csv(str(p), header=True)
    assert df.columns == ["externalmedid", "extmed_source"]
    out = df.selectExpr("externalmedid as EXTMEDID", "extmed_source as EXTMED_SOURCE")
    assert _vals(out) == [("1", "OD")]


def test_empty_source_placeholder_and_projection(engine: DuckDBEngine) -> None:
    """Absent optional source -> sentinel frame -> empty CDM-shaped table."""
    from ovid2.formatters.adapters._sources import _empty_df, is_empty_source
    from ovid2.formatters.engine import _empty_projected_df
    from ovid2.formatters.spec import FormatterSpec

    placeholder = _empty_df(engine.get_session())
    assert is_empty_source(placeholder)
    spec = FormatterSpec(
        v=1,
        pipeline="demo",
        formatter="pcornet_trial",
        adapter="source_passthrough",
        projections={"PATID": "patid", "TRIALID": "trialid"},
    )
    out = _empty_projected_df(placeholder, spec)
    assert out.columns == ["PATID", "TRIALID"]
    assert _vals(out) == []


def test_scanner_probe_lock_is_per_source(engine: DuckDBEngine) -> None:
    from ovid2.core.engines.duckdb_stager import _scanner_probe_lock

    a = _scanner_probe_lock(engine, "a")
    assert _scanner_probe_lock(engine, "a") is a
    assert _scanner_probe_lock(engine, "b") is not a
