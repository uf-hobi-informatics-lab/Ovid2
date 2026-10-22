"""Session-agnostic compute protocols.

These protocols describe the subset of the PySpark DataFrame API that the
Ovid2 pipeline actually uses. They exist so that non-Spark backends
(Snowpark) can plug in without the pipeline having to import
``pyspark.sql.*`` directly.

The default implementation (``PySparkEngine``) returns a real
``pyspark.sql.SparkSession`` whose methods already satisfy
:class:`EngineSession` structurally. Non-Spark implementations wrap their
native session in an adapter that exposes the same surface.

Contract-test guardrail
-----------------------

``tests/contract/test_no_pyspark_outside_engine.py`` forbids
``import pyspark`` outside ``ovid2.core.engines.*``. New consumers of a
Spark method must either extend :class:`EngineSession` here or call
``engine.native()`` and declare their fixer / adapter as
``requires_family="spark"``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass


class EngineFamily(str, Enum):
    """Broad compute-family classification.

    Fixers, adapters, and pipeline code may declare a ``requires_family``
    marker that gates them to one family. This lets us ship Snowpark as a
    real engine without pretending every Spark-only fixer works under it.
    """

    SPARK = "spark"
    SNOWPARK = "snowpark"


@runtime_checkable
class EngineFunctions(Protocol):
    """Column-expression DSL exposed by an engine.

    All ``pyspark.sql.functions`` names used anywhere under ``src/ovid2/``
    are listed here. Non-Spark implementations either map to their native
    equivalent (``snowflake.snowpark.functions``) or raise
    :class:`EngineOperationUnsupported` for the ones that have no analog
    (``input_file_name`` on Snowpark, for example).
    """

    def col(self, name: str) -> Any: ...
    def lit(self, value: Any) -> Any: ...
    def when(self, cond: Any, value: Any) -> Any: ...
    def coalesce(self, *cols: Any) -> Any: ...
    def upper(self, c: Any) -> Any: ...
    def lower(self, c: Any) -> Any: ...
    def trim(self, c: Any) -> Any: ...
    def substring(self, c: Any, pos: int, length: int) -> Any: ...
    def regexp_replace(self, c: Any, pattern: str, replacement: str) -> Any: ...
    def regexp_extract(self, c: Any, pattern: str, idx: int) -> Any: ...
    def to_date(self, c: Any, fmt: str | None = None) -> Any: ...
    def to_timestamp(self, c: Any, fmt: str | None = None) -> Any: ...
    def date_format(self, c: Any, fmt: str) -> Any: ...
    def datediff(self, end: Any, start: Any) -> Any: ...
    def current_timestamp(self) -> Any: ...
    def count(self, c: Any) -> Any: ...
    def broadcast(self, df: Any) -> Any: ...
    def input_file_name(self) -> Any: ...
    def expr(self, sql_string: str) -> Any: ...
    # --- Portable extensions (E-portability phase 1) --------------------
    #
    # These names are consumed by engine-portable pipeline formatters
    # (``pcornet_to_omop`` first) that must run under either family.
    # Each has a native equivalent on both PySpark and Snowpark; the
    # divergent-idiom cases (``create_map[...]`` indexing, ``xxhash64``
    # bit-masking) are folded into portable helpers so pipeline code never
    # branches on the engine.
    def concat(self, *cols: Any) -> Any: ...
    def concat_ws(self, sep: str, *cols: Any) -> Any: ...
    def current_date(self) -> Any: ...
    def max(self, c: Any) -> Any: ...
    def map_from_dict(
        self, mapping: dict[str, Any], key_col: Any, default: Any = None
    ) -> Any: ...
    def stable_hash(self, *cols: Any) -> Any: ...
    def rand(self, seed: int | None = None) -> Any: ...


@runtime_checkable
class EngineSession(Protocol):
    """Session-agnostic query context.

    Structurally satisfied by ``pyspark.sql.SparkSession`` (Spark-family
    engines return the real thing). Snowpark returns an adapter.
    """

    def table(self, name: str) -> Any: ...
    def sql(self, query: str) -> Any: ...
    def createDataFrame(self, data: Any, schema: Any = None) -> Any: ...


class EngineOperationUnsupported(RuntimeError):
    """Raised by a non-Spark engine when the pipeline asks for a Spark-only op.

    Callers may catch this at the fixer / adapter boundary and surface it
    as a warning event (fixer skipped) or a hard error (adapter refuses to
    run). Never propagate silently.
    """

    def __init__(self, engine_type: str, operation: str, detail: str = "") -> None:
        message = f"Engine '{engine_type}' does not support operation '{operation}'"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.engine_type = engine_type
        self.operation = operation
        self.detail = detail


__all__ = [
    "EngineFamily",
    "EngineFunctions",
    "EngineOperationUnsupported",
    "EngineSession",
]
