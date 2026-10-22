# Public transformations library

The transformations library is a **CDM-agnostic, pipeline-type-free menu
of general-purpose lazy DataFrame operations** that users and Agent Ovid
can discover and compose into reusable chains. It is the fourth
declarative layer in Ovid2, alongside mapper ops, fixers, and formatter
adapters — but unlike those, a transform knows nothing about any CDM,
table, or pipeline type.

Use it for general reshaping, formatting, correction, and sampling:
uppercase a column, drop duplicates, clamp a value, melt wide columns to
long, keep the latest row per patient, take a reproducible random
sample, and so on.

## Where transforms live

```
src/ovid2/transforms/
  base.py            # BaseTransform ABC + TransformContext
  registry.py        # TRANSFORMS dict + register()
  spec.py            # TransformSpec + loaders
  engine.py          # run(): walk steps, gate families
  ops/               # one module per family of ops
src/ovid2/resources/transforms/<name>.yaml   # shared library specs
pipelines/ovid2_pipelines/<pipeline>/transforms/<name>.yaml  # per-pipeline specs
```

A transform spec is a **named, reusable chain**. Specs are loaded by
name; a per-pipeline spec overrides a shared-library spec of the same
name.

## Spec shape

```yaml
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

Each step is `{op: <name>, ...params}`. The op name must be registered
in `ovid2.transforms.registry.TRANSFORMS`; the engine fails fast on
unknown ops. Schema: `resources/schemas/transform_spec.v1.json`.

## The op catalogue

| Category | Ops |
| --- | --- |
| **formatting** | `to_upper`, `to_lower`, `to_title`, `trim`, `normalize_whitespace`, `regex_replace`, `pad`, `rename_columns`, `cast`, `coalesce_columns`, `default_value` |
| **correction** | `null_if`, `clamp`, `standardize_null`, `strip_non_numeric`, `strip_non_alpha`, `dedup`, `distinct`, `filter_expr` |
| **reshape** | `select_columns`, `drop_columns`, `reduce`, `explode`, `unpivot`, `pivot`, `add_row_number`, `rank`, `dense_rank` |
| **sampling** | `random_sample`, `limit`, `stratified_sample`, `head_by_group` |

Ops marked `requires_family: spark` (`add_row_number`, `rank`,
`dense_rank`, `stratified_sample`, `head_by_group`) are gated to the
Spark family; the engine refuses them on another family with a clear
error.

Per-op parameters are published in each op's `params_schema` — see the
catalogue endpoint below or the op module docstrings.

## Discovering transforms (users + agents)

The library is **queryable** — the point is that you don't have to read
the source to know what exists.

- **HTTP**: `GET /api/v1/transforms` (optionally `?category=sampling`)
  returns every transform's name / summary / category / requires_family /
  params_schema. `POST /api/v1/transforms:validate` validates a
  candidate spec (schema + op-name check) before you save it.
- **MCP**: the `list_transforms` and `validate_transform` tools wrap
  those endpoints for Agent Ovid.

The web tier reads a web-safe catalogue mirror
(`ovid2.config.transforms_catalogue`) and never imports the pipeline-tier
`transforms` package.

## Using a chain today: the `apply_transforms` mapper op

Until a first-class pipeline stage lands, the whole library is usable
from any pipeline's **mapper spec** via the `apply_transforms` bridge op.

Inline chain:

```yaml
# pipelines/<pipeline>/mappers/demographic.yaml
v: 1
table: demographic
ops:
  - op: apply_transforms
    steps:
      - op: trim
        columns: [SEX]
      - op: to_upper
        columns: [SEX]
```

Named shared spec:

```yaml
ops:
  - op: apply_transforms
    spec: clean_whitespace_and_nulls
```

## Writing a new transform

1. Add a `BaseTransform` subclass under `src/ovid2/transforms/ops/`.
   Declare `name`, `summary`, `category`, `params_schema`, and (if it
   needs a Spark-only feature) `requires_family`.
2. Do **not** `import pyspark`. Use `ctx.functions` (the portable
   `EngineFunctions` DSL) for column work and the portable DataFrame API
   for shape changes. This keeps the library Snowpark-ready and passes
   `tests/contract/test_no_pyspark_outside_engine.py`.
3. Register it in `ovid2.transforms.registry.TRANSFORMS`.
4. Add its metadata to `ovid2.config.transforms_catalogue.TRANSFORM_CATALOGUE`
   (the contract test `test_transforms_catalogue_synchronised.py` will
   fail until you do).
5. Add a unit test under `tests/unit/transforms/`.

See the adr-public-transforms-library design note for the
design rationale.
