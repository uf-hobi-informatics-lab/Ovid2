"""Unit tests for the legacy-Ovid byte-parity layer (``ovid2.legacy``)."""

from __future__ import annotations

import glob
import os
from pathlib import Path

import pytest

from ovid2.legacy.dedup import WHOLE_ROW, legacy_dedup_key
from ovid2.legacy.udfs import format_date, format_time, get_valid_zip5, return_if_float

# --------------------------------------------------------------------- pure python


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "expected"),
    [("2019-03-23", "2019-03-23"), ("03/23/2019", "2019-03-23"), ("bad", None), (None, None)],
)
def test_format_date_matches_dateutil(value: str | None, expected: str | None) -> None:
    assert format_date(value) == expected


@pytest.mark.unit
def test_format_time_and_helpers() -> None:
    assert format_time("2020-01-01 13:45:10") == "13:45"
    assert format_time("nope") == ""
    assert get_valid_zip5("32601") == "32601"
    assert get_valid_zip5("3260") is None
    assert return_if_float("1.5") == "1.5"
    assert return_if_float("x") is None


@pytest.mark.unit
def test_legacy_dedup_keys() -> None:
    cols = ["A", "B", "C", "D", "E", "F", "G"]
    assert legacy_dedup_key("DEMOGRAPHIC", cols) == ("A",)
    assert legacy_dedup_key("enrollment", cols) == ("A", "B", "E")
    assert legacy_dedup_key("unit", cols) == tuple(cols)
    # awk-path keys (what every < 40M-row batch hit), not the Spark-path 1-6.
    assert legacy_dedup_key("treatmentteam_nurse", cols) == ("A", "B", "C", "D", "E")
    assert legacy_dedup_key("goal", cols) == ("A", "B", "C", "D", "E")
    assert legacy_dedup_key("tumor_date", cols) is None
    assert legacy_dedup_key("death", cols, whole_row_tables=frozenset({"death"})) == WHOLE_ROW


@pytest.mark.unit
def test_encryption_prefix_matches_legacy() -> None:
    from ovid2.utils.common import encryption_prefix

    assert encryption_prefix("pi") == "PI"
    assert encryption_prefix("nursing_site") == "SITE"
    # legacy looks up partner.upper(): lower-case keys never match → "None"
    assert encryption_prefix("kappa") == "None"
    assert encryption_prefix("unknown") == "None"


@pytest.mark.unit
def test_xor_key_truncated_to_32_bytes() -> None:
    from ovid2.utils.common import _xor_encrypt_exact

    key = bytes(range(40))
    data = bytes(range(64))
    assert _xor_encrypt_exact(data, key) == bytes(d ^ key[:32][i % 32] for i, d in enumerate(data))


# --------------------------------------------------------------------- spark


def _write_like_legacy(df: object, out: Path) -> list[bytes]:
    df.write.format("csv").option("header", False).option("nullValue", None).option(  # type: ignore[attr-defined]
        "delimiter", "\t"
    ).option("quote", "").mode("overwrite").save(str(out))
    raw = b"".join(Path(p).read_bytes() for p in sorted(glob.glob(str(out / "part-*.csv"))))
    return raw.split(b"\n")[:-1]


@pytest.mark.unit
def test_write_normalise_is_byte_exact(spark: object, tmp_path: Path) -> None:
    from ovid2.legacy.csv_roundtrip import legacy_write_normalise

    rows = [
        ("#a", "#b", 1.5, " x", "a\tb"),
        ("x", "#", None, "", '"q'),
        ("\\", "", 1e10, None, "é ü "),
    ]
    df = spark.createDataFrame(rows, "a string, b string, d double, e string, f string")  # type: ignore[attr-defined]
    expected = _write_like_legacy(df, tmp_path / "w")
    got = [b"\t".join((v or "").encode() for v in r) for r in legacy_write_normalise(df).collect()]
    assert sorted(got) == sorted(expected)


@pytest.mark.unit
def test_read_normalise_turns_empty_tokens_into_null(spark: object) -> None:
    from ovid2.legacy.csv_roundtrip import legacy_round_trip

    df = spark.createDataFrame([(" a ", "", None, '""')], "a string, b string, c string, d string")  # type: ignore[attr-defined]
    assert [tuple(r) for r in legacy_round_trip(df).collect()] == [("a", None, None, None)]


