"""Contract: every engine implements the whole portable surface (engine universality).

:mod:`ovid2.core.engines.surface` lists the PySpark-shaped operations
shared and generated code may use. Every engine in
:data:`~ovid2.core.engines.registry.ENGINE_TYPE_MAP` must implement every
entry, so a pipeline never needs a "Spark-only" escape hatch:

* functions — on the engine's ``get_functions()`` namespace (checked on
  the class, no session is opened);
* DataFrame / Column / GroupedData / Window / Session / Reader methods —
  on the engine's frame / column / … classes (:data:`_ENGINE_CLASSES`).

The second test keeps the list honest: it rescans the code that runs on
engines (shared transformations, mappers, formatters, transforms, the
legacy runtime, every pipeline package, the vendored legacy fix scripts)
for ``pyspark.sql.functions`` names and fails when a name is used that is
not in :data:`~ovid2.core.engines.surface.FUNCTIONS` — add it there *and*
to every engine.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from ovid2.core.engines import surface
from ovid2.core.engines.registry import ENGINE_TYPE_MAP

pytestmark = pytest.mark.contract

#: Engine type → (functions, frame, column, grouped, window spec + entry, session, reader) classes.
_ENGINE_CLASSES: dict[str, dict[str, str]] = {
    "duckdb": {
        "functions": "ovid2.core.engines.duckdb._DuckFunctions",
        "frame": "ovid2.core.engines._duck_frame.DuckFrame",
        "column": "ovid2.core.engines._duck_column.DuckColumn",
        "grouped": "ovid2.core.engines._duck_frame.DuckGroupedData",
        "window": "ovid2.core.engines._duck_frame.DuckWindow",
        "window_spec": "ovid2.core.engines._duck_frame.DuckWindowSpec",
        "session": "ovid2.core.engines.duckdb.DuckDBEngineSession",
        "reader": "ovid2.core.engines.duckdb._DuckReader",
    },
    "snowpark": {
        "functions": "ovid2.core.engines._snow_frame.SnowFunctions",
        "frame": "ovid2.core.engines._snow_frame.SnowFrame",
        "column": "ovid2.core.engines._snow_frame.SnowColumn",
        "grouped": "ovid2.core.engines._snow_frame._SnowGrouped",
        "window": "ovid2.core.engines._snow_frame.SnowWindow",
        "window_spec": "ovid2.core.engines._snow_frame.SnowWindowSpec",
        "session": "ovid2.core.engines.snowpark.SnowparkEngineSession",
        "reader": "ovid2.core.engines.snowpark._SnowparkLocalReader",
    },
    # Spark engines *are* the reference API; their functions namespace is
    # checked against ``pyspark.sql.functions`` directly.
    "pyspark": {"functions": "ovid2.core.engines.pyspark._SparkFunctions"},
    "databricks_spark": {"functions": "ovid2.core.engines.pyspark._SparkFunctions"},
}


def _load(path: str) -> type:
    module, _, name = path.rpartition(".")
    return getattr(importlib.import_module(module), name)  # type: ignore[no-any-return]


def test_every_registered_engine_is_covered() -> None:
    assert set(ENGINE_TYPE_MAP) == set(_ENGINE_CLASSES), (
        "a new engine must declare its portable-surface classes here and implement "
        "every entry of ovid2.core.engines.surface"
    )


def _missing(cls: type, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if not hasattr(cls, n)]


#: Ovid2 extensions that exist only on the adapters (not in ``pyspark.sql.functions``).
_OVID2_EXTENSIONS: tuple[str, ...] = ("map_from_dict", "stable_hash", "to_double")


@pytest.mark.parametrize("engine_type", sorted(_ENGINE_CLASSES))
def test_engine_implements_portable_surface(engine_type: str) -> None:
    classes = _ENGINE_CLASSES[engine_type]
    missing: dict[str, list[str]] = {}
    if engine_type in ("pyspark", "databricks_spark"):
        pytest.importorskip("pyspark")
        import pyspark.sql.functions as spark_functions

        fns = _load(classes["functions"])
        missing["functions"] = [
            n
            for n in surface.FUNCTIONS
            if n not in _OVID2_EXTENSIONS and not hasattr(spark_functions, n)
        ] + [n for n in (*_OVID2_EXTENSIONS, "udf", "pandas_udf") if not hasattr(fns, n)]
    else:
        fns = _load(classes["functions"])
        missing["functions"] = _missing(fns, surface.FUNCTIONS)
        missing["frame"] = _missing(_load(classes["frame"]), surface.DATAFRAME_METHODS)
        missing["column"] = _missing(_load(classes["column"]), surface.COLUMN_METHODS)
        missing["grouped"] = _missing(_load(classes["grouped"]), surface.GROUPED_METHODS)
        win, spec = _load(classes["window"]), _load(classes["window_spec"])
        missing["window"] = [
            n for n in surface.WINDOW_METHODS if not hasattr(win, n) and not hasattr(spec, n)
        ]
        missing["session"] = _missing(_load(classes["session"]), surface.SESSION_METHODS)
        missing["reader"] = _missing(_load(classes["reader"]), surface.READER_METHODS)
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, f"engine {engine_type!r} lacks portable operations: {missing}"


#: Code that runs on engines (repo-relative roots).
_SCANNED: tuple[str, ...] = (
    "src/ovid2/transformations",
    "src/ovid2/mappers",
    "src/ovid2/formatters",
    "src/ovid2/transforms",
    "src/ovid2/legacy",
    "pipelines/ovid2_pipelines",
    "pipelines/legacy_ovid_vendor",
)

_FUNCTION_MODULES = (
    "pyspark.sql.functions",
    "ovid2.core.engines.dsl",
    "ovid2.core.engines.sparkapi.sql.functions",
)

#: Module aliases code uses for the functions namespace (``F.col``).
_ALIASES = ("F", "sf", "dsl", "fns", "psf")

#: Non-function names a ``from … import *`` binds (types, constants).
_NOT_FUNCTIONS = frozenset({"PandasUDFType", "Window"})


def _used_function_names(py: Path, known: frozenset[str]) -> set[str]:
    try:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
    except SyntaxError:
        return set()
    star = any(
        isinstance(n, ast.ImportFrom)
        and n.module in _FUNCTION_MODULES
        and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )
    shadowed: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            shadowed.add(n.name)
        if isinstance(n, (ast.FunctionDef, ast.Lambda)):
            shadowed |= {a.arg for a in n.args.args}
        if isinstance(n, ast.Assign):
            shadowed |= {t.id for t in n.targets if isinstance(t, ast.Name)}
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _FUNCTION_MODULES:
            used |= {a.name for a in node.names if a.name != "*" and a.name in known}
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _ALIASES
            and node.attr in known
        ):
            used.add(node.attr)
        elif (
            star
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in known
            and node.func.id not in shadowed
        ):
            used.add(node.func.id)
    return used - _NOT_FUNCTIONS


def test_used_functions_are_in_the_portable_surface(repo_root: Path) -> None:
    import warnings

    known = frozenset(surface.SPARK_FUNCTION_NAMES) | frozenset(surface.FUNCTIONS)
    portable = frozenset(surface.FUNCTIONS)
    offenders: dict[str, set[str]] = {}
    for root in _SCANNED:
        for py in sorted((repo_root / root).rglob("*.py")):
            if "__pycache__" in py.parts or "_superseded" in py.parts:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                extra = _used_function_names(py, known) - portable
            if extra:
                offenders[str(py.relative_to(repo_root))] = extra
    assert not offenders, (
        "Functions used by engine-run code but missing from ovid2.core.engines.surface.FUNCTIONS "
        "(add them there and implement them on every engine):\n"
        + "\n".join(f"  {k}: {sorted(v)}" for k, v in sorted(offenders.items()))
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run through pytest.")
