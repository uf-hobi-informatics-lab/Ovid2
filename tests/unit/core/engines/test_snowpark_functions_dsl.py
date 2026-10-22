"""Unit tests for the portable :class:`EngineFunctions` DSL extensions on
the Snowpark engine (E-portability phase 1).

Snowpark is not installed on every host, so these tests stub
``snowflake.snowpark.functions`` with a recording double and assert that
:class:`_SnowparkFunctions` calls the correct native function names with
the correct argument order (the divergent-idiom cases:
``concat_ws`` separator-as-Column, ``stable_hash`` → ``abs(hash(...))``,
``rand`` → ``random``, ``map_from_dict`` → chained ``when/otherwise``).

The DataFrame-shape helpers (``union_by_name`` /``drop_duplicates`` /
``with_column``) are exercised against lightweight fake DataFrames.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


class _FakeCol:
    """A recording stand-in for a Snowpark ``Column``."""

    def __init__(self, repr_: str) -> None:
        self._repr = repr_
        self.when_calls: list[tuple[Any, Any]] = []
        self.otherwise_value: Any = "__unset__"

    def cast(self, t: str) -> _FakeCol:
        return _FakeCol(f"cast({self._repr},{t})")

    def __eq__(self, other: Any) -> _FakeCol:  # type: ignore[override]
        return _FakeCol(f"eq({self._repr},{other!r})")

    def when(self, cond: Any, value: Any) -> _FakeCol:
        self.when_calls.append((cond, value))
        return self

    def otherwise(self, value: Any) -> _FakeCol:
        self.otherwise_value = value
        return self

    def __repr__(self) -> str:
        return self._repr


class _FakeFunctions:
    """Recording double for ``snowflake.snowpark.functions``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _rec(self, name: str, *args: Any) -> _FakeCol:
        self.calls.append((name, args))
        return _FakeCol(f"{name}({','.join(repr(a) for a in args)})")

    def col(self, name: str) -> _FakeCol:
        return self._rec("col", name)

    def lit(self, value: Any) -> _FakeCol:
        return self._rec("lit", value)

    def when(self, cond: Any, value: Any) -> _FakeCol:
        c = _FakeCol(f"when({cond!r},{value!r})")
        c.when_calls.append((cond, value))
        return c

    def concat(self, *cols: Any) -> _FakeCol:
        return self._rec("concat", *cols)

    def concat_ws(self, *cols: Any) -> _FakeCol:
        return self._rec("concat_ws", *cols)

    def current_date(self) -> _FakeCol:
        return self._rec("current_date")

    def max(self, c: Any) -> _FakeCol:
        return self._rec("max", c)

    def coalesce(self, *cols: Any) -> _FakeCol:
        return self._rec("coalesce", *cols)

    def abs(self, c: Any) -> _FakeCol:
        return self._rec("abs", c)

    def hash(self, *cols: Any) -> _FakeCol:
        return self._rec("hash", *cols)

    def random(self, *args: Any) -> _FakeCol:
        return self._rec("random", *args)

    def regexp_extract(self, *args: Any) -> _FakeCol:
        return self._rec("regexp_extract", *args)

    def builtin(self, name: str) -> Any:
        """Emulate ``F.builtin('NAME')`` returning a callable.

        The returned callable records ``NAME(*args)`` so tests can
        assert both the builtin name and its arguments.
        """

        def _call(*args: Any) -> _FakeCol:
            return self._rec(name, *args)

        return _call


@pytest.fixture()
def snowpark_functions(monkeypatch: pytest.MonkeyPatch) -> _FakeFunctions:
    """Install a stub ``snowflake.snowpark.functions`` and return the double."""
    fake = _FakeFunctions()
    functions_mod = types.ModuleType("snowflake.snowpark.functions")
    for attr in dir(fake):
        if not attr.startswith("_"):
            setattr(functions_mod, attr, getattr(fake, attr))

    snowpark_mod = types.ModuleType("snowflake.snowpark")
    snowpark_mod.functions = functions_mod  # type: ignore[attr-defined]
    snowflake_pkg = types.ModuleType("snowflake")
    snowflake_pkg.snowpark = snowpark_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "snowflake", snowflake_pkg)
    monkeypatch.setitem(sys.modules, "snowflake.snowpark", snowpark_mod)
    monkeypatch.setitem(sys.modules, "snowflake.snowpark.functions", functions_mod)
    return fake


def _make_functions() -> Any:
    from ovid2.core.engines.snowpark import _SnowparkFunctions

    return _SnowparkFunctions()


@pytest.mark.unit
def test_concat_maps_to_native_concat(snowpark_functions: _FakeFunctions) -> None:
    fns = _make_functions()
    fns.concat(fns.col("x"), fns.col("y"))
    assert any(name == "concat" for name, _ in snowpark_functions.calls)


