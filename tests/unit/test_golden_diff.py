"""Unit tests for the golden-file diff harness."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.tools.golden_diff import compare_csv, compare_directories


def _write_csv(path: Path, header: list[str], rows: list[list[str]], delimiter: str = "|") -> None:
    """Helper to write a CSV with a header row."""
    path.write_text(
        delimiter.join(header) + "\n" + "\n".join(delimiter.join(r) for r in rows) + "\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_compare_csv_match(tmp_path: Path) -> None:
    """Identical files should report ``match``."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["PATID", "SEX"], [["1", "M"], ["2", "F"]])
    _write_csv(b, ["PATID", "SEX"], [["2", "F"], ["1", "M"]])  # reordered rows

    result = compare_csv(a, b)
    assert result.status == "match"


@pytest.mark.unit
def test_compare_csv_column_diff(tmp_path: Path) -> None:
    """Different headers should report ``column_diff``."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["PATID", "SEX"], [["1", "M"]])
    _write_csv(b, ["PATID", "GENDER"], [["1", "M"]])

    result = compare_csv(a, b)
    assert result.status == "column_diff"
    assert "SEX" in result.detail or "GENDER" in result.detail


@pytest.mark.unit
def test_compare_csv_row_diff(tmp_path: Path) -> None:
    """Extra rows should be reported with an ``only_a``/``only_b`` count."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["PATID"], [["1"], ["2"], ["3"]])
    _write_csv(b, ["PATID"], [["1"], ["2"]])

    result = compare_csv(a, b)
    assert result.status == "row_diff"


@pytest.mark.unit
def test_compare_directories(tmp_path: Path) -> None:
    """`compare_directories` should aggregate per-file diffs."""
    left = tmp_path / "legacy"
    right = tmp_path / "ovid2"
    left.mkdir()
    right.mkdir()

    _write_csv(left / "demographic.csv", ["PATID"], [["1"]])
    _write_csv(right / "demographic.csv", ["PATID"], [["1"]])
    _write_csv(left / "encounter.csv", ["ENC"], [["1"]])

    diff = compare_directories(left, right)
    statuses = {f.path: f.status for f in diff.files}
    assert statuses["demographic.csv"] == "match"
    assert statuses["encounter.csv"] == "only_a"
    assert not diff.match


@pytest.mark.unit
def test_compare_csv_masks_updated(tmp_path: Path) -> None:
    """Masked columns (UPDATED) never cause a diff."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["PATID", "UPDATED"], [["1", "2026-01-01 00:00:00"]], "\t")
    _write_csv(b, ["PATID", "UPDATED"], [["1", "2026-11-04 10:00:00"]], "\t")
    assert compare_csv(a, b, delimiter="\t").status == "row_diff"
    assert compare_csv(a, b, delimiter="\t", mask_columns=["UPDATED"]).status == "match"


@pytest.mark.unit
def test_compare_csv_mask_glob(tmp_path: Path) -> None:
    """A glob mask (``REFRESH_*_DATE``) blanks every matching column only."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    header = ["NETWORKID", "REFRESH_DEATH_DATE", "REFRESH_VITAL_DATE"]
    _write_csv(a, header, [["C13", "2026-11-05", "2026-11-05"]], "\t")
    _write_csv(b, header, [["C13", "2026-11-06", "2026-11-06"]], "\t")
    assert compare_csv(a, b, delimiter="\t", mask_columns=["REFRESH_*_DATE"]).status == "match"
    _write_csv(b, header, [["C99", "2026-11-06", "2026-11-06"]], "\t")
    assert compare_csv(a, b, delimiter="\t", mask_columns=["REFRESH_*_DATE"]).status == "row_diff"


@pytest.mark.unit
def test_compare_csv_key_pairing_attributes_columns(tmp_path: Path) -> None:
    """Leftover rows that share a key report which columns disagree."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["PATID", "SEX", "RACE"], [["1", "M", "05"], ["2", "F", "03"]], "\t")
    _write_csv(b, ["PATID", "SEX", "RACE"], [["1", "M", "05"], ["2", "F", "OT"]], "\t")
    result = compare_csv(a, b, delimiter="\t", key_columns=["PATID"])
    assert result.status == "key_match"
    assert result.column_mismatches == {"RACE": 1}
    assert result.samples[0]["diff"]["RACE"] == {"a": "03", "b": "OT"}


@pytest.mark.unit
def test_compare_csv_unpaired_rows_stay_row_diff(tmp_path: Path) -> None:
    """A row with no partner on the key is a real diff."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["PATID"], [["1"], ["2"]], "\t")
    _write_csv(b, ["PATID"], [["1"], ["3"]], "\t")
    result = compare_csv(a, b, delimiter="\t", key_columns=["PATID"])
    assert result.status == "row_diff"
    assert result.only_a == 1 and result.only_b == 1


@pytest.mark.unit
def test_compare_csv_quotes_are_data(tmp_path: Path) -> None:
    """Both engines write QUOTE_NONE; a stray quote must not merge fields."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["X", "Y"], [['"abc', "1"]], "\t")
    _write_csv(b, ["X", "Y"], [['"abc', "2"]], "\t")
    result = compare_csv(a, b, delimiter="\t", key_columns=["X"])
    assert result.column_mismatches == {"Y": 1}


@pytest.mark.unit
def test_compare_csv_ignore_column_order(tmp_path: Path) -> None:
    """Column order can optionally be ignored."""
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    _write_csv(a, ["A", "B"], [["1", "2"]], "\t")
    _write_csv(b, ["B", "A"], [["2", "1"]], "\t")
    assert compare_csv(a, b, delimiter="\t").status == "column_diff"
    assert compare_csv(a, b, delimiter="\t", ignore_column_order=True).status == "match"


@pytest.mark.unit
def test_compare_directories_prefixes(tmp_path: Path) -> None:
    """Legacy ``fixed_<t>.csv`` pairs with Ovid2 ``<t>.csv``."""
    left = tmp_path / "legacy"
    right = tmp_path / "ovid2"
    left.mkdir()
    right.mkdir()
    _write_csv(left / "fixed_death.csv", ["PATID"], [["1"]], "\t")
    _write_csv(left / "mapped_death.csv", ["PATID"], [["9"]], "\t")
    _write_csv(right / "death.csv", ["PATID"], [["1"]], "\t")
    diff = compare_directories(left, right, delimiter="\t", a_prefix="fixed_")
    assert [(f.path, f.status) for f in diff.files] == [("death.csv", "match")]
    assert diff.to_dict()["match"] is True


def test_cli_key_flag_reports_key_match(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """``--key TABLE=COL`` pairs leftover rows; all-paired files are KEY."""
    from ovid2.tools.golden_diff import main

    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "fixed_t.csv").write_text("ID\tV\n1\tx\n2\ty\n")
    (b / "t.csv").write_text("ID\tV\n1\tz\n2\ty\n")
    out = tmp_path / "r.json"
    rc = main(
        [
            str(a),
            str(b),
            "--delimiter",
            "\t",
            "--a-prefix",
            "fixed_",
            "--key",
            "t=ID",
            "--json",
            str(out),
        ]
    )
    import json

    assert rc == 1
    assert json.loads(out.read_text())["files"][0]["status"] == "key_match"
