"""Unit tests for ``ovid2.legacy.runner._publish_fixed`` guards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ovid2.legacy import runner

pytestmark = pytest.mark.unit


class _Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def write(self, df: Any, spark: Any, table: str) -> None:
        self.calls.append(table)


def test_empty_fixed_file_is_refused_before_warehouse_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty ``fixed_<t>.csv`` must not reach the writer.

    Regression: a fix that failed part-way left an empty file; reading it
    gave a zero-column frame and Snowflake failed with ``syntax error …
    unexpected ')'`` (``CREATE TABLE … ()``), hiding the real cause.
    """
    monkeypatch.setattr(runner, "_disc_csv_root", lambda _addr: None)
    path = tmp_path / "fixed_lab_result_cm.csv"
    path.write_text("")
    pub = _Publisher()
    with pytest.raises(ValueError, match="empty"):
        runner._publish_fixed(object(), object(), pub, "lab_result_cm", str(path))  # type: ignore[arg-type]
    assert pub.calls == []


def test_non_empty_fixed_file_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "_disc_csv_root", lambda _addr: None)
    monkeypatch.setattr(runner, "_read_tab", lambda _spark, _path: "frame")
    path = tmp_path / "fixed_demographic.csv"
    path.write_text("PATID\tSEX\n1\tF\n")
    pub = _Publisher()
    runner._publish_fixed(object(), object(), pub, "demographic", str(path))  # type: ignore[arg-type]
    assert pub.calls == ["demographic"]
