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

A per-pipeline chain overrides a shared chain of the same name. The
schema is `resources/schemas/transform_spec.v1.json`.

## Using a chain

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
| formatting | `to_upper`, `to_lower`, `to_title`, `trim`, `normalize_whitespace`, `regex_replace`, `pad`, `rename_columns`, `cast`, `coalesce_columns`, `default_value` |
| correction | `null_if`, `clamp`, `standardize_null`, `strip_non_numeric`, `strip_non_alpha`, `dedup`, `distinct`, `filter_expr` |
| reshape | `select_columns`, `drop_columns`, `reduce`, `explode`, `unpivot`, `pivot`, `add_row_number`, `rank`, `dense_rank` |
| sampling | `random_sample`, `limit`, `stratified_sample`, `head_by_group` |

The window-based ops (`add_row_number`, `rank`, `dense_rank`,
`stratified_sample`, `head_by_group`) need a Spark-family engine.
Parameters for every op: [Transforms catalogue](../10-reference/transforms.md).

## Discover and validate

| Surface | List | Validate |
| --- | --- | --- |
| REST | `GET /api/v1/transforms[?category=]` | `POST /api/v1/transforms:validate` |
| MCP | `list_transforms` | `validate_transform` |

To write a new op, see [Extending Ovid2](../09-developers/extending.md#add-a-transform).
