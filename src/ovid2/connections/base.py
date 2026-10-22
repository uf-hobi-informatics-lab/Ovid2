"""Base connector interface + shared value objects.

Every concrete connector subclasses :class:`BaseConnection` and
implements two capabilities:

- ``test()`` — a cheap round-trip probe (e.g. ``HEAD`` on a bucket,
  ``SELECT 1`` on a database) used by the *Test connection* button in
  the web control panel.
- ``as_address_config(location)`` — return the ``src_args`` dict a
  legacy :class:`ovid2.core.addresses.base.BaseAddress` implementation
  expects, given a per-address location payload (path, database,
  schema, bucket, container, ...).

The second method is the bridge that lets the pipeline keep using the
existing ``BaseAddress`` machinery unchanged: at run time the loader
merges the connection's credentials with the address's location to
build the exact ``src_args`` the old classes were already consuming.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectionTestResult:
    """Structured outcome of a connection probe.

    ``ok`` is the coarse success flag used by the UI badge; ``code``
    is a short machine token (``ok``, ``unreachable``, ``auth``,
    ``timeout``, ``not_implemented``, ``skipped``) that lets automated
    callers branch on the specific failure mode without regex-matching
    the ``message`` string.
    """

    ok: bool
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


class BaseConnection(ABC):
    """Abstract base class for reusable endpoints.

    Subclasses are constructed with an ``args`` dict (the non-secret
    JSON body from the connection file) and an optional ``secrets``
    dict resolved from :mod:`ovid2.config.secrets` via ``secret_ref``.
    Neither dict is mutated after construction.
    """

    #: Short machine name used by the registry (``disc``, ``snowflake``, …).
    type_name: str = ""

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        """Store the raw configuration for later use.

        Args:
            args: Non-secret arguments (host, region, warehouse, …).
            secrets: Secret material (password, token, private key, …).
                May be ``None`` for connectors that do not need any
                (e.g. ``disc`` on a local filesystem).
        """
        self.args: dict[str, Any] = dict(args or {})
        self.secrets: dict[str, Any] = dict(secrets or {})

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    def test(self) -> ConnectionTestResult:
        """Attempt a cheap round-trip probe.

        Default implementation returns a ``not_implemented`` result so
        connectors can be dropped in without a working probe. Real
        connectors override this.
        """
        return ConnectionTestResult(
            ok=False,
            code="not_implemented",
            message=f"Test probe not implemented for connector '{self.type_name}'.",
        )

    # ------------------------------------------------------------------
    # Legacy-address bridge
    # ------------------------------------------------------------------

    @abstractmethod
    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Return the ``src_args`` dict a legacy address class expects.

        Args:
            location: Per-address arguments (``path``, ``database``,
                ``schema``, ``bucket``, ``container``, ``format``, …).

        Returns:
            The merged dict passed straight through to a
            :class:`ovid2.core.addresses.base.BaseAddress` constructor.
        """
