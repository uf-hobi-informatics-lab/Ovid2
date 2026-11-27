"""Engine-dispatching column DSL for shared (engine-portable) code.

Shared fixers, mapper ops, analysis passes and formatter adapters were
written against ``pyspark.sql.functions``. Importing their names from
here instead keeps the call sites unchanged::

    from ovid2.core.engines.dsl import broadcast, col, lit, when
    from ovid2.core.engines import dsl as F

Each name resolves **at call time** to the active engine's function:

* Spark family (or no active engine): the real
  ``pyspark.sql.functions`` attribute — the Spark plan is byte-identical
  to a direct import.
* Any other engine: :meth:`BaseEngine.get_functions` (``DuckDBEngine`` /
  ``SnowparkEngine``). A name the engine does not implement raises
  :class:`~ovid2.core.engines.protocols.EngineOperationUnsupported`.

The active engine is set by :func:`use_engine` (``run_pipeline``, the
health runner, ``register_shared_tables``). When unset, the engine is
inferred from the arguments (a DuckDB column / frame selects DuckDB);
otherwise Spark is assumed.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

from ovid2.core.engines.protocols import EngineOperationUnsupported


class _EngineSlot:
    """A ``ContextVar`` that pickles as a fresh, empty slot.

    Snowpark ships UDF bodies (and, by value, the Ovid2 modules they close
    over — this one included) to Snowflake with ``cloudpickle``. A raw
    ``ContextVar`` cannot be pickled, and the host's active engine is
    meaningless inside the remote runtime, so the slot drops its value.
    """

    def __init__(self) -> None:
        self._var: contextvars.ContextVar[Any] = contextvars.ContextVar(
            "ovid2_active_engine", default=None
        )

    def get(self) -> Any:
        """Return the context-local engine (or ``None``)."""
        return self._var.get()

    def set(self, value: Any) -> contextvars.Token[Any]:
        """Set the context-local engine; return the reset token."""
        return self._var.set(value)

    def reset(self, token: contextvars.Token[Any]) -> None:
        """Restore the value captured by ``token``."""
        self._var.reset(token)

    def __reduce__(self) -> tuple[type[_EngineSlot], tuple[()]]:
        return (_EngineSlot, ())


class _ProcessSlot(list[Any]):
    """Process-wide fallback holder; pickles empty (see :class:`_EngineSlot`)."""

    def __reduce__(self) -> tuple[type[_ProcessSlot], tuple[list[None]]]:
        return (_ProcessSlot, ([None],))


_ACTIVE = _EngineSlot()

#: Process-wide fallback (a run uses exactly one engine per process).
_PROCESS_ENGINE: list[Any] = _ProcessSlot([None])


def set_active_engine(engine: Any) -> None:
    """Set the process-wide active engine (``None`` resets to Spark)."""
    _PROCESS_ENGINE[0] = engine


@contextlib.contextmanager
def use_engine(engine: Any) -> Iterator[None]:
    """Make ``engine`` the active engine for the enclosed block."""
    token = _ACTIVE.set(engine)
    previous = _PROCESS_ENGINE[0]
    _PROCESS_ENGINE[0] = engine
    try:
        yield
    finally:
        _ACTIVE.reset(token)
        _PROCESS_ENGINE[0] = previous


def active_engine() -> Any:
    """Return the active engine, or ``None`` (Spark assumed)."""
    engine = _ACTIVE.get()
    return engine if engine is not None else _PROCESS_ENGINE[0]


def _is_duck_arg(args: tuple[Any, ...]) -> bool:
    """Return ``True`` when any argument is a DuckDB-engine column / frame.

    Only the *type's module* is inspected: attribute access on a Spark
    ``Column`` builds a new column expression, so ``getattr`` probing is
    not safe here.
    """
    for arg in args:
        if isinstance(arg, (list, tuple)):
            if _is_duck_arg(tuple(arg)):
                return True
        elif type(arg).__module__.startswith("ovid2.core.engines._duck"):
            return True
    return False


def functions_for(engine: Any = None) -> Any:
    """Return the functions namespace for ``engine`` (or the active one)."""
    engine = engine if engine is not None else active_engine()
    if engine is None or getattr(engine, "session_is_spark", True):
        from pyspark.sql import functions as spark_functions

        return spark_functions
    return engine.get_functions()


def functions_and_family_for_frame(df: Any, engine: Any = None) -> tuple[Any, str]:
    """Return ``(functions, family)`` for running engine-portable code on ``df``.

    ``engine`` (or the active one) wins; otherwise the frame's type decides
    (DuckDB / Snowpark frame, else Spark). Lives here so algorithm modules
    never import a concrete engine to answer the question.
    """
    engine = engine if engine is not None else active_engine()
    if engine is not None:
        family = getattr(engine, "family", "spark")
        return engine.get_functions(), str(getattr(family, "value", family))
    module = type(df).__module__
    if module.startswith("ovid2.core.engines._duck"):
        from ovid2.core.engines.duckdb import _DuckFunctions

        return _DuckFunctions(), "duckdb"
    if module.startswith("snowflake.snowpark"):
        from ovid2.core.engines.snowpark import _SnowparkFunctions

        return _SnowparkFunctions(), "snowpark"
    from ovid2.core.engines.pyspark import spark_functions

    return spark_functions(), "spark"


def _resolve(name: str, args: tuple[Any, ...]) -> Any:
    engine = active_engine()
    if (engine is None or getattr(engine, "session_is_spark", True)) and _is_duck_arg(args):
        from ovid2.core.engines.duckdb import _DuckFunctions

        namespace: Any = _DuckFunctions()
    else:
        namespace = functions_for(engine)
    fn = getattr(namespace, name, None)
    if fn is None:
        type_name = getattr(engine, "type_name", "?")
        raise EngineOperationUnsupported(type_name, name, "not in this engine's functions DSL")
    return fn


def _dispatch(name: str) -> Any:
    def _call(*args: Any, **kwargs: Any) -> Any:
        return _resolve(name, args + tuple(kwargs.values()))(*args, **kwargs)

    _call.__name__ = name
    _call.__qualname__ = name
    _call.__doc__ = f"Engine-dispatching ``{name}`` (see :mod:`ovid2.core.engines.dsl`)."
    return _call


# Explicit names (so ``from ovid2.core.engines.dsl import col`` type-checks).
array = _dispatch("array")
broadcast = _dispatch("broadcast")
coalesce = _dispatch("coalesce")
col = _dispatch("col")
concat = _dispatch("concat")
concat_ws = _dispatch("concat_ws")
count = _dispatch("count")
countDistinct = _dispatch("countDistinct")  # noqa: N816 — PySpark name
create_map = _dispatch("create_map")
current_date = _dispatch("current_date")
current_timestamp = _dispatch("current_timestamp")
date_format = _dispatch("date_format")
datediff = _dispatch("datediff")
explode = _dispatch("explode")
expr = _dispatch("expr")
initcap = _dispatch("initcap")
input_file_name = _dispatch("input_file_name")
length = _dispatch("length")
lit = _dispatch("lit")
lower = _dispatch("lower")
max = _dispatch("max")
min = _dispatch("min")
monotonically_increasing_id = _dispatch("monotonically_increasing_id")
rand = _dispatch("rand")
regexp_extract = _dispatch("regexp_extract")
regexp_replace = _dispatch("regexp_replace")
round = _dispatch("round")
row_number = _dispatch("row_number")
sha2 = _dispatch("sha2")
split = _dispatch("split")
substring = _dispatch("substring")
sum = _dispatch("sum")
to_date = _dispatch("to_date")
to_timestamp = _dispatch("to_timestamp")
trim = _dispatch("trim")
upper = _dispatch("upper")
when = _dispatch("when")
year = _dispatch("year")


class _WindowProxy:
    """Engine-dispatching ``Window`` (``Window.partitionBy(...).orderBy(...)``).

    Resolves to ``pyspark.sql.Window`` on Spark-family engines and to the
    active engine's ``functions.Window`` otherwise (DuckDB).
    """

    @staticmethod
    def _target(args: tuple[Any, ...]) -> Any:
        engine = active_engine()
        if (engine is None or getattr(engine, "session_is_spark", True)) and not _is_duck_arg(args):
            from pyspark.sql import Window as SparkWindow

            return SparkWindow
        if engine is None or getattr(engine, "session_is_spark", True):
            from ovid2.core.engines.duckdb import _DuckFunctions

            return _DuckFunctions.Window
        window = getattr(engine.get_functions(), "Window", None)
        if window is None:
            raise EngineOperationUnsupported(
                str(getattr(engine, "type_name", "?")), "Window", "no window support"
            )
        return window

    def partitionBy(self, *cols: Any) -> Any:  # noqa: N802 — PySpark API
        """Start a window spec with partition keys."""
        return self._target(cols).partitionBy(*cols)

    def orderBy(self, *cols: Any) -> Any:  # noqa: N802 — PySpark API
        """Start a window spec with an ordering."""
        return self._target(cols).orderBy(*cols)

    def rowsBetween(self, start: int, end: int) -> Any:  # noqa: N802 — PySpark API
        """Start a window spec with a ROWS frame."""
        return self._target(()).rowsBetween(start, end)

    def rangeBetween(self, start: int, end: int) -> Any:  # noqa: N802 — PySpark API
        """Start a window spec with a RANGE frame."""
        return self._target(()).rangeBetween(start, end)

    #: Frame boundaries — identical values on every engine (Spark's).
    unboundedPreceding: int = -(1 << 63)  # noqa: N815 — PySpark API
    unboundedFollowing: int = (1 << 63) - 1  # noqa: N815 — PySpark API
    currentRow: int = 0  # noqa: N815 — PySpark API


#: Engine-dispatching ``Window`` entry point.
Window = _WindowProxy()


class _LazyUdf:
    """A UDF bound to whichever engine is active when it is *applied*.

    Legacy ports and shared helpers create UDFs at import time
    (``fmt_udf = udf(fn, StringType())``), before any engine exists.
    The real engine UDF is built on first call, per engine family, so
    the same module-level object works on every engine.
    """

    def __init__(self, fn: Any, return_type: Any, *, vectorised: bool) -> None:
        self.func = fn
        self.returnType = return_type
        self._vectorised = vectorised
        self._built: dict[str, Any] = {}
        self._deterministic = True
        self.__name__ = getattr(fn, "__name__", "udf")
        self.__doc__ = getattr(fn, "__doc__", None)

    def _for(self, args: tuple[Any, ...]) -> Any:
        engine = active_engine()
        spark = (engine is None or getattr(engine, "session_is_spark", True)) and not _is_duck_arg(
            args
        )
        key = "spark" if spark else str(getattr(engine, "type_name", "duckdb") or "duckdb")
        built = self._built.get(key)
        if built is None:
            if spark:
                from ovid2.core.engines.pyspark import spark_udf

                built = spark_udf(self.func, self.returnType, vectorised=self._vectorised)
            else:
                namespace = (
                    engine.get_functions()
                    if engine is not None and not _is_duck_arg(args)
                    else None
                )
                if namespace is None:
                    from ovid2.core.engines.duckdb import _DuckFunctions

                    namespace = _DuckFunctions()
                maker = namespace.pandas_udf if self._vectorised else namespace.udf
                built = maker(self.func, self.returnType)
            if not self._deterministic:
                mark = getattr(built, "asNondeterministic", None)
                built = mark() if callable(mark) else built
            self._built[key] = built
        return built

    def __call__(self, *cols: Any) -> Any:
        """Apply the UDF to ``cols`` on the active engine."""
        return self._for(cols)(*cols)

    def engine_udf(self) -> Any:
        """Return the active engine's UDF object (for ``session.udf.register``)."""
        return self._for(())

    def asNondeterministic(self) -> _LazyUdf:  # noqa: N802 — PySpark API
        """PySpark API: the engine UDF is built non-deterministic (never memoised)."""
        self._deterministic = False
        self._built.clear()
        return self