@pytest.mark.unit
def test_legacy_select_projection(spark: object, monkeypatch: pytest.MonkeyPatch) -> None:
    import types

    from ovid2.cdm.registry import get_cdm
    from ovid2.mappers.base import MapOpContext
    from ovid2.mappers.ops.legacy_select import LegacySelectOp

    dicts = types.SimpleNamespace(sex_dict={"MALE": "M"})
    ctx = MapOpContext(
        spark=spark,
        pipeline="pi",
        table="t",
        cdm=get_cdm("pcornet_7_0"),  # type: ignore[arg-type]
        pipeline_dictionaries=dicts,
    )
    df = spark.createDataFrame(
        [("1", "male", "keep"), ("2", "X", "k2")], "ID string, SEX string, EXTRA string"
    )  # type: ignore[attr-defined]
    params = {
        "columns": [
            "ID",
            {"SEX": {"dict": "sex_dict", "col": "SEX"}},
            {"SOURCE": {"source": True}},
        ],
        "append_additional_fields": {"join_field": "ID"},
    }
    out = LegacySelectOp().apply(df, params, ctx)
    assert out.columns == ["ID", "SEX", "SOURCE", "EXTRA"]
    assert sorted(tuple(r) for r in out.collect()) == [
        ("1", "M", "PI", "keep"),
        ("2", "X", "PI", "k2"),
    ]


@pytest.mark.unit
def test_legacy_fix_stage_reads_fixes_literal(tmp_path: Path) -> None:
    from ovid2.legacy.fix_stage import read_fixes

    script = tmp_path / "x_fixer.py"
    script.write_text(
        "fixes = {\n"
        '  "2": {"fix_type":"common","fix_name":"b_fix","version":"1.0","src1":"s","src2":"t"},\n'
        '  # "3": {"fix_type":"common","fix_name":"skip","version":"1.0","src1":"","src2":""},\n'
        '  "1": {"fix_type":"custom","fix_name":"a_fix","version":"1.0","src1":"x","src2":"y"},\n'
        "}\n",
        encoding="utf-8",
    )
    entries = read_fixes(script)
    assert [e.fix_name for e in entries] == ["a_fix", "b_fix"]


@pytest.mark.unit
def test_shim_resolves_legacy_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ovid2.legacy import fix_shim

    vendor = tmp_path / "vendor"
    (vendor / "common" / "lists").mkdir(parents=True)
    (vendor / "common" / "lists" / "loinc_list.csv").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("OVID2_LEGACY_VENDOR_DIR", str(vendor))
    monkeypatch.setitem(fix_shim._STATE, "work_root", str(tmp_path / "run"))
    assert fix_shim.resolve_legacy_path("/app/common/lists/loinc_list.csv") == os.path.join(
        vendor, "common", "lists", "loinc_list.csv"
    )
    assert fix_shim.resolve_legacy_path("/app/partners/p/data/a/b/fixer_output/fixed_x.csv") == str(
        tmp_path / "run" / "fixer_output" / "fixed_x.csv"
    )
    assert fix_shim.resolve_legacy_path("/somewhere/else.csv") == "/somewhere/else.csv"


# ------------------------------------------------- CPython-3.10 strftime parity


@pytest.mark.unit
def test_legacy_strftime_unpadded_year() -> None:
    """Legacy ran CPython 3.10 (glibc): ``%Y`` is unpadded for years < 1000."""
    from datetime import date, datetime

    from ovid2.legacy.udfs import get_date_from_datetime, legacy_strftime

    assert format_date("12/18/0201") == "201-12-18"
    assert legacy_strftime(datetime(201, 11, 18, 9, 5), "%Y-%m-%d %H:%M") == "201-11-18 09:05"
    assert legacy_strftime(date(5, 1, 2), "%%Y=%Y") == "%Y=5"
    assert legacy_strftime(date(2019, 3, 4), "%Y-%m-%d") == "2019-03-04"
    assert get_date_from_datetime(datetime(999, 1, 1)) == "999-01-01"


@pytest.mark.unit
def test_migrator_rewrites_method_strftime_only() -> None:
    from ovid2.tools.legacy_formatter_migrator import port_formatter

    src = (
        "import time\n"
        "try:\n"
        "    a = x.strftime('%Y')\n"
        "    b = time.strftime('%Y')\n"
        "    cf.write_pyspark_output_file(payspark_df=df, output_file_name='formatted_t.csv',"
        " output_data_folder_path='/app/partners/p/data/{x}/formatter_output/')\n"
        "except Exception as e:\n"
        "    pass\n"
    )
    code = port_formatter(src, pipeline="p", origin="o").source
    assert "a = legacy_strftime(x, '%Y')" in code
    assert "b = time.strftime('%Y')" in code
    assert "from ovid2.legacy.udfs import legacy_strftime" in code


