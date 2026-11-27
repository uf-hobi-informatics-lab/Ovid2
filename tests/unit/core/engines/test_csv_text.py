"""The pure-Python Spark CSV text model reproduces Spark 4.1 byte-for-byte.

``ovid2.core.engines.csv_text`` is how non-Spark engines emulate legacy
Ovid's stage-boundary CSV round trips. The oracle
(``data/spark_csv_oracle.json.gz``) was produced by running real Spark
4.1 ``to_csv`` / ``DataFrame.write.csv`` / ``spark.read.csv`` with the
exact legacy options over fuzzed strings (NUL, tabs, quotes,
backslashes, comment chars, CR/LF, whitespace).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from ovid2.core.engines.csv_text import is_blank_line, parse_line, write_cell, write_line

pytestmark = pytest.mark.unit

_ORACLE = Path(__file__).parent / "data" / "spark_csv_oracle.json.gz"


@pytest.fixture(scope="module")
def oracle() -> dict[str, Any]:
    with gzip.open(_ORACLE, "rt", encoding="utf-8") as fh:
        return dict(json.load(fh))


def test_writer_cell_matches_spark_to_csv(oracle: dict[str, Any]) -> None:
    bad = [
        (v, expected)
        for v, expected in oracle["writer"]
        if write_cell(v, line_start=True) != expected
    ]
    assert not bad, bad[:5]


def test_writer_file_body_matches_spark_csv_writer(oracle: dict[str, Any]) -> None:
    vals = oracle["file_body"]["vals"]
    body = "".join(write_line([v, v, "|"]) + "\n" for v in vals)
    assert body == oracle["file_body"]["body"]


@pytest.mark.parametrize(("escape", "slot"), [('"', 2), ("\\", 3)])
def test_reader_matches_spark_csv_reader(oracle: dict[str, Any], escape: str, slot: int) -> None:
    bad = []
    for row in oracle["reader"]:
        line, width = row[0], row[1]
        expected = row[slot]
        got = parse_line(line, width=width, escape=escape)
        if got != expected:
            bad.append((line, expected, got))
    assert not bad, bad[:5]


def test_blank_lines_are_skipped() -> None:
    assert is_blank_line(" \t\x00")
    assert not is_blank_line(" a ")


def test_reader_pads_and_truncates_to_width() -> None:
    assert parse_line("a", width=3) == ["a", None, None]
    assert parse_line("a\tb\tc\td", width=2) == ["a", "b"]


def test_reader_whitespace_options_match_spark(oracle: dict[str, Any]) -> None:
    """``ignoreLeadingWhiteSpace`` / ``ignoreTrailingWhiteSpace`` × escape, ``sep=','``."""
    bad = []
    for key, pairs in oracle["reader_ws"].items():
        lead, trail, esc = key.split("|")
        escape = '"' if esc == '"' else "\\"
        for line, expected in pairs:
            got = parse_line(
                line,
                width=4,
                delimiter=",",
                escape=escape,
                ignore_leading=lead == "True",
                ignore_trailing=trail == "True",
            )
            if got != expected:
                bad.append((key, line, expected, got))
    assert not bad, bad[:5]
