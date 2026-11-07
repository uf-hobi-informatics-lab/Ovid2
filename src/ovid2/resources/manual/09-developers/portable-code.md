---
title: Portable engine code
summary: Write fixers, mapper ops, transforms and adapters once and run them on Spark, Snowpark and DuckDB.
audience: [developer]
---

Shared algorithm code (fixers, mapper ops, transforms, adapters, health
operators) must run on **every** engine. The rules:

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

## Spark-only code

If something truly needs Spark (window functions over huge partitions,
RDDs, Spark ML), declare it:

| Scope | Declaration | Effect |
| --- | --- | --- |
| one custom formatter | `requires_family = "spark"` | only that table fails on other engines, with a clear message |
| one transform | `requires_family: spark` in its metadata | refused on other families |
| a whole pipeline | `REQUIRES_FAMILY = "spark"` in `pipeline_settings.py` | refused at plan time on DuckDB |
| a raw engine object | `engine.native()` | allowed only when the pipeline declares `REQUIRES_FAMILY` |

## Semantic parity to keep in mind

- Surrogate ids from `stable_hash` differ between engines.
- Ties broken arbitrarily (which duplicate dedup keeps) may differ.
- `monotonically_increasing_id`-style ids are unique on each engine, but
  their values differ.

A parity test pattern runs the same operator on every engine and compares
the rows (see `tests/unit/quality/test_health_engine_parity.py`).
