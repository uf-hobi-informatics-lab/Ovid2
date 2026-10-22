"""Ovid2 ``s3`` driver — S3 and S3-compatible object storage (D3.s3).

Successor to the pre-D3.s3 pair
:class:`ovid2.connections.s3.S3Connection` +
:class:`ovid2.core.addresses.s3.S3Address`. Both are re-exported here
so import sites just change their path
(``from ovid2.connections.s3 import S3Connection`` →
``from ovid2.drivers.s3 import S3Connection``;
``from ovid2.core.addresses.s3 import S3Address`` →
``from ovid2.drivers.s3 import S3Address``).

The native :class:`S3Driver` self-registers in
:data:`ovid2.drivers.registry.DRIVERS` at :mod:`.driver` import time
(triggered from this ``__init__``).
"""

from __future__ import annotations

# Import the driver module so its bottom-of-file ``register()`` call
# runs, replacing the LegacyDriverAdapter_s3 entry.
from ovid2.drivers.s3.driver import S3Driver
from ovid2.drivers.s3.legacy import S3Address, S3Connection
from ovid2.drivers.s3.metadata import (
    build_address_config,
    connection_fields,
    location_fields,
    resolve_auth_mode,
)

__all__ = [
    "S3Address",
    "S3Connection",
    "S3Driver",
    "build_address_config",
    "connection_fields",
    "location_fields",
    "resolve_auth_mode",
]