# ------------------------------------------- stage-boundary text (W not idempotent)


def _legacy_file_roundtrip(spark: object, df: object, tmp: Path) -> object:
    """Real legacy W (write_pyspark_output_file options) → R (spark_read options)."""
    df.coalesce(1).write.format("csv").option("header", True).option("nullValue", None).option(  # type: ignore[attr-defined]
        "delimiter", "\t"
    ).option("quote", "").mode("overwrite").save(str(tmp))
    return spark.read.csv(  # type: ignore[attr-defined]
        str(tmp), sep="\t", header=True, quote='"', inferSchema=False, multiLine=False, escape='"'
    )


@pytest.mark.unit
def test_undeduplicated_table_is_w_r_w(spark: object, tmp_path: Path) -> None:
    """tumor_date: legacy re-wrote the formatter file (W∘R∘W), escaping it twice."""
    from ovid2.legacy.csv_roundtrip import legacy_read_normalise, write_legacy_text
    from ovid2.legacy.dedup import legacy_deduplicate

    vals = ["99\t", "a\\b", "", None, "plain", '"q"']
    df = spark.createDataFrame([(str(i), v) for i, v in enumerate(vals)], "id string, v string")  # type: ignore[attr-defined]
    legacy = _legacy_file_roundtrip(spark, df, tmp_path / "f")  # R(formatted_<t>.csv)
    text = legacy_deduplicate(df, "tumor_date", None)
    write_legacy_text(text, str(tmp_path / "o"), "t.csv")
    # Byte check against the real legacy deduplicator file.
    legacy.coalesce(1).write.format("csv").option("header", True).option("nullValue", None).option(  # type: ignore[attr-defined]
        "delimiter", "\t"
    ).option("quote", "").mode("overwrite").save(str(tmp_path / "ref"))
    ref = Path(glob.glob(str(tmp_path / "ref" / "part-*"))[0]).read_bytes()
    got = (tmp_path / "o" / "t.csv").read_bytes()
    assert sorted(got.split(b"\n")) == sorted(ref.split(b"\n"))
    # Reader view == what the legacy mapper read.
    reread = spark.read.csv(  # type: ignore[attr-defined]
        str(tmp_path / "ref"), sep="\t", header=True, quote='"', inferSchema=False, escape='"'
    )
    assert sorted(map(tuple, legacy_read_normalise(text).collect()), key=repr) == sorted(
        map(tuple, reread.collect()), key=repr
    )


@pytest.mark.unit
def test_read_normalise_matches_real_reader(spark: object, tmp_path: Path) -> None:
    """R on W-text equals reading the written file with the legacy reader options."""
    from ovid2.legacy.csv_roundtrip import legacy_read_normalise, write_legacy_text

    rows = [
        ("", ""),  # all-empty line -> dropped by the reader
        (None, None),
        (" ", None),  # blank after trim -> dropped
        ('"q"', "x"),
        ('"a""b"', ""),
        ('""', "y"),
        ("\x0099\\\x00", "z"),
        (" a ", '"x\\"y"'),
    ]
    text = spark.createDataFrame(rows, "a string, b string")  # type: ignore[attr-defined]
    write_legacy_text(text, str(tmp_path / "f"), "t.csv")
    real = spark.read.csv(  # type: ignore[attr-defined]
        str(tmp_path / "f" / "t.csv"),
        sep="\t",
        header=True,
        quote='"',
        inferSchema=False,
        escape='"',
    )
    got = sorted(map(tuple, legacy_read_normalise(text).collect()), key=repr)
    assert got == sorted(map(tuple, real.collect()), key=repr)
    assert len(got) == 5


@pytest.mark.unit
def test_safe_header_matches_spark_reader(spark: object, tmp_path: Path) -> None:
    """Empty / duplicate header names get Spark's ``_c<i>`` / ``<name><i>``."""
    from ovid2.legacy.csv_roundtrip import legacy_read_normalise, safe_header

    hdr = ["A", "", "B", "a", "", "C"]
    (tmp_path / "f.csv").write_text("\t".join(hdr) + "\n" + "\t".join("v" for _ in hdr) + "\n")
    real = spark.read.csv(str(tmp_path / "f.csv"), sep="\t", header=True, quote='"')  # type: ignore[attr-defined]
    assert safe_header(hdr) == real.columns
    df = spark.createDataFrame(
        [tuple("v" for _ in hdr)], ", ".join(f"c{i} string" for i in range(6))
    ).toDF(*hdr)  # type: ignore[attr-defined]
    assert legacy_read_normalise(df).columns == real.columns


