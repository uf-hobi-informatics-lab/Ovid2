---
title: Domain packs
summary: Ship a whole data domain — data model, health checks, packages, vocabularies, report templates, operators and fixers — as one drop-in bundle, without editing Ovid2.
audience: [developer, admin]
---

A **domain pack** is a directory with a `pack.yaml` that teaches Ovid2 a
data domain. PCORnet CDM v7.0 is the built-in reference pack. A pack can
contribute:

| Content | Manifest key | Format |
| --- | --- | --- |
| Data model(s) | `cdms: [<id>, <id>_…]` | the C1 YAML tree under `cdms/<name>/` ([Add a CDM](extending.md#add-a-cdm)) |
| Health checks | `checks_dir` | `health_check.v1` specs |
| Report-cycle packages | `packages_dir` | `health_package.v1` manifests |
| Vocabularies / bindings | `vocabularies_dir`, `bindings_dir` | S3 YAML ([Add a vocabulary](extending.md#add-a-vocabulary)) |
| Report Studio templates | `report_templates_dir` | `<source_kind>/<stem>.yaml` for an existing source kind |
| Extra check buckets | `parts: [{id, label}]` | ordered after the core parts |
| Check operators *(pip only)* | `operators_module` | a module using `@operator` |
| Fixers *(pip only)* | `fixers_module` + `fixers` | `FIXERS = {name: BaseFixer}` plus web-safe metadata |

The schema is `resources/schemas/pack.v1.json`.

## Three kinds of pack

| Origin | Where | May ship code | Active |
| --- | --- | --- | --- |
| built-in | `src/ovid2/resources/packs/<id>/` | — (content is in the core tree) | always |
| installed | a Python distribution with an `ovid2.packs` entry point | yes | when enabled |
| folder | `$OVID2_PACKS_DIR` or `$XDG_CONFIG_HOME/ovid2/packs/<id>/` | no — YAML only | when enabled |

**Installing a pack does not enable it.** Enable it on the host:

```bash
ovid2 tools pack enable acme_trials       # writes $XDG_CONFIG_HOME/ovid2/packs.yaml
# or: export OVID2_PACKS_ENABLED=acme_trials   (comma-separated, or * for all)
```

Then restart the web server and the worker. The browser never enables a
pack: an installed pack ships code that the executor imports.

## Naming rule

Every name a pack adds starts with its id, so packs can never collide with
core content or with each other:

- `<id>` or `<id>_…` — CDMs, parts, fixers, operators;
- `<id>.…` — check ids, package ids, vocabularies, report templates.

A pack that breaks the rule, has the same id as another pack, uses a
reserved id, or needs a newer Ovid2 (`requires_ovid2: ">=0.2"`) stays
inactive. `/packs` and `ovid2 tools pack list` show why. A single
mis-named document inside an active pack is skipped with a warning.

## Create a pack

```bash
ovid2 tools pack scaffold acme_trials --out-dir . --domain clinical_trials
ovid2 tools pack validate ./acme_trials
```

The skeleton has one CDM with one table, one check and one package.
`validate` checks the manifest, every content file against its schema, the
naming rule, that each check's operator exists, and that every remediation
fixer and transform resolves. It exits 1 on any error.

For operators or fixers, scaffold an installable distribution instead:

```bash
ovid2 tools pack scaffold acme_trials --out-dir . --python
pip install ./ovid2-pack-acme-trials
```

The generated `pyproject.toml` declares the entry point:

```toml
[project.entry-points."ovid2.packs"]
acme_trials = "ovid2_pack_acme_trials"
```

Pack operators follow the same rules as core operators: lazy, and column
functions from `ovid2.core.engines.dsl`
([Add a health check or operator](extending.md#add-a-health-check-or-operator)).
For each fixer in `fixers_module`, write its `summary`, `scope` and
`column_roles` under `provides.fixers`. The web tier reads that metadata
without importing your code.

## Severity, scope and remediation

Every health check, in a pack or in core, can declare:

```yaml
severity: blocker            # blocker | warning (default) | info
applies_to_cdms: [acme_trials]
remediation:
  hint: Deliver the SUBJECT table in the extract.
  fixers: [acme_trials_trim_ids]
  transforms: [standardize_null]
```

- **severity** orders failures in reports and in the repair context.
  `blocker` means do not release.
- **applies_to_cdms** — a run on any other CDM reports the check
  *not applicable* without reading its tables.
- **remediation** feeds `GET /api/v1/runs/{id}/repair-context` and the
  assistant's `get_repair_context` tool. The assistant can only *propose*
  the listed fixers and transforms; a human applies them. Keep `hint`
  free of data values.

## Containers

The Docker image bakes Ovid2's own source, not packs installed on the
host. For containerised runs, install a pip pack into the image, or mount
a folder pack and set `OVID2_PACKS_DIR` and `OVID2_PACKS_ENABLED` for the
worker.

## Where to look

- `/packs` in the web panel, `GET /api/v1/packs`, MCP `list_packs` /
  `get_pack`.
- `ovid2 tools pack list | show <id>` — JSON.
- Design: the adr-domain-packs design note.

→ `test_packs_tier_neutral.py`, `test_pack_manifests_valid.py`,
`test_check_remediation_resolves.py`
