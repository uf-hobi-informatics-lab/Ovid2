"""Ovid2 ``access`` driver — Microsoft Access (.accdb / .mdb) source.

Reads a Microsoft Access database file as a set of tables — each user
table inside the file is surfaced and read as a *separate* output
table. Backed by the UCanAccess JDBC driver baked into the ETL image.

The native :class:`AccessDriver` self-registers in
:data:`ovid2.drivers.registry.DRIVERS` at :mod:`.driver` import time
(triggered from this ``__init__``). The legacy
:class:`AccessConnection` / :class:`AccessAddress` pair is re-exported
for the :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` and
:data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` catalogues.
"""

from __future__ import annotations

# Import the driver module so its bottom-of-file ``register()`` call
# runs, replacing any LegacyDriverAdapter_access fallback.
from ovid2.drivers.access.driver import AccessDriver
from ovid2.drivers.access.legacy import AccessAddress, AccessConnection
from ovid2.drivers.access.metadata import (
    connection_fields,
    container_requirements_from,
    location_fields,
    normalise_access_args,
)

__all__ = [
    "AccessAddress",
    "AccessConnection",
    "AccessDriver",
    "connection_fields",
    "container_requirements_from",
    "location_fields",
    "normalise_access_args",
]
