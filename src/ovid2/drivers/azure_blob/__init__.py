"""Ovid2 ``azure_blob`` driver — ADLS Gen2 / Azure Blob Storage (D3.azure_blob).

Successor to the pre-D3.azure_blob pair
:class:`ovid2.connections.azure_blob.AzureBlobConnection` +
:class:`ovid2.core.addresses.azure_blob.AzureBlobAddress`. Both are
re-exported here so import sites just change their path
(``from ovid2.connections.azure_blob import AzureBlobConnection`` →
``from ovid2.drivers.azure_blob import AzureBlobConnection``;
``from ovid2.core.addresses.azure_blob import AzureBlobAddress`` →
``from ovid2.drivers.azure_blob import AzureBlobAddress``).

The native :class:`AzureBlobDriver` self-registers in
:data:`ovid2.drivers.registry.DRIVERS` at :mod:`.driver` import time
(triggered from this ``__init__``).
"""

from __future__ import annotations

# Import the driver module so its bottom-of-file ``register()`` call
# runs, replacing the LegacyDriverAdapter_azure_blob entry.
from ovid2.drivers.azure_blob.driver import AzureBlobDriver
from ovid2.drivers.azure_blob.legacy import AzureBlobAddress, AzureBlobConnection
from ovid2.drivers.azure_blob.metadata import (
    build_address_config,
    connection_fields,
    location_fields,
    resolve_auth_mode,
)

__all__ = [
    "AzureBlobAddress",
    "AzureBlobConnection",
    "AzureBlobDriver",
    "build_address_config",
    "connection_fields",
    "location_fields",
    "resolve_auth_mode",
]
