---
title: Authoring pipelines
summary: Describe a new data source as a pipeline package — mostly YAML — then validate and run it.
audience: [developer]
---

A **pipeline package** turns one source's raw extract into a CDM. You
describe it declaratively, as data:

| You write | Where | It controls |
| --- | --- | --- |
| Settings | `pipeline_settings.py` | target CDM, identity (HARVEST), defaults |
| Formatter specs | `formatters/<table>.yaml` | how each CDM table is built from raw files |
| Mapper specs | `mappers/<table>.yaml` | how values are translated to standard codes |
| Dictionaries | `dictionaries.py` | the value maps the mappers use |
| Fixer chains | `fixer_configs.yaml` (or `fixer_configs.py`) | which corrections apply to each table |
| Transform chains | `transforms/<name>.yaml` + `transforms/assignments.yaml` (optional) | reusable clean-up / user-defined columns, run in the `transform` stage |

Hand-written Python is the **exception**. Use it only behind
`adapter: custom` (formatters) or `class:` (mappers), and only when the
declarative form can't express what you need.

## Workflow

1. [Scaffold a package](new-pipeline.md) from a template.
2. Fill in [settings and dictionaries](settings-and-dictionaries.md).
3. Write a [formatter](formatters.md) per table. Start with `format`-only
   runs.
4. Add [mappers](mappers.md) for coded columns.
5. Choose a [fixer chain](fixers.md) per table.
6. Optionally compose [transforms](transforms.md).
7. [Validate and test](validate-and-test.md), then run a health check on
   the output.

> [!TIP]
> Profile the source first with an [analysis run](../04-running-jobs/analysis-and-dq.md)
> (`raw_distinct_values`). Its value lists are the fastest way to fill in
> `dictionaries.py`.

## Where your package lives

The `ovid2_pipelines` namespace merges every directory on `PYTHONPATH`
that contains an `ovid2_pipelines/` folder. Keep site packages in your
own repository and add its `pipelines/` directory to `PYTHONPATH` (or set
`OVID2_PIPELINE_PACKAGES_DIR`). From a source checkout the CLI and web find
`<repo>/pipelines` automatically. Production jobs run inside the Docker image, so
**rebuild the image** after changing a package.
