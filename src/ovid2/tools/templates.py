"""Pipeline template catalogue.

A *template* is an existing pipeline package used as the scaffold for a
new pipeline. Creating an ETL-type pipeline copies the template's
``pipelines/ovid2_pipelines/<template>/`` tree under the user-given name;
the operator then personalises the dictionaries, formatters, and fixer
chains.

The catalogue is keyed by a short template id and records:

- ``source_pipeline`` — the on-disk pipeline package to clone.
- ``cdm`` — the target CDM the cloned pipeline produces
  (``resolve_cdm_for_pipeline`` reads it from the copied
  ``pipeline_settings``).
- ``kind`` — the pipeline kind the template is intended for. Every
  template here targets ``etl``; the ``analysis`` / ``dq_checks`` /
  ``copy`` / ``move`` kinds do not bind a pipeline package and so do
  not need a template.
- ``label`` / ``description`` — human-facing copy for the pipeline
  create UI.

Templates are deliberately a small curated set — the three canonical
ETL shapes Ovid2 ships today:

- ``pcornet_etl``      — source feed → PCORnet 7.0 CDM (clones ``example_pcornet``).
- ``omop_source_etl``  — OMOP source feed → PCORnet 7.0 (clones ``acme``).
- ``pcornet_to_omop``  — PCORnet source → OMOP 5.3 (clones ``pcornet_to_omop``).

Templates whose source package is not installed are hidden by
:func:`list_templates` / :func:`get_template`.

Add a template by appending an entry here; the scaffolder
(:mod:`ovid2.tools.new_pipeline`) and the contract test
(``tests/contract/test_pipeline_templates_exist.py``) pick it up
automatically.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass

__all__ = ["PipelineTemplate", "TEMPLATES", "get_template", "list_templates"]


@dataclass(frozen=True)
class PipelineTemplate:
    """One entry in the pipeline template catalogue."""

    id: str
    source_pipeline: str
    cdm: str
    kind: str
    label: str
    description: str


TEMPLATES: dict[str, PipelineTemplate] = {
    "pcornet_etl": PipelineTemplate(
        id="pcornet_etl",
        source_pipeline="example_pcornet",
        cdm="pcornet_7_0",
        kind="etl",
        label="PCORnet ETL",
        description=(
            "Source feed → PCORnet 7.0 CDM. Declarative "
            "source-passthrough / semantic-join formatters plus a "
            "dictionaries.py and fixer_configs.py to personalise."
        ),
    ),
    "omop_source_etl": PipelineTemplate(
        id="omop_source_etl",
        source_pipeline="acme",
        cdm="pcornet_7_0",
        kind="etl",
        label="OMOP → PCORnet",
        description=(
            "OMOP source feed → PCORnet 7.0 CDM. Ships semantic_join "
            "formatters over the OMOP CONCEPT view plus six hand-written "
            "custom formatters to adapt."
        ),
    ),
    "pcornet_to_omop": PipelineTemplate(
        id="pcornet_to_omop",
        source_pipeline="pcornet_to_omop",
        cdm="omop_5_3",
        kind="etl",
        label="PCORnet → OMOP ETL",
        description=(
            "PCORnet source → OMOP 5.3 CDM. 18 custom formatters that "
            "resolve OMOP concept ids live from the CONCEPT view."
        ),
    ),
}


def _source_installed(template: PipelineTemplate) -> bool:
    """Return True when the template's ``source_pipeline`` package is importable.

    Deployments may ship only a subset of the pipeline packages (the public
    distribution, for example, carries only the generic ones), so a
    template whose source package is absent is hidden rather than offered
    and then failing at scaffold time.
    """
    try:
        return importlib.util.find_spec(f"ovid2_pipelines.{template.source_pipeline}") is not None
    except (ImportError, ValueError):
        # ``ovid2_pipelines`` itself is not on sys.path.
        return False


def list_templates(*, include_unavailable: bool = False) -> list[PipelineTemplate]:
    """Return the templates, ordered by id.

    Args:
        include_unavailable: Also return templates whose source pipeline
            package is not installed in this deployment.
    """
    return [
        TEMPLATES[k]
        for k in sorted(TEMPLATES)
        if include_unavailable or _source_installed(TEMPLATES[k])
    ]


def get_template(template_id: str) -> PipelineTemplate | None:
    """Return the available template with ``template_id``, else ``None``."""
    template = TEMPLATES.get(template_id)
    if template is None or not _source_installed(template):
        return None
    return template


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        "This module is a data catalogue imported by the scaffolder; "
        "it is not a script."
    )
