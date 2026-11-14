---
title: Transforms library
summary: Compose CDM-agnostic DataFrame ops (formatting, correction, reshape, sampling) into reusable named chains.
audience: [developer, researcher]
---

The **transforms library** is a set of general-purpose, lazy DataFrame
operations. A transform knows nothing about any CDM, table or pipeline
type. Use transforms for general clean-up: trimming, case, null
standardisation, clamping, unpivoting, keeping the latest row per
patient, or taking a reproducible sample.

## A chain

```yaml
# src/ovid2/resources/transforms/clean_sex.yaml   (shared library)
# or pipelines/ovid2_pipelines/<pipeline>/transforms/clean_sex.yaml (per pipeline)
v: 1
name: clean_sex
description: Normalise the SEX column.
steps:
  - op: trim
    columns: [SEX]
  - op: to_upper
    columns: [SEX]
  - op: standardize_null
    columns: [SEX]
```

Chains are looked up in three places, first hit wins:

| Scope | Location | Edited in |
| --- | --- | --- |
| pipeline-private | `pipelines/ovid2_pipelines/<pipeline>/transforms/<name>.yaml` | pipeline builder → *Private transforms* |
| user library | `$XDG_CONFIG_HOME/ovid2/transforms/<name>.yaml` (`$OVID2_TRANSFORMS_DIR`) | **Transforms** page (`/transforms`) |
| shipped | `src/ovid2/resources/transforms/<name>.yaml` | read-only (duplicate to customise) |

The schema is `resources/schemas/transform_spec.v1.json`. Every step's
parameters are validated against the op's `params_schema` before the chain
runs.

## User-defined transforms (`derive_column`)

A chain *is* your user-defined function: a named, versioned recipe built
from library ops. To compute a value, use `derive_column` with a
Spark-SQL **row expression**:

```yaml
v: 1
name: normalise_sex
description: Upper-case SEX; blank / null becomes UN.
steps:
  - op: derive_column
    column: SEX
    expr: "coalesce(nullif(upper(trim(SEX)), ''), 'UN')"
```

Expressions (here and in `filter_expr`) are parsed and checked against an
allow-list before they run:

- one expression only — no `;`;
- no sub-queries, `SELECT`, `EXISTS`, joins, DDL / DML;
- no aggregates, window functions or lambdas;
- only allow-listed, row-local built-ins (string, null / conditional,
  numeric, date / time, simple array functions). `reflect`,
  `java_method`, `rand`, `input_file_name`, user UDFs and any unknown
  function are rejected.

The same text runs on every engine: Spark natively, DuckDB through the
sqlglot transpiler, Snowpark transpiled to Snowflake SQL.

## Using a chain

### Assign it to a pipeline (recommended)

The `transform` stage runs between `deduplicate` and `map`. It applies the
chains assigned in `<pipeline>/transforms/assignments.yaml`:

```yaml
v: 1
tables:
  "*":                          # every table, first
    - spec: clean_whitespace_and_nulls
  demographic:
    - spec: normalise_sex
    - steps:                    # an inline, anonymous chain
        - op: to_upper
          columns: [RACE]
```

Assign from the **Transforms** page (*Assign to a pipeline*), the pipeline
builder (*Transform assignments*), `POST /api/v1/transform-library/{name}/assign`,
or the `assign_transform` MCP tool. A pipeline without assignments skips
the stage. Schema: `resources/schemas/transform_assignments.v1.json`.

### Inside a mapper

From a mapper spec, via the `apply_transforms` op:

```yaml
ops:
  - op: apply_transforms
    spec: clean_sex                 # a named chain
  - op: apply_transforms
    steps:                          # or inline
      - op: clamp
        column: RESULT_NUM
        min: 0
        max: 100000
```

## The catalogue

| Category | Ops |
| --- | --- |
| formatting | `to_upper`, `to_lower`, `to_title`, `trim`, `normalize_whitespace`, `regex_replace`, `pad`, `rename_columns`, `cast`, `coalesce_columns`, `default_value`, `derive_column` |
| correction | `null_if`, `clamp`, `standardize_null`, `strip_non_numeric`, `strip_non_alpha`, `dedup`, `distinct`, `filter_expr` |
| reshape | `select_columns`, `drop_columns`, `reduce`, `explode`, `unpivot`, `pivot`, `add_row_number`, `rank`, `dense_rank` |
| sampling | `random_sample`, `limit`, `stratified_sample`, `head_by_group` |

The window-based ops (`add_row_number`, `rank`, `dense_rank`,
`stratified_sample`, `head_by_group`) need a Spark-family engine.
Parameters for every op: [Transforms catalogue](../10-reference/transforms.md).

## Discover and validate

| Surface | List | Validate |
| --- | --- | --- |
| REST | `GET /api/v1/transforms[?category=]`, `GET /api/v1/transform-library` | `POST /api/v1/transforms:validate` |
| MCP | `list_transforms`, `list_transform_library` | `validate_transform` |
| Save | `PUT /api/v1/transform-library/{name}`, MCP `save_transform` | validated on save |

To write a new op, see [Extending Ovid2](../09-developers/extending.md#add-a-transform).

## Applying a UDF from the navigator

The [pipeline navigator](../03-web-panel/navigator.md) applies a computed
column, a chain of functions or an existing chain to a field or table in
one step, and shows where every chain is used.
