---
title: Portable engine code
summary: Write fixers, mapper ops, transforms and adapters once and run them on Spark, Snowpark and DuckDB.
audience: [developer]
---

Shared algorithm code (fixers, mapper ops, transforms, adapters, health
operators) must run on **every** engine. The rules:

## Algorithms take everything from their context

An algorithm (anything under `formatters/`, `mappers/`, `transforms/`,
`transformations/` or `algorithms/`) imports only the engine-neutral API —
`ovid2.core.engines.dsl` and `protocols` — and uses what its context hands
it: the engine, the functions DSL, a resolved source handle
(`AdapterContext.source`, with capability flags such as `is_local_path`) or
a reader callable. It never imports `pyspark`, `snowflake`, `duckdb`, a
concrete engine module, `ovid2.drivers` or `ovid2.connections` — not even
lazily inside a function (`test_algorithms_engine_and_storage_free.py`).

Pipeline code finds algorithms through one facade,
`ovid2.algorithms.registry.AlgorithmRegistry`, the counterpart of the
driver and engine registries:

```python
from ovid2.algorithms.registry import registry

fixer = registry.get("fixer", "patid_orphans")  # kind, name
df = fixer.apply(df, context)
registry.describe("transform", "to_upper")  # summary, requires, …
```

The facade delegates to each kind's own registry (`ADAPTERS`, `MAP_OPS`,
`COMMON_FIXES`, `TRANSFORMS`, the YAML health checks), so you still
*register* a new op where its kind lives. Code outside those registries
must not import them directly (`test_algorithm_registry_complete.py`).

## Declare what you need from the engine

Every algorithm declares the engine capabilities it uses:

```python
from typing import ClassVar

from ovid2.config.capabilities import Capability


class StripDigits(BaseTransform):
    REQUIRES: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.FN_COL, Capability.FN_REGEXP_REPLACE, Capability.REGEX}
    )
```

Health operators pass `requires=` to `@operator(...)`. List every DSL
function you call (`fn:*`) and any *feature* (`window`, `regex`,
`rand_seeded`, …); `test_algorithm_requires_declared.py` AST-checks the list
against your code. Feature capabilities are also mirrored in the web-safe
`config/algorithm_requirements.py`, which the plan-time check reads. The full
list and the per-engine table are in `docs/engines.md` → *Capabilities*.

## Import columns from the DSL

```python
# Yes
from ovid2.core.engines.dsl import col, lit, when, coalesce, Window

# No: fails test_no_runtime_pyspark_in_shared.py
from pyspark.sql import functions as F
```

`ovid2.core.engines.dsl` resolves each name to the active engine's
implementation **when it is called**. Alternatively, use
`context.functions`, the engine's `EngineFunctions` object.

`pyspark` may still be imported under `TYPE_CHECKING`, for annotations.

## Use engine helpers for shape changes

| Instead of | Use |
| --- | --- |
| `df1.unionByName(df2, allowMissingColumns=True)` | `engine.union_by_name(df1, df2, allow_missing_columns=True)` |
| `df.dropDuplicates(keys)` | `engine.drop_duplicates(df, keys)` |
| `df.persist()` / checkpoints | `engine.checkpoint(df, name)` |
| building a typed empty frame | `engine.create_typed_dataframe(…)` |

## SQL strings

Spark SQL in projections, `selectExpr` and `expr` is transpiled for DuckDB
by `sqlglot`, and Java date patterns are converted with LEGACY-parser
parity. Keep expressions to standard Spark SQL functions.

## Every engine runs everything

There is no Spark-only code. Every pipeline, formatter, transform, fixer,
mapper op and health check runs on every engine, and nothing can declare a
required engine family (`test_no_engine_family_gates.py`). To keep it that
way:

- **Use the DSL for everything column-level**, including `Window`
  (`rowsBetween`, `rangeBetween`, `unboundedPreceding`, …), `udf` and
  `pandas_udf`. Python UDFs are portable: Spark and DuckDB run them
  locally; Snowpark registers them as temporary Snowflake Python UDFs.
- **Stay on the portable surface.** `core/engines/surface.py` lists every
  function and DataFrame / Column / GroupedData / Window / Session /
  Reader method an engine must provide. A call outside it fails
  `test_engine_surface_complete.py` until it is added there **and**
  implemented by every engine.
- **Pipeline packages never import `pyspark`.** Generated `legacy_ovid`
  ports import `ovid2.core.engines.sparkapi`, a portable stand-in; the
  legacy migrator rewrites their imports.
- **Avoid `engine.native()`.** It returns the raw session and is not gated,
  but code that uses it only works on the engines it was written for.
  Prefer adding the missing operation to the surface.

## Semantic parity to keep in mind

- Surrogate ids from `stable_hash` differ between engines.
- Ties broken arbitrarily (which duplicate dedup keeps, which of several
  equally valid `Maps to` concepts wins) may differ.
- `monotonically_increasing_id`-style ids are unique on each engine, but
  their values differ.
- Python UDFs are slower on Snowpark, and literal `create_map` lookups
  with more than 512 entries become UDF lookups there.

A parity test pattern runs the same operator on every engine and compares
the rows (see `tests/unit/quality/test_health_engine_parity.py`).