@pytest.mark.unit
def test_concat_ws_passes_separator_as_lit_first(
    snowpark_functions: _FakeFunctions,
) -> None:
    """Snowpark's concat_ws takes the separator as the first (Column) arg."""
    fns = _make_functions()
    fns.concat_ws("-", fns.col("x"), fns.col("y"))
    concat_ws_calls = [c for c in snowpark_functions.calls if c[0] == "concat_ws"]
    assert len(concat_ws_calls) == 1
    first_arg = concat_ws_calls[0][1][0]
    # First arg is a lit('-') Column, not the bare string.
    assert repr(first_arg) == "lit('-')"


@pytest.mark.unit
def test_current_date_maps_to_native(snowpark_functions: _FakeFunctions) -> None:
    fns = _make_functions()
    fns.current_date()
    assert any(name == "current_date" for name, _ in snowpark_functions.calls)


@pytest.mark.unit
def test_max_maps_to_native(snowpark_functions: _FakeFunctions) -> None:
    fns = _make_functions()
    fns.max(fns.col("n"))
    assert any(name == "max" for name, _ in snowpark_functions.calls)


@pytest.mark.unit
def test_stable_hash_uses_abs_hash_and_casts_string(
    snowpark_functions: _FakeFunctions,
) -> None:
    fns = _make_functions()
    result = fns.stable_hash(fns.col("p"), fns.col("e"))
    names = [name for name, _ in snowpark_functions.calls]
    assert "hash" in names
    assert "abs" in names
    # Result is cast to string.
    assert "string" in repr(result)


@pytest.mark.unit
def test_map_from_dict_builds_chained_when_otherwise(
    snowpark_functions: _FakeFunctions,
) -> None:
    fns = _make_functions()
    result = fns.map_from_dict({"M": "8507", "F": "8532"}, fns.col("sex"), default="0")
    # The first .when is seeded by F.when (records call #1); the second
    # mapping key chains a further .when onto the same column (call #2);
    # the default lands in otherwise.
    assert isinstance(result, _FakeCol)
    assert result.otherwise_value == "0"
    assert len(result.when_calls) == 2


@pytest.mark.unit
def test_map_from_dict_empty_returns_lit_default(
    snowpark_functions: _FakeFunctions,
) -> None:
    fns = _make_functions()
    result = fns.map_from_dict({}, fns.col("k"), default="fallback")
    assert repr(result) == "lit('fallback')"


@pytest.mark.unit
def test_rand_maps_to_random(snowpark_functions: _FakeFunctions) -> None:
    fns = _make_functions()
    fns.rand()
    fns.rand(seed=7)
    random_calls = [c for c in snowpark_functions.calls if c[0] == "random"]
    assert len(random_calls) == 2
    # Seeded call forwards the seed positionally.
    assert random_calls[1][1] == (7,)


# ---- DataFrame-shape helpers on SnowparkEngine ---------------------------


class _FakeDF:
    """Minimal fake Snowpark DataFrame recording snake_case method calls."""

    def __init__(self, columns: list[str]) -> None:
        self.columns = columns
        self.union_by_name_arg: Any = None
        self.drop_duplicates_arg: Any = "__unset__"
        self.with_column_calls: list[tuple[str, Any]] = []

    def union_by_name(self, other: Any) -> _FakeDF:
        self.union_by_name_arg = other
        return self

    def drop_duplicates(self, cols: Any = "__unset__") -> _FakeDF:
        self.drop_duplicates_arg = cols
        return self

    def with_column(self, name: str, col: Any) -> _FakeDF:
        self.with_column_calls.append((name, col))
        self.columns = self.columns + [name]
        return self


@pytest.mark.unit
def test_snowpark_drop_duplicates_uses_snake_case() -> None:
    from ovid2.core.engines.snowpark import SnowparkEngine

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    df = _FakeDF(["id", "v"])
    engine.drop_duplicates(df, ["id"])
    assert df.drop_duplicates_arg == ["id"]

    df2 = _FakeDF(["id"])
    engine.drop_duplicates(df2)
    assert df2.drop_duplicates_arg == "__unset__"


@pytest.mark.unit
def test_snowpark_with_column_uses_snake_case() -> None:
    from ovid2.core.engines.snowpark import SnowparkEngine

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    df = _FakeDF(["n"])
    engine.with_column(df, "doubled", "expr")
    assert df.with_column_calls == [("doubled", "expr")]


@pytest.mark.unit
def test_snowpark_union_by_name_no_missing_columns() -> None:
    from ovid2.core.engines.snowpark import SnowparkEngine

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    left = _FakeDF(["id", "x"])
    right = _FakeDF(["id", "x"])
    engine.union_by_name(left, right, allow_missing_columns=False)
    assert left.union_by_name_arg is right


@pytest.mark.unit
def test_snowpark_union_by_name_aligns_missing_columns(
    snowpark_functions: _FakeFunctions,
) -> None:
    from ovid2.core.engines.snowpark import SnowparkEngine

    engine = SnowparkEngine(args={"account": "a", "user": "u"}, secrets={"password": "p"})
    left = _FakeDF(["id", "x"])
    right = _FakeDF(["id", "y"])
    engine.union_by_name(left, right, allow_missing_columns=True)
    # left gained 'y', right gained 'x' as NULL literals before union.
    assert ("y", left.with_column_calls[0][1]) == left.with_column_calls[0]
    assert any(name == "y" for name, _ in left.with_column_calls)
    assert any(name == "x" for name, _ in right.with_column_calls)


