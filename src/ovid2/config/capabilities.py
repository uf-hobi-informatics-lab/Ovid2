"""Engine capabilities: the closed vocabulary algorithms declare in ``requires``.

Tier-neutral (pure data) so the web tier (``jobs_svc.submit``), the CLI plan
check and the engines share it. Re-exported by
:mod:`ovid2.core.engines.protocols`.

Two layers (``docs/engines.md`` → *Capabilities*):

* **Features** — coarse behaviours whose support really varies between
  engines: ``window``, ``regex``, ``checkpoint``, ``broadcast_join``,
  ``rand_seeded`` (a seed gives reproducible draws),
  ``sample_with_replacement``, ``sha2_all_bits`` (SHA-224/384/512 as well
  as 256), ``map_from_columns`` (``create_map`` over non-literal columns),
  ``input_file_name`` (a real per-row source file path).
* **Functions** — ``fn:<name>`` (member ``FN_<NAME>``), one per column
  function in :data:`ovid2.core.engines.surface.FUNCTIONS` plus
  :data:`FUNCTION_EXTENSIONS`. Every engine implements every one
  (``test_engine_surface_complete.py``), so they never refuse a plan; they
  make declarations exact and are AST-checked against the DSL names each
  algorithm calls (``test_algorithm_requires_declared.py``).
  ``test_engine_capabilities_declared.py`` keeps the list equal to the
  surface.

Each engine declares :meth:`~ovid2.core.engines.base.BaseEngine.capabilities`
(mirrored web-safe in :data:`ENGINE_CAPABILITIES`); :mod:`ovid2.algorithms.plan`
refuses a run whose algorithms require a capability the engine lacks.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

__all__ = [
    "ENGINE_CAPABILITIES",
    "FEATURE_CAPABILITIES",
    "FUNCTION_CAPABILITIES",
    "FUNCTION_EXTENSIONS",
    "FUNCTION_PREFIX",
    "Capability",
    "engine_capabilities",
    "function_capability",
    "parse_capabilities",
]

#: Prefix of function-capability values (``fn:col``).
FUNCTION_PREFIX: str = "fn:"

#: Ovid2 column functions beyond the PySpark surface (on every engine).
FUNCTION_EXTENSIONS: tuple[str, ...] = ("stable_hash", "map_from_dict", "to_double")


class Capability(str, Enum):
    """Closed set of engine capabilities an algorithm may require (see module doc)."""

    # --- features -------------------------------------------------------
    WINDOW = "window"
    REGEX = "regex"
    CHECKPOINT = "checkpoint"
    BROADCAST_JOIN = "broadcast_join"
    RAND_SEEDED = "rand_seeded"
    SAMPLE_WITH_REPLACEMENT = "sample_with_replacement"
    SHA2_ALL_BITS = "sha2_all_bits"
    MAP_FROM_COLUMNS = "map_from_columns"
    INPUT_FILE_NAME = "input_file_name"

    # --- functions (surface.FUNCTIONS + extensions; pinned by contract) ---
    FN_COL = "fn:col"
    FN_COLUMN = "fn:column"
    FN_LIT = "fn:lit"
    FN_WHEN = "fn:when"
    FN_COALESCE = "fn:coalesce"
    FN_EXPR = "fn:expr"
    FN_NVL = "fn:nvl"
    FN_IFNULL = "fn:ifnull"
    FN_NULLIF = "fn:nullif"
    FN_ISNULL = "fn:isnull"
    FN_ISNAN = "fn:isnan"
    FN_NANVL = "fn:nanvl"
    FN_GREATEST = "fn:greatest"
    FN_LEAST = "fn:least"
    FN_BROADCAST = "fn:broadcast"
    FN_INPUT_FILE_NAME = "fn:input_file_name"
    FN_MONOTONICALLY_INCREASING_ID = "fn:monotonically_increasing_id"
    FN_RAND = "fn:rand"
    FN_UPPER = "fn:upper"
    FN_LOWER = "fn:lower"
    FN_INITCAP = "fn:initcap"
    FN_TRIM = "fn:trim"
    FN_LTRIM = "fn:ltrim"
    FN_RTRIM = "fn:rtrim"
    FN_LENGTH = "fn:length"
    FN_SUBSTRING = "fn:substring"
    FN_CONCAT = "fn:concat"
    FN_CONCAT_WS = "fn:concat_ws"
    FN_LPAD = "fn:lpad"
    FN_RPAD = "fn:rpad"
    FN_INSTR = "fn:instr"
    FN_LOCATE = "fn:locate"
    FN_REGEXP_REPLACE = "fn:regexp_replace"
    FN_REGEXP_EXTRACT = "fn:regexp_extract"
    FN_SPLIT = "fn:split"
    FN_TRANSLATE = "fn:translate"
    FN_REVERSE = "fn:reverse"
    FN_REPEAT = "fn:repeat"
    FN_FORMAT_STRING = "fn:format_string"
    FN_MD5 = "fn:md5"
    FN_SHA1 = "fn:sha1"
    FN_SHA2 = "fn:sha2"
    FN_HASH = "fn:hash"
    FN_XXHASH64 = "fn:xxhash64"
    FN_BASE64 = "fn:base64"
    FN_ASCII = "fn:ascii"
    FN_TO_CSV = "fn:to_csv"
    FN_ABS = "fn:abs"
    FN_ROUND = "fn:round"
    FN_BROUND = "fn:bround"
    FN_FLOOR = "fn:floor"
    FN_CEIL = "fn:ceil"
    FN_SQRT = "fn:sqrt"
    FN_POW = "fn:pow"
    FN_EXP = "fn:exp"
    FN_LOG = "fn:log"
    FN_SIGNUM = "fn:signum"
    FN_TO_DOUBLE = "fn:to_double"
    FN_TO_DATE = "fn:to_date"
    FN_TO_TIMESTAMP = "fn:to_timestamp"
    FN_DATE_FORMAT = "fn:date_format"
    FN_DATEDIFF = "fn:datediff"
    FN_DATE_ADD = "fn:date_add"
    FN_DATE_SUB = "fn:date_sub"
    FN_ADD_MONTHS = "fn:add_months"
    FN_MONTHS_BETWEEN = "fn:months_between"
    FN_CURRENT_DATE = "fn:current_date"
    FN_CURRENT_TIMESTAMP = "fn:current_timestamp"
    FN_YEAR = "fn:year"
    FN_MONTH = "fn:month"
    FN_DAYOFMONTH = "fn:dayofmonth"
    FN_DAYOFWEEK = "fn:dayofweek"
    FN_DAYOFYEAR = "fn:dayofyear"
    FN_QUARTER = "fn:quarter"
    FN_WEEKOFYEAR = "fn:weekofyear"
    FN_HOUR = "fn:hour"
    FN_MINUTE = "fn:minute"
    FN_SECOND = "fn:second"
    FN_LAST_DAY = "fn:last_day"
    FN_TRUNC = "fn:trunc"
    FN_DATE_TRUNC = "fn:date_trunc"
    FN_UNIX_TIMESTAMP = "fn:unix_timestamp"
    FN_FROM_UNIXTIME = "fn:from_unixtime"
    FN_FROM_UTC_TIMESTAMP = "fn:from_utc_timestamp"
    FN_TO_UTC_TIMESTAMP = "fn:to_utc_timestamp"
    FN_COUNT = "fn:count"
    FN_COUNTDISTINCT = "fn:countDistinct"
    FN_COUNT_DISTINCT = "fn:count_distinct"
    FN_SUM = "fn:sum"
    FN_SUMDISTINCT = "fn:sumDistinct"
    FN_AVG = "fn:avg"
    FN_MEAN = "fn:mean"
    FN_MIN = "fn:min"
    FN_MAX = "fn:max"
    FN_FIRST = "fn:first"
    FN_LAST = "fn:last"
    FN_STDDEV = "fn:stddev"
    FN_VARIANCE = "fn:variance"
    FN_APPROX_COUNT_DISTINCT = "fn:approx_count_distinct"
    FN_COLLECT_LIST = "fn:collect_list"
    FN_COLLECT_SET = "fn:collect_set"
    FN_BOOL_AND = "fn:bool_and"
    FN_BOOL_OR = "fn:bool_or"
    FN_ROW_NUMBER = "fn:row_number"
    FN_RANK = "fn:rank"
    FN_DENSE_RANK = "fn:dense_rank"
    FN_PERCENT_RANK = "fn:percent_rank"
    FN_CUME_DIST = "fn:cume_dist"
    FN_NTILE = "fn:ntile"
    FN_LAG = "fn:lag"
    FN_LEAD = "fn:lead"
    FN_ARRAY = "fn:array"
    FN_STRUCT = "fn:struct"
    FN_CREATE_MAP = "fn:create_map"
    FN_EXPLODE = "fn:explode"
    FN_EXPLODE_OUTER = "fn:explode_outer"
    FN_ARRAY_CONTAINS = "fn:array_contains"
    FN_ARRAY_JOIN = "fn:array_join"
    FN_SIZE = "fn:size"
    FN_SORT_ARRAY = "fn:sort_array"
    FN_ELEMENT_AT = "fn:element_at"
    FN_FLATTEN = "fn:flatten"
    FN_ARRAY_DISTINCT = "fn:array_distinct"
    FN_ASC = "fn:asc"
    FN_DESC = "fn:desc"
    FN_ASC_NULLS_FIRST = "fn:asc_nulls_first"
    FN_ASC_NULLS_LAST = "fn:asc_nulls_last"
    FN_DESC_NULLS_FIRST = "fn:desc_nulls_first"
    FN_DESC_NULLS_LAST = "fn:desc_nulls_last"
    FN_UDF = "fn:udf"
    FN_PANDAS_UDF = "fn:pandas_udf"
    FN_MAP_FROM_DICT = "fn:map_from_dict"
    FN_STABLE_HASH = "fn:stable_hash"


def function_capability(name: str) -> Capability:
    """Return the ``fn:<name>`` capability for column function ``name``."""
    return Capability(f"{FUNCTION_PREFIX}{name}")


def parse_capabilities(values: Iterable[object] | str | None) -> frozenset[Capability]:
    """Coerce declared ``requires`` values (members or strings) to capabilities.

    Raises:
        ValueError: On a value outside the closed set.
    """
    if values is None:
        return frozenset()
    items: Iterable[object] = (values,) if isinstance(values, str) else values
    return frozenset(Capability(str(getattr(v, "value", v))) for v in items)


#: Every function capability (``fn:*``).
FUNCTION_CAPABILITIES: frozenset[Capability] = frozenset(
    c for c in Capability if c.value.startswith(FUNCTION_PREFIX)
)

#: Every feature capability.
FEATURE_CAPABILITIES: frozenset[Capability] = frozenset(
    c for c in Capability if not c.value.startswith(FUNCTION_PREFIX)
)

_C = Capability

#: Web-safe mirror of each engine type's ``capabilities()`` (pinned to the
#: engines by ``tests/contract/test_engine_capabilities_declared.py``). Every
#: engine has every function; the features differ:
#:
#: * ``rand_seeded`` — DuckDB's ``random()`` takes no per-call seed and
#:   Snowpark's ``sample`` ignores one; the Spark family honours both.
#: * ``sample_with_replacement`` — DuckDB samples Bernoulli only.
#: * ``sha2_all_bits`` — DuckDB has SHA-256 only.
#: * ``map_from_columns`` — DuckDB and Snowpark build ``create_map`` from
#:   literal keys / values only.
#: * ``input_file_name`` — Snowpark frames carry no source file path
#:   (``""``); a Databricks-Connect session reads remote files, so a local
#:   path is not meaningful there.
ENGINE_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "pyspark": FUNCTION_CAPABILITIES | FEATURE_CAPABILITIES,
    "databricks_spark": FUNCTION_CAPABILITIES | (FEATURE_CAPABILITIES - {_C.INPUT_FILE_NAME}),
    "duckdb": FUNCTION_CAPABILITIES
    | (
        FEATURE_CAPABILITIES
        - {_C.RAND_SEEDED, _C.SAMPLE_WITH_REPLACEMENT, _C.SHA2_ALL_BITS, _C.MAP_FROM_COLUMNS}
    ),
    "snowpark": FUNCTION_CAPABILITIES
    | (FEATURE_CAPABILITIES - {_C.RAND_SEEDED, _C.MAP_FROM_COLUMNS, _C.INPUT_FILE_NAME}),
}


def engine_capabilities(engine_type: str) -> frozenset[Capability] | None:
    """Return the declared capabilities of ``engine_type`` (``None`` if unknown)."""
    return ENGINE_CAPABILITIES.get(engine_type)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.capabilities is a library module.")
