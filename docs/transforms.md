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

Every op runs on every engine (`pyspark`, `databricks_spark`, `duckdb`,
`snowpark`), including the window-based ones (`add_row_number`, `rank`,
`dense_rank`, `stratified_sample`, `head_by_group`). There is no
engine-family gate — see [Engine universality](engines.md#engine-universality).

Per-op parameters are published in each op's `params_schema` — see the
catalogue endpoint below or the op module docstrings.

## Discovering transforms (users + agents)

The library is **queryable** — the point is that you don't have to read
the source to know what exists.

- **HTTP**: `GET /api/v1/transforms` (optionally `?category=sampling`)
  returns every transform's name / summary / category / params_schema. `POST /api/v1/transforms:validate` validates a
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
   Declare `name`, `summary`, `category` and `params_schema`. There is no
   engine-family field: every transform must run on every engine.
2. Do **not** `import pyspark`. Use `ctx.functions` / `ovid2.core.engines.dsl`
   (the portable column DSL, including `Window`, `udf`, `pandas_udf`) for
   column work and the portable DataFrame API (`core/engines/surface.py`)
   for shape changes. This passes
   `tests/contract/test_no_pyspark_outside_engine.py` and
   `test_engine_surface_complete.py`.
3. Declare what it needs from the engine in `REQUIRES` (a
   `frozenset[Capability]` from `ovid2.config.capabilities`): every DSL
   function it calls (`Capability.FN_UPPER`, …) and any feature
   (`Capability.WINDOW`, `Capability.REGEX`, …). When only some parameters
   need a feature, add a `requires_for(cls, params)` classmethod — for
   example `random_sample` needs `rand_seeded` only with a `seed`.
   `test_algorithm_requires_declared.py` AST-checks the declaration against
   the source, and features must be mirrored in
   `ovid2.config.algorithm_requirements` (web-safe, used by the plan-time
   check). See `docs/engines.md` → *Capabilities*.
4. Register it in `ovid2.transforms.registry.TRANSFORMS` (it is then reachable
   through `ovid2.algorithms.registry.AlgorithmRegistry`).
5. Add its metadata to `ovid2.config.transforms_catalogue.TRANSFORM_CATALOGUE`
   (the contract test `test_transforms_catalogue_synchronised.py` will
   fail until you do).
6. Add a unit test under `tests/unit/transforms/`.

See the adr-public-transforms-library design note for the
design rationale.

## User-defined transforms, the library and the transform stage

See the manual page `06-authoring/transforms.md` and
the adr-pipeline-builder design note. In short:

- `derive_column {column, expr, cast?}` computes a column from an
  allow-listed Spark-SQL row expression (`ovid2.config.sql_expr_guard`);
  `filter_expr` uses the same guard.
- Chains are found pipeline-private → `$XDG_CONFIG_HOME/ovid2/transforms`
  (user library) → shipped.
- `<pkg>/transforms/assignments.yaml` assigns chains to tables; the
  `transform` stage (between deduplicate and map) runs them on the active
  engine.
- Edit everything in the web pipeline builder
  (`/pipeline-packages/<p>/builder`) and the `/transforms` library page.

