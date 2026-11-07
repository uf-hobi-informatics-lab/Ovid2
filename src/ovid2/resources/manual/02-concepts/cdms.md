---
title: Data models (CDMs)
summary: How a common data model is described as YAML so that the ETL engine never depends on any one CDM.
audience: [researcher, developer]
---

A **common data model** fixes what the output looks like:

- which tables exist, and their columns and types;
- each table's key;
- which values are allowed;
- what counts as "no information".

Ovid2 bundles these CDMs:

| CDM | Version | Purpose |
| --- | --- | --- |
| `pcornet_7_0` | 7.0 | PCORnet Common Data Model — the default target |
| `omop_5_3` | 5.3 | OHDSI OMOP Common Data Model |
| `ovid2_pipeline_nursing_v1`, `_tumor_v1`, `_veritas_v1` | 1.0 | lightweight models for specialised source families |

Browse them in the panel under **Configure → CDMs**, or read the generated
[CDM reference](../10-reference/cdms.md). Researchers should start at
[Data & data models](../05-data/index.md).

## CDMs are data, not code

Each CDM is a YAML tree:

```text
src/ovid2/resources/cdms/pcornet_7_0/
├── cdm.yaml              version, table order, reference tables,
│                         flavour-of-null, id columns, shared views
├── tables/<table>.yaml   fields (name, type, length), composite key, constraints
└── valuesets/<t>.<f>.yaml   bounded enumerations (SEX, DX_TYPE, …)
```

The engine reads everything about a CDM through a **`CdmHandle`** in a
registry, and never imports CDM-specific code. That is why:

- the same deduplicator, fixers and writers serve PCORnet and OMOP alike;
- a new CDM is a YAML directory plus a five-line registration module;
- Snowflake output is created with each column's exact CDM type and length.

## What a CDM provides at run time

| Field | Used by |
| --- | --- |
| table list and order | `--tables all`, reference-table ordering |
| composite keys | the deduplicate stage |
| reference tables | ordering, cross-table fixers |
| field types and lengths | formatter validation, typed warehouse DDL |
| valuesets | health checks (`valueset_conformance`), mapper targets |
| flavour-of-null | fixers such as `delete_when_null_or_fon` |
| shared views | e.g. OMOP `CONCEPT` views for concept joins |

Every bounded valueset is also registered as a vocabulary called
`<cdm>.<table>.<column>`, so mappers and checks can refer to it by name.

To add or regenerate a CDM, see [Extending Ovid2](../09-developers/extending.md#add-a-cdm).
