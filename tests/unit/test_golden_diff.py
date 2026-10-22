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
