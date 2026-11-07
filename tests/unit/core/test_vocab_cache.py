"""Unit tests for :mod:`ovid2.core.vocab_cache` (columnar vocabulary cache)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ovid2.core import vocab_cache as vc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OVID2_VOCAB_PARQUET_CACHE", raising=False)
    monkeypatch.setenv("OVID2_VOCAB_CACHE_DIR", str(tmp_path / "cache"))


def _src(tmp_path: Path, name: str = "CONCEPT.csv", body: str = "a\tb\n1\t2\n") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def test_miss_then_publish_then_hit(tmp_path: Path) -> None:
    src = _src(tmp_path)
    assert vc.cached_parquet(src, delimiter="\t", header=True) is None
    target = vc.build_target(src, delimiter="\t", header=True)
    assert target is not None
    temp, final = target
    temp.mkdir()
    (temp / "part-0.parquet").write_bytes(b"x")
    assert vc.publish(temp, final) == final
    assert vc.cached_parquet(src, delimiter="\t", header=True) == final


def test_incomplete_dir_is_invisible(tmp_path: Path) -> None:
    src = _src(tmp_path)
    _temp, final = vc.build_target(src, delimiter="\t", header=True)  # type: ignore[misc]
    final.mkdir(parents=True)  # no marker
    assert vc.cached_parquet(src, delimiter="\t", header=True) is None


def test_key_changes_with_file_and_options(tmp_path: Path) -> None:
    src = _src(tmp_path)
    k1 = vc.cache_key(src, delimiter="\t", header=True)
    assert k1 != vc.cache_key(src, delimiter=",", header=True)
    assert k1 != vc.cache_key(src, delimiter="\t", header=False)
    src.write_text("a\tb\n1\t2\n3\t4\n")
    os.utime(src, ns=(1, 1))
    assert k1 != vc.cache_key(src, delimiter="\t", header=True)


def test_new_version_prunes_old(tmp_path: Path) -> None:
    src = _src(tmp_path, "CONCEPT_RELATIONSHIP.csv")
    temp, final = vc.build_target(src, delimiter="\t", header=True)  # type: ignore[misc]
    temp.mkdir()
    vc.publish(temp, final)
    other = _src(tmp_path, "CONCEPT.csv")
    t2, f2 = vc.build_target(other, delimiter="\t", header=True)  # type: ignore[misc]
    t2.mkdir()
    vc.publish(t2, f2)
    src.write_text("changed\n")
    os.utime(src, ns=(5, 5))
    t3, f3 = vc.build_target(src, delimiter="\t", header=True)  # type: ignore[misc]
    t3.mkdir()
    vc.publish(t3, f3)
    assert not final.exists()  # old CONCEPT_RELATIONSHIP version pruned
    assert f2.exists()  # CONCEPT untouched (different stem)
    assert f3.exists()


def test_lost_race_keeps_winner(tmp_path: Path) -> None:
    src = _src(tmp_path)
    t1, final = vc.build_target(src, delimiter="\t", header=True)  # type: ignore[misc]
    t2, _ = vc.build_target(src, delimiter="\t", header=True)  # type: ignore[misc]
    t1.mkdir()
    t2.mkdir()
    (t2 / "loser").write_text("x")
    vc.publish(t1, final)
    assert vc.publish(t2, final) == final
    assert not t2.exists()
    assert not (final / "loser").exists()


def test_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OVID2_VOCAB_PARQUET_CACHE", "0")
    src = _src(tmp_path)
    assert vc.build_target(src, delimiter="\t", header=True) is None
    assert vc.cached_parquet(src, delimiter="\t", header=True) is None


def test_unwritable_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    monkeypatch.setenv("OVID2_VOCAB_CACHE_DIR", str(ro / "nested"))
    try:
        assert vc.build_target(_src(tmp_path), delimiter="\t", header=True) is None
    finally:
        ro.chmod(0o755)


@pytest.mark.integration
def test_spark_shared_view_uses_cache(spark: object, tmp_path: Path) -> None:
    """First read builds the Parquet cache; second read scans Parquet."""
    from types import SimpleNamespace

    from ovid2.core.events import NullEventBus
    from ovid2.core.pipeline import _read_shared_view_spark

    src = _src(tmp_path, body="CONCEPT_ID\tCONCEPT_CODE\n1\tA\n2\tB\n")
    view = SimpleNamespace(source_path=src, delimiter="\t", header=True)
    df1 = _read_shared_view_spark(spark, view, NullEventBus())
    assert vc.cached_parquet(src, delimiter="\t", header=True) is not None
    df2 = _read_shared_view_spark(spark, view, NullEventBus())
    assert sorted(map(tuple, df1.collect())) == [("1", "A"), ("2", "B")]
    assert sorted(map(tuple, df2.collect())) == [("1", "A"), ("2", "B")]
    assert "parquet" in df2._jdf.queryExecution().optimizedPlan().toString().lower()