@pytest.mark.unit
def test_infer_schema_view_matches_legacy_spark33_utc(spark: object) -> None:
    """append_additional_fields re-read with inferSchema (Spark 3.3, UTC JVM).

    Expected values were measured on the legacy image (``ovid_image_2``).
    """
    from ovid2.legacy.csv_roundtrip import legacy_infer_schema_view, legacy_write_normalise

    df = spark.createDataFrame(  # type: ignore[attr-defined]
        [("2026-06-05", "007", "1.50", "2026-06-05 10:00:00", "x"), (None, "2", "3", None, None)],
        "a string, b string, c string, d string, e string",
    )
    # Ovid2 production config sets LEGACY; the emulation must not depend on it.
    spark.conf.set("spark.sql.legacy.timeParserPolicy", "LEGACY")  # type: ignore[attr-defined]
    before = spark.conf.get("spark.sql.session.timeZone")  # type: ignore[attr-defined]
    view = legacy_infer_schema_view(legacy_write_normalise(df), spark)
    out = sorted(map(tuple, view.collect()), key=repr)
    assert spark.conf.get("spark.sql.session.timeZone") == before  # type: ignore[attr-defined]
    assert spark.conf.get("spark.sql.legacy.timeParserPolicy") == "LEGACY"  # type: ignore[attr-defined]
    assert out == sorted(
        [
            ("2026-06-05T00:00:00.000Z", "7", "1.5", "2026-06-05T10:00:00.000Z", "x"),
            (None, "2", "3.0", None, None),
        ],
        key=repr,
    )


@pytest.mark.unit
def test_awk_merge_with_no_inputs_truncates_and_raises(tmp_path: Path) -> None:
    """Shell ``awk … prefix*.csv > out``: empty glob → empty out + exit 2."""
    import subprocess

    from ovid2.legacy.fix_shim import CommonFuncitons

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "fixed_x.csv").write_text("old\n")
    with pytest.raises(subprocess.CalledProcessError):
        CommonFuncitons.awk_merge(str(tmp_path / "none"), str(out_dir) + "/", "x", "fixed_x.csv")
    assert (out_dir / "fixed_x.csv").read_bytes() == b""
    (tmp_path / "w").mkdir()
    (tmp_path / "w" / "x_1.csv").write_text("h\n1\n")
    (tmp_path / "w" / "x_2.csv").write_text("h\n2\n")
    CommonFuncitons.awk_merge(str(tmp_path / "w"), str(out_dir) + "/", "x", "fixed_x.csv")
    assert (out_dir / "fixed_x.csv").read_text() == "h\n1\n2\n"


@pytest.mark.unit
def test_read_normalise_resplits_nul_quoted_tab(spark: object, tmp_path: Path) -> None:
    """A tab inside a value: W NUL-quotes it, the legacy reader re-splits the row."""
    from ovid2.legacy.csv_roundtrip import (
        legacy_read_normalise,
        legacy_write_normalise,
        write_legacy_text,
    )

    df = spark.createDataFrame(  # type: ignore[attr-defined]
        [("1", "A\\tB", "x", "y"), ("\\tlead", "q", '"z"', None), ("#c", "", "a b", "w")],
        "a string, b string, c string, d string",
    )
    df = df.selectExpr(*[f"replace({c}, '\\\\t', '\\t') as {c}" for c in df.columns])
    text = legacy_write_normalise(df)
    write_legacy_text(text, str(tmp_path / "f"), "t.csv")
    real = spark.read.csv(  # type: ignore[attr-defined]
        str(tmp_path / "f" / "t.csv"),
        sep="\t",
        header=True,
        quote='"',
        inferSchema=False,
        multiLine=False,
        escape='"',
    )
    got = sorted(map(tuple, legacy_read_normalise(text).collect()), key=repr)
    assert got == sorted(map(tuple, real.collect()), key=repr)


