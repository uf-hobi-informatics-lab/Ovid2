"""Advisory type stamping for driver-based routes.

Under D6.0 the driver protocol grows an optional
:meth:`Driver.declared_cdm` classmethod that returns the CDM stamped
on an address's location, when the driver knows one. This module
consumes those declarations and produces:

- :func:`declared_cdm_for_address` — a helper that resolves an
  address name to its declared CDM (or ``None``).
- :func:`validate_route_types` — an advisory route validator that
  emits a :mod:`logging` warning when a route's source and target
  addresses declare different CDMs.

Strict enforcement (refusing to launch on mismatch) is opt-in via
``strict_types: true`` on the route. The default is advisory-only
so pipelines with legacy address JSONs that lack a ``cdm`` field
continue to run unchanged.

Design rationale
----------------

Route validation lives here — not in :mod:`ovid2.web.services.pipelines_svc`
— because the driver registry is the source of truth for what a
peripheral knows about its own type. Keeping the check on this side
means:

- The web tier's route validator gets uniform behaviour without
  having to know per-type schema conventions (Snowflake looks up
  ``cdm``; a hypothetical Kafka driver might look up ``topic_schema``).
- Native driver ports (D3.<type>) can override ``declared_cdm`` to
  add richer logic (e.g. resolve a schema URL, consult a semantic
  binding) without touching the web tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TypeMismatch:
    """One advisory type-mismatch finding.

    Emitted by :func:`validate_route_types` when the source and
    target address of a route declare different CDMs. Callers may
    choose to escalate to an error (strict mode) or ignore.
    """

    route_name: str
    from_address: str
    to_address: str
    from_cdm: str | None
    to_cdm: str | None

    def message(self) -> str:
        """Return a human-readable one-liner for logs and UI."""
        return (
            f"Route '{self.route_name}': source '{self.from_address}' "
            f"declares CDM {self.from_cdm!r} but target "
            f"'{self.to_address}' declares CDM {self.to_cdm!r}."
        )


def declared_cdm_for_address(address_name: str) -> str | None:
    """Return the CDM the address's driver declares, or ``None``.

    Walks the same path as :mod:`ovid2.drivers.collector`:

    1. Load the address JSON.
    2. Resolve to a driver via the D2 registry.
    3. Ask the driver via :meth:`Driver.declared_cdm`.

    Returns:
        The declared CDM name (e.g. ``"pcornet_7_0"``), or ``None``
        when the driver doesn't stamp one — either because the type
        never carries CDM info (disc, s3, azure_blob, gcs) or
        because the specific address's location doesn't populate
        the ``cdm`` field.
    """
    from ovid2.config.addresses import load_address_config
    from ovid2.config.connections import load_connection_config
    from ovid2.drivers.registry import DRIVERS, load_class

    try:
        payload = load_address_config(address_name)
    except (FileNotFoundError, ValueError):
        return None

    shape = payload.get("_shape")
    if shape == "connection":
        connection_name = str(payload.get("connection") or "")
        if not connection_name:
            return None
        try:
            conn_raw = load_connection_config(connection_name)
        except (FileNotFoundError, ValueError):
            return None
        type_name = str(conn_raw.get("type") or "")
        location = dict(payload.get("location") or {})
    else:
        type_name = str(payload.get("src_name") or "")
        # Legacy addresses store the CDM (if any) directly under
        # ``src_args`` — the pre-C1 Snowflake convention — so we
        # forward the whole args block as the location for
        # ``declared_cdm``.
        location = dict(payload.get("src_args") or {})

    if type_name not in DRIVERS:
        return None
    try:
        cls = load_class(type_name)
    except (KeyError, ImportError, AttributeError):
        return None
    try:
        return cls.declared_cdm(location)
    except Exception:
        return None


def validate_route_types(
    route_name: str,
    from_address: str,
    to_address: str,
    *,
    strict: bool = False,
) -> TypeMismatch | None:
    """Compare the declared CDMs of a route's source and target.

    Args:
        route_name: Route name (for the log line).
        from_address: Source address name.
        to_address: Target address name.
        strict: When ``True``, a mismatch also raises
            :class:`ValueError` (opt-in; D6.1 will wire this to the
            ``strict_types`` route field). When ``False`` (default),
            the mismatch is logged as a warning and returned.

    Returns:
        A :class:`TypeMismatch` describing the finding when the two
        addresses declare different CDMs, or ``None`` when they
        agree (or when at least one declares ``None`` — in
        advisory mode we treat "unknown" as compatible so the
        legacy pipeline corpus continues to run).
    """
    from_cdm = declared_cdm_for_address(from_address)
    to_cdm = declared_cdm_for_address(to_address)

    if from_cdm is None or to_cdm is None:
        return None
    if from_cdm == to_cdm:
        return None

    finding = TypeMismatch(
        route_name=route_name,
        from_address=from_address,
        to_address=to_address,
        from_cdm=from_cdm,
        to_cdm=to_cdm,
    )
    logger.warning("Type mismatch: %s", finding.message())
    if strict:
        raise ValueError(finding.message())
    return finding


__all__ = [
    "TypeMismatch",
    "declared_cdm_for_address",
    "validate_route_types",
]
