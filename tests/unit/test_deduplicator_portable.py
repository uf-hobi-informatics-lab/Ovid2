"""Unit tests for the engine-portable (Snowpark) path of
:class:`ovid2.transformations.deduplicators.base.Deduplicator`.

Salting is a Spark-only skew mitigation, so under a non-Spark engine the
deduplicator falls back to a single-stage ``drop_duplicates`` on the
composite key while preserving null-key passthrough. These tests drive
that path with a self-contained fake engine + DataFrame + functions so
they run without a JVM or a Snowflake session.
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.transformations.deduplicators.base import Deduplicator


class _FakeCol:
    """Minimal recording Column supporting the predicate operators used."""

    def __init__(self, repr_: str) -> None:
        self._repr = repr_

    def isNotNull(self) -> "_FakeCol":  # noqa: N802 — mirror pyspark API
        return _FakeCol(f"isNotNull({self._repr})")

    def __eq__(self, other: Any) -> "_FakeCol":  # type: ignore[override]
        return _FakeCol(f"eq({self._repr},{other!r})")

    def __ne__(self, other: Any) -> "_FakeCol":  # type: ignore[override]
        return _FakeCol(f"ne({self._repr},{other!r})")

    def __and__(self, other: Any) -> "_FakeCol":
        return _FakeCol(f"and({self._repr},{other!r})")

    def __invert__(self) -> "_FakeCol":
        return _FakeCol(f"not({self._repr})")

    def __repr__(self) -> str:
        return self._repr


class _FakeFunctions:
    def col(self, name: str) -> _FakeCol:
        return _FakeCol(f"col({name})")

    def lit(self, value: Any) -> _FakeCol:
        return _FakeCol(f"lit({value!r})")


class _FakeDF:
    """Records filter/drop_duplicates/union operations without executing."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.filters: list[Any] = []

    def filter(self, predicate: Any) -> "_FakeDF":
        child = _FakeDF(f"{self.tag}.filter({predicate!r})")
        return child


class _FakeSnowparkEngine:
    """Non-Spark engine exposing just the portable helpers the dedup uses."""

    type_name = "snowpark"
    session_is_spark = False

    def __init__(self) -> None:
        self.drop_calls: list[tuple[str, list[str] | None]] = []
        self.union_calls: list[tuple[str, str]] = []

    def get_functions(self) -> _FakeFunctions:
        return _FakeFunctions()

    def drop_duplicates(self, df: _FakeDF, cols: list[str] | None = None) -> _FakeDF:
        self.drop_calls.append((df.tag, cols))
        return _FakeDF(f"dropdup({df.tag},{cols})")

    def union_by_name(
        self, left: _FakeDF, right: _FakeDF, *, allow_missing_columns: bool = False
    ) -> _FakeDF:
        self.union_calls.append((left.tag, right.tag))
        return _FakeDF(f"union({left.tag},{right.tag})")


@pytest.mark.unit
def test_portable_single_key_drops_and_unions() -> None:
    """Non-Spark path: drop_duplicates on the key + union the null-key rows."""
    engine = _FakeSnowparkEngine()
    dedup = Deduplicator("demographic", engine=engine)
    assert dedup.key_fields == ["PATID"]

    out = dedup.deduplicate(_FakeDF("src"))

    # One drop_duplicates on the composite key, one union with the
    # passthrough (null-key) rows — no salt column, no two-stage dedup.
    assert len(engine.drop_calls) == 1
    assert engine.drop_calls[0][1] == ["PATID"]
    assert len(engine.union_calls) == 1
    assert out.tag.startswith("union(")


@pytest.mark.unit
def test_portable_no_key_fields_plain_drop() -> None:
    """Empty composite key → a plain drop_duplicates, no union."""
    engine = _FakeSnowparkEngine()
    dedup = Deduplicator("x", key_fields=[], engine=engine)

    dedup.deduplicate(_FakeDF("src"))

    assert engine.drop_calls == [("src", None)]
    assert engine.union_calls == []


@pytest.mark.unit
def test_portable_null_passthrough_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With passthrough off, a single drop_duplicates and no union runs."""
    import ovid2.transformations.deduplicators.base as mod

    monkeypatch.setattr(mod, "DEDUP_NULL_KEY_PASSTHROUGH", False)
    engine = _FakeSnowparkEngine()
    dedup = Deduplicator("diagnosis", engine=engine)

    dedup.deduplicate(_FakeDF("src"))

    assert len(engine.drop_calls) == 1
    assert engine.drop_calls[0][1] == dedup.key_fields
    assert engine.union_calls == []


@pytest.mark.unit
def test_spark_engine_uses_spark_path() -> None:
    """An engine with session_is_spark=True routes to the PySpark branch."""

    class _SparkishEngine:
        type_name = "pyspark"
        session_is_spark = True

    dedup = Deduplicator("demographic", engine=_SparkishEngine())
    assert dedup._is_spark is True  # noqa: SLF001


@pytest.mark.unit
def test_no_engine_defaults_to_spark() -> None:
    """No engine attached is treated as Spark for backwards compatibility."""
    dedup = Deduplicator("demographic")
    assert dedup._is_spark is True  # noqa: SLF001
