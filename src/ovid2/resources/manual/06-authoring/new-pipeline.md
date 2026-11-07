---
title: Scaffold a new pipeline
summary: Create a pipeline package from a template with the CLI or the web panel.
audience: [developer]
---

## From the CLI

```bash
ovid2 tools new-pipeline --template pcornet_etl --name myhospital
ovid2 pipeline validate --pipeline myhospital
ovid2 pipeline tables myhospital
```

| Template | Copies | Target CDM | Use for |
| --- | --- | --- | --- |
| `pcornet_etl` | `example_pcornet` | `pcornet_7_0` | PCORnet-shaped text extracts → PCORnet |
| `pcornet_to_omop` | `pcornet_to_omop` | `omop_5_3` | PCORnet database → OMOP |

Templates whose source package isn't installed are hidden. To clone any
installed package instead, use `--source <package> --cdm <cdm>`.

The scaffolder copies the package under the new name and **resets its
identity values** (database name, HARVEST datamart ids, Datavant key) to
placeholders. Fill them in yourself.

## From the web panel

**Pipelines → + New pipeline → kind ETL**. Choose an **ETL template** and
a name. When you save, a package is scaffolded under that name and a
pipeline config is created that points to it.

## What you get

```text
ovid2_pipelines/myhospital/
├── __init__.py
├── pipeline_settings.py        ← edit identity values
├── dictionaries.py             ← adapt value maps
├── fixer_configs.py            ← review per-table chains
├── formatters/*.yaml           ← one per CDM table; adapt globs and projections
├── formatters_custom/          ← only if the template needs custom formatters
└── mappers/*.yaml              ← value-mapping specs
```

## Turning a table off

If your source doesn't provide a table, set `enabled: false` in that
table's formatter. `--tables all` then skips it, and the run summary says
so.

Next: [Settings & dictionaries](settings-and-dictionaries.md).
