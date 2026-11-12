"""Resolve the active engine's functions DSL + family for a transform run.

Transform ops are engine-portable: they call ``ctx.functions`` and the
portable DataFrame API. The executor-side callers (the ``transform``
pipeline stage and the ``apply_transforms`` mapper bridge) need the
*active* engine's :class:`~ovid2.core.engines.protocols.EngineFunctions`
and family name rather than assuming Spark. The active engine is the one
bound by :func:`ovid2.core.engines.dsl.use_engine` for the run; when
unset (unit tests, ad-hoc use) the DataFrame's type decides.
"""

from __future__ import annotations

from typing import Any

__all__ = ["engine_context_for"]


def engine_context_for(df: Any, engine: Any = None) -> tuple[Any, str]:
    """Return ``(functions, family)`` for running a transform chain on ``df``.

    Args:
        df: The DataFrame the chain will run on.
        engine: Explicit engine (wins over the active one).

    Returns:
        The engine's ``EngineFunctions`` and its family value
        (``'spark'`` / ``'duckdb'`` / ``'snowpark'``).
    """
    from ovid2.core.engines.dsl import active_engine

    engine = engine if engine is not None else active_engine()
    if engine is not None:
        family = getattr(engine, "family", "spark")
        family_name = str(getattr(family, "value", family))
        return engine.get_functions(), family_name
    module = type(df).__module__
    if module.startswith("ovid2.core.engines._duck"):
        from ovid2.core.engines.duckdb import _DuckFunctions

        return _DuckFunctions(), "duckdb"
    if module.startswith("snowflake.snowpark"):
        from ovid2.core.engines.snowpark import _SnowparkFunctions

        return _SnowparkFunctions(), "snowpark"
    from ovid2.core.engines.pyspark import spark_functions

    return spark_functions(), "spark"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.transforms.runtime is a library module.")
