"""Unit tests for :mod:`ovid2.tools.parity_sample`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.tools.parity_sample import sample


@pytest.mark.unit
def test_sample_filters_by_patient_and_bounds_scan(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "person.csv").write_text("person_id,x\n" + "".join(f"{i},a\n" for i in range(50)))
    (src / "visit.csv").write_text(
        "visit_id,person_id\n" + "".join(f"v{i},{i % 50}\n" for i in range(500))
    )
    (src / "visit.csv.gpg").write_bytes(b"\x00secret")
    (src / "lookup.csv").write_text("code,name\nA,alpha\n")
    kept = sample(src, tmp_path / "dst", 5, max_scan_bytes=200, skip_suffixes=(".gpg",))
    assert "visit.csv.gpg" not in kept
    assert kept["lookup.csv"] == -1
    assert kept["person.csv"] == 5
    visit_rows = (tmp_path / "dst" / "visit.csv").read_text().splitlines()[1:]
    # Only the first ~200 bytes were scanned, and only sampled patients kept.
    assert len(visit_rows) == kept["visit.csv"] <= 20
    people = {
        r.split(",")[0] for r in (tmp_path / "dst" / "person.csv").read_text().splitlines()[1:]
    }
    assert all(r.split(",")[1] in people for r in visit_rows)
