"""Ovid Data Space (ODS) — every storage account as a tree of plain directories.

A single POSIX-style namespace ``/<connection>/<path…>``: the first
segment is a storage account (connection), the rest is the location
under it — folders for disc / s3 / azure_blob / gcs, ``DATABASE/SCHEMA``
for warehouses. Every data space is the same; there is no
``pipeline`` / ``batch_id`` level. The complete path of a folder /
schema is the ``batch_id`` of a run that reads it, and an address
pointing at it is the exact location its data is read from / written
to.

See :doc:the adr-address-is-exact-location design note.

Public API:

- :class:`OdsPath` — value object for a normalised path.
- :func:`resolve` — path → storage account + exact location (+ the
  saved address pointing exactly there).
- :func:`list_children` — one level under a path.
- :func:`list_storage_accounts` — the root.
- :func:`list_dimensions` / :func:`dimension_for_address` — storage
  types (the connection-type families).
"""

from __future__ import annotations

from ovid2.dataspace.path import OdsPath
from ovid2.dataspace.registry import (
    dimension_for_address,
    list_addresses_by_dimension,
    list_dimensions,
)
from ovid2.dataspace.resolver import (
    OdsEntry,
    OdsResolutionError,
    ResolvedTarget,
    driver_lister,
    list_children,
    list_storage_accounts,
    resolve,
)

__all__ = [
    "OdsEntry",
    "OdsPath",
    "OdsResolutionError",
    "ResolvedTarget",
    "dimension_for_address",
    "driver_lister",
    "list_addresses_by_dimension",
    "list_children",
    "list_dimensions",
    "list_storage_accounts",
    "resolve",
]
