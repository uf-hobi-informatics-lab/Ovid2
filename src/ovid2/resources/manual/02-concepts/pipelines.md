---
title: Pipelines & job kinds
summary: Pipeline packages hold the rules; pipeline configs are saved jobs; the kind decides what a job does.
audience: [user, developer]
---

The word *pipeline* covers two related things in Ovid2.

## Pipeline package — the rules for one source

A **pipeline package** is a Python package in the `ovid2_pipelines`
namespace. It describes one data source: how to read its files, how to
translate its codes, and which corrections to apply.

```text
pipelines/ovid2_pipelines/<name>/
├── __init__.py
├── pipeline_settings.py   target CDM, identity values (HARVEST), defaults
├── dictionaries.py        source value → standard value maps
├── fixer_configs.py       per-table fixer chains
├── formatters/<table>.yaml    one declarative spec per CDM table
├── formatters_custom/*.py     rare hand-written formatters (adapter: custom)
├── mappers/<table>.yaml       declarative value-mapping specs (optional)
└── transforms/<name>.yaml     reusable transform chains (optional)
```

Two packages ship publicly:

| Package | Converts | Use it |
| --- | --- | --- |
| `example_pcornet` | PCORnet text extracts → PCORnet CDM 7.0 | as a reference, and as the `pcornet_etl` scaffolding template |
| `pcornet_to_omop` | PCORnet CDM 7.0 → OMOP CDM 5.3 (18 of 18 tables) | to publish a PCORnet database as OMOP |

Site packages live in your own `ovid2_pipelines/` directory on
`PYTHONPATH`. The namespace merges them with the bundled packages. See
[Authoring pipelines](../06-authoring/index.md).

## Pipeline config — a saved, schedulable job

A **pipeline config** is a JSON document in
`$XDG_CONFIG_HOME/ovid2/pipelines/<name>.json`. You manage configs on the
web *Pipelines* page. A config pins:

- the **kind**;
- the **pipeline package** (for package-backed kinds);
- the input and output addresses (either may be left open);
- tables, stages, engine and workers;
- a **trigger**: manual, cron, interval or watch.

## Job kinds

| Kind | What it does | Needs a package? | CDM-shaped? |
| --- | --- | --- | --- |
| `etl` | format → deduplicate → transform → map → fix → write | yes (scaffolded from a template) | yes |
| `analysis` | read-only summaries (monthly counts, distinct values) | yes (pick an existing one) | no |
| `dq_checks` | PCORnet EDC checks for a report cycle | no | reads CDM tables |
| `health` | EDC checks + Ovid2 health panels with a browsable report | no | reads CDM tables |
| `copy` | stream tables from one address to another, unchanged | no | no |
| `move` | driver-native successor to `copy` | no | no |
| `export` | one table → CSV / Excel file (from the Data Space page only) | no | no |

*Package-free* kinds (`health`, `dq_checks`, `copy`, `move`) don't import
any pipeline package. Their `pipeline` field is just a label. Health checks
default to the generic label `pcornet_edc`, so one config can check any
PCORnet database.

The complete, generated table is in [Run kinds](../10-reference/run-kinds.md).
