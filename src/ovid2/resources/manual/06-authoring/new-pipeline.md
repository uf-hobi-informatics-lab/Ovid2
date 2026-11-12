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

## What the copy rewrites

Scaffolding (from the wizard, `POST /api/v1/pipeline-packages/scaffold`
or `ovid2 tools new-pipeline`) makes the clone self-contained:

- every `ovid2_pipelines.<template>` reference (imports, YAML `class:`
  paths) and generated `partner_name = '<template>'` binding is rewritten
  to the new name;
- every **identity** constant the template declares is re-personalised,
  following aliases such as `DEFAULT_PIPELINE_DB_NAME = DEFAULT_PARTNER_DB_NAME`:

| Constant | Clone default | Must match |
| --- | --- | --- |
| `NETWORKID`, `NETWORK_NAME`, `DATAMART_PLATFORM` | template value (`$OVID2_NETWORK_ID`) | HARVEST |
| `DATAMARTID` | `<NETWORKID><NAME>` | HARVEST datamart id |
| `DATAMART_NAME` | the name | HARVEST |
| `DEFAULT_PIPELINE_DB_NAME`, `MERGE_DATABASE_NAME` | `CHAR_<NAME>` | Snowflake databases |
| `DATAVANT_KEY` | the name, lower-case | Datavant site key |
| `CDM_SOURCE_NAME`, `CDM_SOURCE_ABBREVIATION` | name-derived | OMOP `cdm_source` |

Override any of them on the wizard's **Identity** step, with the
`identity` field of the scaffold request, or later in the pipeline
builder's **Settings**. A clone of a `legacy_ovid` port records
`LEGACY_VENDOR_PARTNER` so the fix stage keeps using the template's
vendored fixer scripts.

ETL pipeline names must be Python identifiers (letters, digits, `_`),
because the package is imported as `ovid2_pipelines.<name>`.

## Names that must match

`GET /api/v1/pipeline-packages/{p}/bindings` (MCP `check_pipeline_bindings`,
builder → *Name checks*) checks every cross-configuration name statically:
CDM registration, identity placeholders, the required engine family,
`hash_id` salt keys in `secrets.d/<pipeline>.json`, `dictionaries.py`
names, fixer names / scopes / keys / reference tables / list files,
formatter class paths and join vocabularies, and transform assignments.

