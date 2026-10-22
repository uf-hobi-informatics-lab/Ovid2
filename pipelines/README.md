# pipelines/

Pipeline packages live under ``pipelines/ovid2_pipelines/<name>/``. The
directory is a PEP 420 namespace package: ``pipelines/ovid2_pipelines/``
deliberately has no ``__init__.py``, so several deployments can drop pipeline
packages at different paths without conflicting.

## Bundled pipelines

| Package | Shape | Purpose |
| --- | --- | --- |
| `example_pcornet` | PCORnet 7.0 text extracts → PCORnet 7.0 CDM | Site-neutral reference pipeline; the `pcornet_etl` scaffolding template. Every identity value in `pipeline_settings.py` is a placeholder. |
| `pcornet_to_omop` | PCORnet 7.0 CDM → OMOP CDM 5.3 | Full 18-table converter; the `pcornet_to_omop` scaffolding template. |

Site-specific pipelines (your own feeds) are meant to live **outside** this
repository: put them in your own `ovid2_pipelines/` directory and add its parent
to `PYTHONPATH` (or install them as a separate package). The namespace package
merges every `ovid2_pipelines/` on `sys.path`.

## Layout

Each pipeline directory contains:

- ``__init__.py``          — regular package marker.
- ``pipeline_settings.py``  — constants only (mapper family, harvest, datavant, upload).
- ``fixer_configs.py``     — ``FIXER_CHAINS`` + ``MULTI_TABLE_FIXES`` dicts.
- ``dictionaries.py``      — per-column value-mapping dictionaries.
- ``formatters/``          — YAML spec files, one per CDM output table.
- ``formatters_custom/``   — hand-written ``BaseFormatter`` subclasses for the
  ``adapter: custom`` escape hatch (rare).
- ``mappers/``             — optional declarative mapper specs (one YAML per table).

## Adding a pipeline

Scaffold from a template, then personalise it:

```
ovid2 tools new-pipeline --template pcornet_etl --name myhospital
ovid2 pipeline validate --pipeline myhospital
```

The scaffolder copies the template and rewrites its identity constants
(DB name, datamart id/name, Datavant key) to placeholders derived from the new
name. The web control panel's **New pipeline** wizard does the same thing.
