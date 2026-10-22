"""Contract: :data:`DRIVERS` covers every legacy connection type.

Post-D2 the driver registry is the source of truth. But the legacy
:data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` still exists
(and is even referenced by contract tests that predate the refactor).
This test enforces the invariant that every legacy type has a matching
driver registered — either a :class:`LegacyDriverAdapter` shim
(D2 → D3.<type>) or a native driver class (post-D3).

If this test fails, the driver-refactor migration has left a legacy
type without an entry in :data:`DRIVERS`, which means the web tier
(which is being migrated to consume :data:`DRIVERS`) will list one
fewer connector than the legacy tier.
"""

from __future__ import annotations

from ovid2.connections.registry import CONNECTION_TYPE_MAP
from ovid2.drivers.registry import DRIVERS


def test_every_legacy_connection_type_has_a_driver() -> None:
    """DRIVERS must be a superset of CONNECTION_TYPE_MAP."""
    missing = set(CONNECTION_TYPE_MAP) - set(DRIVERS)
    assert not missing, (
        "Every entry in CONNECTION_TYPE_MAP must have a matching entry in "
        f"DRIVERS (either a native driver or a LegacyDriverAdapter). "
        f"Missing: {sorted(missing)}. Ensure "
        "ovid2.drivers.legacy_adapter.register_all_legacy_drivers() "
        "ran at import time."
    )


def test_no_orphan_drivers() -> None:
    """DRIVERS must not exceed CONNECTION_TYPE_MAP + a small allow-list.

    Any DRIVERS entry that has no legacy counterpart is either:

    - A native driver added *after* the corresponding legacy connector
      class was deleted (post-D3 state — legal).
    - A stale registration from a bad merge (illegal).

    Until every legacy connector has been migrated, the allow-list is
    empty. Update it in lock-step with each D3 migration.
    """
    # As of D2, every entry in DRIVERS should correspond to a legacy
    # connection type. D3.<type> migrations may delete the legacy class
    # AND its CONNECTION_TYPE_MAP entry in the same commit — at that
    # point this list must be updated.
    allowed_orphans: set[str] = set()

    orphans = set(DRIVERS) - set(CONNECTION_TYPE_MAP) - allowed_orphans
    assert not orphans, (
        f"Orphan DRIVERS entries with no legacy counterpart: {sorted(orphans)}. "
        "Either add them to CONNECTION_TYPE_MAP, delete them from DRIVERS, "
        "or extend the allow-list in this test if this is a post-D3 "
        "native-only driver."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Contract test; run with pytest.")
