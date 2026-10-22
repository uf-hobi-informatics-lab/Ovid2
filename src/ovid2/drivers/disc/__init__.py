"""Ovid2 ``disc`` driver — filesystem peripheral (D3.disc).

Successor to the pre-D3.disc pair
:class:`ovid2.connections.disc.DiscConnection` +
:class:`ovid2.core.addresses.disc.DiscAddress`. Both are re-exported
here so import sites just change their path
(``from ovid2.connections.disc import DiscConnection`` →
``from ovid2.drivers.disc import DiscConnection``).

The native :class:`DiscDriver` self-registers in
:data:`ovid2.drivers.registry.DRIVERS` at :mod:`.driver` import time
(triggered from this ``__init__``).
"""

from __future__ import annotations

# Import the driver module so its bottom-of-file ``register()`` call
# runs, replacing the LegacyDriverAdapter_disc entry.
from ovid2.drivers.disc.driver import DiscDriver
from ovid2.drivers.disc.legacy import DiscAddress, DiscConnection
from ovid2.drivers.disc.metadata import (
    connection_fields,
    container_requirements_from,
    location_fields,
    normalise_disc_args,
)

__all__ = [
    "DiscAddress",
    "DiscConnection",
    "DiscDriver",
    "connection_fields",
    "container_requirements_from",
    "location_fields",
    "normalise_disc_args",
]