def udf(f: Any = None, returnType: Any = "string", **kwargs: Any) -> Any:  # noqa: N803
    """Engine-portable ``pyspark.sql.functions.udf`` (callable or decorator form)."""
    returnType = kwargs.pop("return_type", returnType)  # noqa: N806
    if f is None or not callable(f):
        rt = f if f is not None else returnType
        return lambda fn: _LazyUdf(fn, rt, vectorised=False)
    return _LazyUdf(f, returnType, vectorised=False)


def pandas_udf(f: Any = None, returnType: Any = None, functionType: Any = None) -> Any:  # noqa: N803
    """Engine-portable scalar ``pandas_udf`` (Series in, Series out)."""
    del functionType
    if f is None or not callable(f):
        rt = f if f is not None else (returnType or "string")
        return lambda fn: _LazyUdf(fn, rt, vectorised=True)
    return _LazyUdf(f, returnType or "string", vectorised=True)


class PandasUDFType:
    """``pyspark.sql.functions.PandasUDFType`` constants (scalar only is portable)."""

    SCALAR = 200
    SCALAR_ITER = 204
    GROUPED_MAP = 201
    GROUPED_AGG = 202


def __getattr__(name: str) -> Any:
    """Dispatch any other ``pyspark.sql.functions`` name the same way."""
    if name.startswith("__"):
        raise AttributeError(name)
    return _dispatch(name)


__all__ = [
    "PandasUDFType",
    "Window",
    "active_engine",
    "array",
    "broadcast",
    "coalesce",
    "col",
    "concat",
    "concat_ws",
    "count",
    "countDistinct",
    "create_map",
    "current_date",
    "current_timestamp",
    "date_format",
    "datediff",
    "explode",
    "expr",
    "functions_and_family_for_frame",
    "functions_for",
    "initcap",
    "input_file_name",
    "length",
    "lit",
    "lower",
    "max",
    "min",
    "monotonically_increasing_id",
    "pandas_udf",
    "rand",
    "regexp_extract",
    "regexp_replace",
    "round",
    "row_number",
    "set_active_engine",
    "sha2",
    "split",
    "substring",
    "sum",
    "to_date",
    "to_timestamp",
    "trim",
    "udf",
    "upper",
    "use_engine",
    "when",
    "year",
]
