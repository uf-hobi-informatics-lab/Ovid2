"""Legacy connection + address re-exports for the Snowflake driver.

Keeps the pre-D3.snowflake :class:`BaseConnection` /
:class:`BaseAddress` shape alive for the two callers that still need
them:

- :mod:`ovid2.config.connections` — instantiates a connection via
  :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` (dotted path).
- :mod:`ovid2.config.addresses` — instantiates an address via
  :data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` (dotted path).

Both catalogues are retargeted to point at the classes in this module
so every existing import site
(``from ovid2.connections.snowflake import SnowflakeConnection`` or
``from ovid2.core.addresses.snowflake import SnowflakeAddress``)
becomes a one-liner rewrite
``from ovid2.drivers.snowflake import SnowflakeConnection`` /
``from ovid2.drivers.snowflake import SnowflakeAddress`` with no
behaviour change.

Preserves the pre-D3.snowflake private-method surface
(``_build_connect_kwargs`` alias, ``build_connect_kwargs``) that the
existing unit-test suite reads. The address class is re-exported
verbatim from :mod:`ovid2.drivers.snowflake.spark` — it already
implements the ``BaseAddress`` contract directly (there is no thin
shim like ``AzureBlobAddress`` has, because the pre-refactor address
class was already the pipeline's own read/write implementation).
"""

from __future__ import annotations

from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult
from ovid2.drivers.snowflake.metadata import build_address_config
from ovid2.drivers.snowflake.probe import (
    build_connect_kwargs,
    probe_auth_from_config,
)

# Re-export the pipeline-side address class verbatim so the two
# import paths (``ovid2.drivers.snowflake`` and the retargeted
# ``ADDRESS_TYPE_MAP["snowflake"]``) resolve to the same class.
from ovid2.drivers.snowflake.spark import SnowflakeAddress


class SnowflakeConnection(BaseConnection):
    """Snowflake account endpoint.

    The private-key material is **not** stored in the panel's
    ``secrets.d/`` files. Operators point the connection at existing
    key + passphrase files on disk (typically ``/usr/include/rsa/``
    on Ovid servers) and the runtime reads + decodes them
    just-in-time. The only actual secret the panel captures is
    ``password``, used when the account authenticates by password
    rather than key-pair.

    Preserves the pre-D3.snowflake private-method surface
    (``build_connect_kwargs`` + its underscore alias) that the
    existing unit-test suite reads.
    """

    type_name = "snowflake"

    # ------------------------------------------------------------------
    # Internal helpers (preserved public surface — tests read these)
    # ------------------------------------------------------------------

    def build_connect_kwargs(self) -> dict[str, Any]:
        """Return the kwargs dict to pass to ``snowflake.connector.connect``.

        Delegates to
        :func:`~ovid2.drivers.snowflake.probe.build_connect_kwargs` so
        the file-read + PEM/DER dance is defined in exactly one place.
        """
        return build_connect_kwargs(dict(self.args), dict(self.secrets))

    #: Back-compat alias — older code (and the pre-D3.snowflake
    #: ``test()`` implementation) called this by its underscore name.
    _build_connect_kwargs = build_connect_kwargs

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    def test(self) -> ConnectionTestResult:
        """Attempt a ``SELECT 1`` round-trip via ``snowflake-connector-python``."""
        probe = probe_auth_from_config(dict(self.args), dict(self.secrets))
        return ConnectionTestResult(
            ok=probe.ok,
            code=probe.code,
            message=probe.message,
            detail=dict(probe.detail),
        )

    # ------------------------------------------------------------------
    # Legacy-address bridge
    # ------------------------------------------------------------------

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Return the ``src_args`` dict a legacy Snowflake address expects."""
        return build_address_config(
            dict(self.args),
            dict(self.secrets),
            dict(location),
        )


__all__ = ["SnowflakeAddress", "SnowflakeConnection"]
