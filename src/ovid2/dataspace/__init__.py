"""Ovid Data Space (ODS) — naming layer over connections + addresses.

A single POSIX-style namespace that identifies a health-data table by
``/<pipeline>/<batch_id>/<table>``. Vendor storage (disc, S3, Azure,
Snowflake, …) is orthogonal and referred to as a *dimension*.

See :doc:the adr-ods-naming-layer design note for the
design rationale.

Public API:

- :class:`OdsPath` — value object for a normalised ODS path.
- :class:`ResolvedTarget` — the outcome of :func:`resolve`.
- :func:`resolve` — ``(path, dim) → ResolvedTarget``.
- :func:`list_pipelines`, :func:`list_batches`, :func:`list_tables` —
  read-only listings.
- :func:`list_dimensions` — every registered dimension name.
- :func:`dimension_for_address` — connection-type of an address.

v1 is deliberately a *naming-only* layer: no read/write facade, no
mount table, no cross-dimension mirroring. See the ADR for the
scope split.
"""

from __future__ import annotations

from ovid2.dataspace.path import OdsPath
from ovid2.dataspace.registry import (
    dimension_for_address,
    list_addresses_by_dimension,
    list_dimensions,
)
from ovid2.dataspace.resolver import (
    OdsResolutionError,
    ResolvedTarget,
    list_batches,
    list_pipelines,
    list_tables,
    resolve,
)

__all__ = [
    "OdsPath",
    "OdsResolutionError",
    "ResolvedTarget",
    "dimension_for_address",
    "list_addresses_by_dimension",
    "list_batches",
    "list_dimensions",
    "list_pipelines",
    "list_tables",
    "resolve",
]
