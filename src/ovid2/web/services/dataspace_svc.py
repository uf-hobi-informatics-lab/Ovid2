"""Read-only ODS service for the web tier.

Wraps :mod:`ovid2.dataspace` and returns pydantic models the FastAPI
router can hand back verbatim. Never imports pyspark or any pipeline
module — see ``tests/contract/test_web_no_pipeline_imports.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from ovid2 import dataspace
from ovid2.dataspace.path import OdsPath, OdsPathError
from ovid2.dataspace.resolver import OdsResolutionError
from ovid2.web.models import (
    OdsBatchListing,
    OdsBatchSummary,
    OdsDimensionCatalog,
    OdsDimensionSummary,
    OdsPipelineListing,
    OdsPipelineSummary,
    OdsResolveResult,
    OdsTableListing,
    OdsTableSummary,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OdsServiceError",
    "get_dimension_catalog",
    "list_batch_tables",
    "list_pipeline_batches",
    "list_pipelines",
    "resolve_path",
]


class OdsServiceError(ValueError):
    """Raised for user-facing ODS service errors (400-class)."""


def get_dimension_catalog() -> OdsDimensionCatalog:
    """Return every dimension + the deployment's dimension defaults."""
    # Import lazily so tests that stub settings can override cheaply.
    from ovid2.web.services import settings_svc

    global_settings = settings_svc.get_settings()
    ods_settings = global_settings.ods

    dimensions: list[OdsDimensionSummary] = []
    for dim in dataspace.list_dimensions():
        candidates = dataspace.list_addresses_by_dimension(dim)
        default_address = ods_settings.dimension_defaults.get(dim)
        if default_address and default_address not in candidates:
            # Bogus setting — surface as no-default rather than a hard error
            # so the /dataspace page renders even when settings drift.
            logger.warning(
                "settings.ods.dimension_defaults['%s'] points at unknown address '%s'; ignoring.",
                dim,
                default_address,
            )
            default_address = None
        dimensions.append(
            OdsDimensionSummary(
                name=dim,
                address_count=len(candidates),
                default_address=default_address,
            )
        )
    return OdsDimensionCatalog(
        default_dimension=ods_settings.default_dimension,
        dimensions=dimensions,
    )


def _known_dimensions_for_pipeline(pipeline: str) -> list[str]:
    """Return every dimension that has at least one address for a pipeline.

    The disc dimension is answered by scanning the filesystem; other
    dimensions currently answer "unknown" and are omitted. See the
    ADR's follow-up section.
    """
    if pipeline in dataspace.list_pipelines(dim="disc"):
        return ["disc"]
    return []


def list_pipelines() -> OdsPipelineListing:
    """Return every pipeline discoverable in the data space."""
    pipelines: list[OdsPipelineSummary] = []
    for name in dataspace.list_pipelines():
        pipelines.append(
            OdsPipelineSummary(
                name=name,
                known_dimensions=_known_dimensions_for_pipeline(name),
            )
        )
    return OdsPipelineListing(pipelines=pipelines)


def list_pipeline_batches(pipeline: str) -> OdsBatchListing:
    """Return every batch under a pipeline (disc dimension only in v1)."""
    if not pipeline:
        raise OdsServiceError("pipeline is required")
    batches = [
        OdsBatchSummary(batch_id=b, dimensions=["disc"])
        for b in dataspace.list_batches(pipeline, dim="disc")
    ]
    return OdsBatchListing(pipeline=pipeline, batches=batches)


def list_batch_tables(pipeline: str, batch_id: str) -> OdsTableListing:
    """Return every table under a pipeline batch."""
    if not pipeline:
        raise OdsServiceError("pipeline is required")
    if not batch_id:
        raise OdsServiceError("batch_id is required")
    tables = [
        OdsTableSummary(name=t, dimensions=["disc"])
        for t in dataspace.list_tables(pipeline, batch_id, dim="disc")
    ]
    return OdsTableListing(pipeline=pipeline, batch_id=batch_id, tables=tables)


def resolve_path(path: str, dim: str | None = None) -> OdsResolveResult:
    """Resolve an ODS path — returns naming info, never the address object."""
    try:
        target = dataspace.resolve(path, dim=dim, materialise=False)
    except OdsPathError as exc:
        raise OdsServiceError(str(exc)) from exc
    except OdsResolutionError as exc:
        raise OdsServiceError(str(exc)) from exc
    return OdsResolveResult(
        path=target.path.as_str(),
        pipeline=target.path.pipeline,
        batch_id=target.path.batch_id,
        table=target.path.table,
        dimension=target.dimension,
        address_name=target.address_name,
        tier=target.tier,
        candidates=list(target.candidates),
    )


def _serialise_path(path: OdsPath) -> dict[str, Any]:
    """Helper used by tests to snapshot an OdsPath as a dict."""
    return {
        "path": path.as_str(),
        "pipeline": path.pipeline,
        "batch_id": path.batch_id,
        "table": path.table,
        "depth": path.depth,
    }