@pytest.mark.unit
def test_migrator_script_without_try_runs_in_source_order() -> None:
    from ovid2.tools.legacy_formatter_migrator import port_formatter

    src = (
        "input_data_folder_path = f'/data/{input_data_folder}/'\n"
        "t = '*x.csv'\n"
        "df = spark.read.load(input_data_folder_path + t)\n"
        "cf.write_pyspark_output_file(payspark_df=df, output_file_name='formatted_x.csv',"
        " output_data_folder_path='/app/partners/p/data/{f}/formatter_output/')\n"
    )
    code = port_formatter(src, pipeline="p", origin="o").source
    body = code.split("def _legacy_body")[1]
    assert body.count("spark.read.load(") == 1
    assert body.index("t = '*x.csv'") < body.index("spark.read.load(")


@pytest.mark.unit
@pytest.mark.filterwarnings("ignore:Cannot infer the eval type")
def test_vendor_module_udfs_run_on_python_workers(spark: object) -> None:
    """``fix_transforms`` module-level UDFs must not be pickled by reference."""
    from ovid2.legacy import fix_stage

    try:
        fix_stage.vendor_root()
    except FileNotFoundError:
        pytest.skip("legacy vendor tree absent (public mirror: site ports are excluded)")

    with fix_stage._legacy_import_env(spark, "xi", "x"):  # type: ignore[arg-type]
        import fix_transforms  # type: ignore[import-not-found]

        df = spark.createDataFrame([("1.23456789012",)], "v string")  # type: ignore[attr-defined]
        out = df.select(fix_transforms.shorten_range_value_udf("v")).collect()
    assert out[0][0] == "1.234568"


@pytest.mark.unit
def test_infer_schema_view_nul_quoted_tab_matches_spark33(spark: object) -> None:
    """Measured on ``ovid_image_2``: the NUL-quoted tab value re-splits."""
    from ovid2.legacy.csv_roundtrip import legacy_infer_schema_view

    text = spark.createDataFrame(  # type: ignore[attr-defined]
        [("1", "\x00CH, U\tDETECTED\x00", "x")], "a string, b string, c string"
    )
    out = legacy_infer_schema_view(text, spark).collect()
    assert tuple(out[0]) == ("1", "\x00CH, U", "DETECTED\x00")


@pytest.mark.unit
def test_backslash_escape_reader_matches_direct_spark_read(spark: object, tmp_path: Path) -> None:
    """pro_cm_mapper reads with Spark's default escape: ``""`` is not an escaped quote."""
    from ovid2.legacy.csv_roundtrip import legacy_read_normalise, write_legacy_text

    text = spark.createDataFrame(  # type: ignore[attr-defined]
        [("1", '"x ""K2', "", "No", '"" ""S"")?"', "9")],
        "a string, b string, c string, d string, e string, f string",
    )
    write_legacy_text(text, str(tmp_path / "f"), "t.csv")
    for reader, opts in (("spark_read", {"escape": '"'}), ("backslash_escape", {})):
        real = spark.read.csv(  # type: ignore[attr-defined]
            str(tmp_path / "f" / "t.csv"),
            sep="\t",
            header=True,
            quote='"',
            inferSchema=False,
            **opts,
        )
        got = [tuple(r) for r in legacy_read_normalise(text, reader=reader).collect()]
        assert got == [tuple(r) for r in real.collect()], reader


@pytest.mark.unit
def test_non_stopping_session_stop_is_local(spark: object) -> None:
    """``spark.stop()`` in a legacy script poisons that proxy only (legacy semantics)."""
    from ovid2.legacy.fix_shim import NonStoppingSession

    first = NonStoppingSession(spark)  # type: ignore[arg-type]
    assert first.range(1).count() == 1
    first.stop()
    with pytest.raises(RuntimeError, match="stopped SparkContext"):
        first.range(1)
    assert NonStoppingSession(spark).range(2).count() == 2  # type: ignore[arg-type]
    assert spark.range(3).count() == 3  # type: ignore[attr-defined]


@pytest.mark.unit
def test_legacy_dedup_keeps_first_row_in_file_order(spark: object) -> None:
    """awk ``!seen[$1]++``: the first occurrence in write order wins."""
    from ovid2.legacy.dedup import legacy_deduplicate

    rows = [("1", "first"), ("2", "a"), ("1", "second"), ("3", "b"), ("1", "third")]
    df = spark.createDataFrame(rows, "PATID string, V string").coalesce(1)  # type: ignore[attr-defined]
    out = {r["PATID"]: r["V"] for r in legacy_deduplicate(df, "death", None).collect()}
    assert out == {"1": "first", "2": "a", "3": "b"}
