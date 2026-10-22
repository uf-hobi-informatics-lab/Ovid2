"""Ovid2 ``snowflake`` driver — Snowflake data warehouse (D3.snowflake).

Successor to the pre-D3.snowflake pair
:class:`ovid2.connections.snowflake.SnowflakeConnection` +
:class:`ovid2.core.addresses.snowflake.SnowflakeAddress`. Both are
re-exported here so import sites just change their path
(``from ovid2.connections.snowflake import SnowflakeConnection`` →
``from ovid2.drivers.snowflake import SnowflakeConnection``;
``from ovid2.core.addresses.snowflake import SnowflakeAddress`` →
``from ovid2.drivers.snowflake import SnowflakeAddress``).

The native :class:`SnowflakeDriver` self-registers in
:data:`ovid2.drivers.registry.DRIVERS` at :mod:`.driver` import time
(triggered from this ``__init__``).
"""

from __future__ import annotations

# Import the driver module so its bottom-of-file ``register()`` call
# runs, replacing the LegacyDriverAdapter_snowflake entry.
from ovid2.drivers.snowflake.driver import SnowflakeDriver
from ovid2.drivers.snowflake.legacy import SnowflakeAddress, SnowflakeConnection
from ovid2.drivers.snowflake.metadata import (
    DEFAULT_CREDENTIALS_DIR,
    DEFAULT_PASSPHRASE_FILE,
    DEFAULT_PRIVATE_KEY_FILE,
    build_address_config,
    connection_fields,
    location_fields,
)
from ovid2.drivers.snowflake.probe import (
    _pem_to_der,
    _read_passphrase,
    _read_private_key_pem,
    _resolve_credentials_dir,
)

# Import the staging subpackage so each Snowpark strategy under
# ``ovid2.drivers.snowflake.staging.*`` self-registers on the
# :class:`SnowparkEngineStager` dispatch map. Phase C adds ``disc``;
# Phase D will add ``s3`` and ``azure_blob``. Import is side-effect
# only — the subpackage exports nothing.
from ovid2.drivers.snowflake import staging as _staging  # noqa: F401,E402

__all__ = [
    "DEFAULT_CREDENTIALS_DIR",
    "DEFAULT_PASSPHRASE_FILE",
    "DEFAULT_PRIVATE_KEY_FILE",
    "SnowflakeAddress",
    "SnowflakeConnection",
    "SnowflakeDriver",
    "_pem_to_der",
    "_read_passphrase",
    "_read_private_key_pem",
    "_resolve_credentials_dir",
    "build_address_config",
    "connection_fields",
    "location_fields",
]
