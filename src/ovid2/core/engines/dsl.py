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

_ACTIVE: contextvars.ContextVar[Any] = contextvars.ContextVar("ovid2_active_engine", default=None)

#: Process-wide fallback (a run uses exactly one engine per process).
_PROCESS_ENGINE: list[Any] = [None]


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


#: Engine-dispatching ``Window`` entry point.
Window = _WindowProxy()


def __getattr__(name: str) -> Any:
    """Dispatch any other ``pyspark.sql.functions`` name the same way."""
    if name.startswith("__"):
        raise AttributeError(name)
    return _dispatch(name)


__all__ = [
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
    "functions_for",
    "initcap",
    "input_file_name",
    "length",
    "lit",
    "lower",
    "max",
    "min",
    "monotonically_increasing_id",
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
    "upper",
    "use_engine",
    "when",
    "year",
]
