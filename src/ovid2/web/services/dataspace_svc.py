"""Read-only Data Space service for the web tier.

Every storage account is a tree of plain directories:
``/<connection>/<path…>``. Listing a folder / schema goes through the
``ovid2 tools browse-storage`` executor broker (via
:mod:`storage_browse_svc`) so the API process never resolves a secret.
Never imports pyspark or any pipeline module —
``tests/contract/test_web_no_pipeline_imports.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ovid2 import dataspace
from ovid2.dataspace.path import OdsPathError
from ovid2.dataspace.resolver import OdsResolutionError
from ovid2.web.models import (
    OdsDimensionCatalog,
    OdsDimensionSummary,
    OdsEntryModel,
    OdsListing,
    OdsResolveResult,
)

logger = logging.getLogger(__name__)

__all__ = [
    "OdsServiceError",
    "get_dimension_catalog",
    "list_path",
    "resolve_path",
]


class OdsServiceError(ValueError):
    """Raised for user-facing ODS service errors (400-class)."""


def get_dimension_catalog() -> OdsDimensionCatalog:
    """Return every storage type (dimension) and how many addresses use it."""
    from ovid2.web.services import settings_svc

    ods_settings = settings_svc.get_settings().ods
    dimensions: list[OdsDimensionSummary] = []
    for dim in dataspace.list_dimensions():
        candidates = dataspace.list_addresses_by_dimension(dim)
        default_address = ods_settings.dimension_defaults.get(dim)
        if default_address and default_address not in candidates:
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


def _brokered_lister(
    connection: str, prefix: str | None
) -> tuple[bool, Sequence[dict[str, Any]], str | None]:
    """List one level of a storage account through the executor broker."""
    from ovid2.web.services import storage_browse_svc

    try:
        response = storage_browse_svc.browse_storage(connection, prefix)
    except storage_browse_svc.StorageBrowseError as exc:
        raise OdsServiceError(str(exc)) from exc
    entries = [
        {
            "name": e.name,
            "kind": e.kind,
            "path": e.path,
            "size_bytes": e.size_bytes,
            "modified_at": e.modified_at,
            "row_count": e.row_count,
        }
        for e in response.entries
    ]
    return response.supported, entries, response.message


def list_path(path: str = "/") -> OdsListing:
    """List one level under a Data Space path.

    ``/`` lists every storage account; ``/<conn>/<…>`` lists that folder
    / schema exactly as the storage account's browser does.

    Raises:
        OdsServiceError: On a malformed path or an unknown storage account.
    """
    try:
        target = dataspace.resolve(path or "/")
        supported, entries, message = dataspace.list_children(target.path, lister=_brokered_lister)
    except (OdsPathError, OdsResolutionError) as exc:
        raise OdsServiceError(str(exc)) from exc
    return OdsListing(
        path=target.path.as_str(),
        connection=target.connection,
        type=target.type,
        location=target.location,
        batch_id=target.batch_id,
        address_name=target.address_name,
        supported=supported,
        message=message,
        entries=[
            OdsEntryModel(
                name=e.name,
                kind=e.kind,
                path=e.path,
                type=e.type,
                size_bytes=e.size_bytes,
                modified_at=e.modified_at,
                row_count=e.row_count,
            )
            for e in entries
        ],
    )


def resolve_path(path: str) -> OdsResolveResult:
    """Resolve a Data Space path — naming info only, never the address object."""
    try:
        target = dataspace.resolve(path)
    except (OdsPathError, OdsResolutionError) as exc:
        raise OdsServiceError(str(exc)) from exc
    return OdsResolveResult(
        path=target.path.as_str(),
        connection=target.connection,
        type=target.type,
        location=target.location,
        batch_id=target.batch_id,
        address_name=target.address_name,
        candidates=list(target.candidates),
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Imported by the ovid2 web routes; not a script.")