# ---------------------------------------------------------------------------
# Java/Spark → Snowflake datetime-format translation
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("java_fmt", "expected"),
    [
        ("yyyy", "YYYY"),
        ("MM", "MM"),
        ("dd", "DD"),
        ("yyyy-MM-dd", "YYYY-MM-DD"),
        # The bug the Snowpark person E2E surfaced: lowercase mm (minutes)
        # must NOT collapse to MM (months); HH -> HH24, ss -> SS.
        ("yyyy-MM-dd HH:mm:ss", "YYYY-MM-DD HH24:MI:SS"),
        # Literal digits (the 00:00:00 sentinel) pass through untouched.
        ("yyyy-MM-dd 00:00:00", "YYYY-MM-DD 00:00:00"),
        ("yy", "YY"),
        ("hh", "HH12"),
        ("SSS", "FF3"),
    ],
)
def test_java_to_snowflake_format(java_fmt: str, expected: str) -> None:
    from ovid2.core.engines.snowpark import _java_to_snowflake_format

    assert _java_to_snowflake_format(java_fmt) == expected


# ---------------------------------------------------------------------------
# to_date / to_timestamp — TRY_TO_* + VARCHAR cast (null-on-failure +
# tolerate native DATE/TIMESTAMP inputs from a Snowflake source address)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_date_uses_try_to_date_with_varchar_cast(
    snowpark_functions: _FakeFunctions,
) -> None:
    """``to_date(col, fmt)`` maps to ``TRY_TO_DATE(col::string, fmt)``.

    ``TRY_TO_DATE`` preserves Spark's null-on-mismatch semantics (the
    person formatter's coalesce fallback relies on it); the ``::string``
    cast stops ``TO_DATE(<native date>, fmt)`` raising "too many
    arguments" when the source is a Snowflake DATE column.
    """
    fns = _make_functions()
    fns.to_date(fns.col("birth_date"), "yyyy-MM-dd")
    names = [name for name, _ in snowpark_functions.calls]
    assert "TRY_TO_DATE" in names
    # Never the strict two-arg TO_DATE.
    assert "to_date" not in names
    # The input column was cast to string before the builtin ran.
    try_call = next(c for c in snowpark_functions.calls if c[0] == "TRY_TO_DATE")
    first_arg = try_call[1][0]
    assert "cast(" in repr(first_arg) and "string" in repr(first_arg)
    # The format was translated and passed as a lit.
    second_arg = try_call[1][1]
    assert repr(second_arg) == "lit('YYYY-MM-DD')"


@pytest.mark.unit
def test_to_date_no_format_still_casts_and_tries(
    snowpark_functions: _FakeFunctions,
) -> None:
    fns = _make_functions()
    fns.to_date(fns.col("d"))
    try_call = next(c for c in snowpark_functions.calls if c[0] == "TRY_TO_DATE")
    assert len(try_call[1]) == 1
    assert "cast(" in repr(try_call[1][0])


@pytest.mark.unit
def test_to_timestamp_uses_try_to_timestamp_with_varchar_cast(
    snowpark_functions: _FakeFunctions,
) -> None:
    fns = _make_functions()
    fns.to_timestamp(fns.col("ts"), "yyyy-MM-dd HH:mm:ss")
    names = [name for name, _ in snowpark_functions.calls]
    assert "TRY_TO_TIMESTAMP" in names
    assert "to_timestamp" not in names
    try_call = next(c for c in snowpark_functions.calls if c[0] == "TRY_TO_TIMESTAMP")
    assert "cast(" in repr(try_call[1][0])
    assert repr(try_call[1][1]) == "lit('YYYY-MM-DD HH24:MI:SS')"


@pytest.mark.unit
def test_regexp_extract_uses_native_group_semantics(
    snowpark_functions: _FakeFunctions,
) -> None:
    """Delegates to Snowpark's Spark-compatible ``regexp_extract``.

    Regression: the old ``regexp_substr(c, pattern, 1, idx)`` raised
    ``TypeError`` (plain ints) and treated ``idx`` as an occurrence.
    """
    fns = _make_functions()
    col = fns.col("x")
    fns.regexp_extract(col, r"(\d+)-(\d+)", 2)
    assert ("regexp_extract", (col, r"(\d+)-(\d+)", 2)) in snowpark_functions.calls


@pytest.mark.unit
def test_regexp_extract_builds_on_real_snowpark() -> None:
    """Against the real SDK the expression builds (no ``TypeError``)."""
    pytest.importorskip("snowflake.snowpark")
    from snowflake.snowpark.functions import col

    from ovid2.core.engines.snowpark import _SnowparkFunctions

    expr = _SnowparkFunctions().regexp_extract(col("X"), r"a(b)", 1)
    assert type(expr).__name__ == "Column"
