"""Pipeline template + pipeline scaffolding service (Phase 3).

Thin web-tier wrapper over :mod:`ovid2.tools.templates` (the curated
catalogue) and :mod:`ovid2.tools.new_pipeline` (the scaffolder). Kept
free of ``ovid2.core.*`` imports so the web tier stays pyspark-free
(see ``tests/contract/test_web_no_pipeline_imports.py``) — the
scaffolder only walks the ``ovid2_pipelines`` namespace and copies
files.
"""

from __future__ import annotations

import logging

from ovid2.web import db as web_db
from ovid2.web.models import (
    PipelineTemplateSummary,
    ScaffoldPipelineRequest,
    ScaffoldPipelineResponse,
)

logger = logging.getLogger(__name__)

__all__ = ["ScaffoldError", "list_pipeline_templates", "scaffold_pipeline"]


class ScaffoldError(ValueError):
    """Raised when a scaffold request cannot be satisfied."""


def list_pipeline_templates() -> list[PipelineTemplateSummary]:
    """Return every curated pipeline template."""
    from ovid2.tools.templates import list_templates

    return [
        PipelineTemplateSummary(
            id=t.id,
            source_pipeline=t.source_pipeline,
            cdm=t.cdm,
            kind=t.kind,
            label=t.label,
            description=t.description,
        )
        for t in list_templates()
    ]


def scaffold_pipeline(
    request: ScaffoldPipelineRequest, *, actor: str = "web"
) -> ScaffoldPipelineResponse:
    """Create a new pipeline package from a template or explicit source.

    Args:
        request: Parsed scaffold payload.
        actor: Free-form origin label for the audit row.

    Returns:
        A :class:`ScaffoldPipelineResponse` naming the new package.

    Raises:
        ScaffoldError: On invalid input or a failed copy.
    """
    from ovid2.tools.new_pipeline import (
        NewPipelineError,
    )
    from ovid2.tools.new_pipeline import (
        scaffold_pipeline as _scaffold,
    )
    from ovid2.tools.templates import get_template

    source_pipeline = request.source_pipeline
    cdm = request.cdm

    if request.template:
        template = get_template(request.template)
        if template is None:
            raise ScaffoldError(f"Unknown pipeline template '{request.template}'.")
        source_pipeline = template.source_pipeline
        cdm = template.cdm
    elif not (source_pipeline and cdm):
        raise ScaffoldError("Supply either `template` or both `source_pipeline` and `cdm`.")

    try:
        dest = _scaffold(request.name, source_pipeline=source_pipeline, cdm=cdm)
    except NewPipelineError as exc:
        raise ScaffoldError(str(exc)) from None

    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="pipeline.scaffold",
        target=request.name,
        detail_json=(
            f'{{"source_pipeline": "{source_pipeline}", "cdm": "{cdm}", "path": "{dest}"}}'
        ),
    )
    logger.info(
        "Scaffolded pipeline '%s' from '%s' (cdm=%s)",
        request.name,
        source_pipeline,
        cdm,
    )
    return ScaffoldPipelineResponse(
        name=request.name,
        source_pipeline=str(source_pipeline),
        cdm=str(cdm),
        path=str(dest),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
