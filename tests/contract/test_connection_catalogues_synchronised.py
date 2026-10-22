"""Contract: the four connection/address catalogues must agree on type names.

Adding a new peripheral (S3, Azure, a hypothetical Kafka source) today
requires touching **five** hand-maintained dicts across two modules:

- ``ovid2.connections.registry.CONNECTION_TYPE_MAP``
- ``ovid2.connections.registry._FIELD_SPECS``
- ``ovid2.connections.registry._LOCATION_SPECS``
- ``ovid2.config.addresses.ADDRESS_TYPE_MAP``
- ``ovid2.config.addresses.CONNECTION_TO_SRC_NAME``

Forgetting any one causes a **runtime** failure, sometimes hours into a
pipeline run. The canonical repro is the ``S3-Testing`` bug: the S3
connection existed, the address JSON pointed at it, but
``ADDRESS_TYPE_MAP`` had no ``s3`` entry, so
``ovid2 tools copy … --to S3-Testing`` raised
``ValueError: Address 'S3-Testing' targets connection type 's3' which
has no matching legacy address class yet.``

This test is the pre-Driver-refactor safety net: it fails at commit
time when any of the five catalogues drift out of sync. It will remain
useful even after the D1–D5 refactor collapses these catalogues,
because the derived views must still expose the same key set.

Notes on the invariant
----------------------

- ``CONNECTION_TYPE_MAP`` is the source of truth for connector types.
- ``_FIELD_SPECS`` and ``_LOCATION_SPECS`` must cover every type in
  ``CONNECTION_TYPE_MAP``. Extra entries are also disallowed — every
  UI-consumed catalogue must round-trip to a registered class.
- ``ADDRESS_TYPE_MAP`` covers the pipeline-side address class for a
  connection type. It is legal for a connection type to have **no**
  pipeline-side address (control-plane only), but if it does, the two
  maps must agree.
- ``CONNECTION_TO_SRC_NAME`` is the bridge for W7 shape addresses. It
  must have an entry for every ``ADDRESS_TYPE_MAP`` key (otherwise the
  new-shape loader can't dereference the address to its class).
"""

from __future__ import annotations

from ovid2.config.addresses import ADDRESS_TYPE_MAP, CONNECTION_TO_SRC_NAME
from ovid2.connections.registry import (
    CONNECTION_TYPE_MAP,
    connection_field_spec,
    location_field_spec,
)


def _connection_types() -> set[str]:
    return set(CONNECTION_TYPE_MAP.keys())


def _field_spec_types() -> set[str]:
    types: set[str] = set()
    for t in CONNECTION_TYPE_MAP:
        try:
            connection_field_spec(t)
        except KeyError:
            continue
        types.add(t)
    return types


def _location_spec_types() -> set[str]:
    types: set[str] = set()
    for t in CONNECTION_TYPE_MAP:
        try:
            location_field_spec(t)
        except KeyError:
            continue
        types.add(t)
    return types


def test_every_connection_type_has_a_connection_field_spec() -> None:
    """Every type in ``CONNECTION_TYPE_MAP`` must have a field spec."""
    conn_types = _connection_types()
    field_types = _field_spec_types()
    missing = conn_types - field_types
    assert not missing, (
        "Every connection type in CONNECTION_TYPE_MAP must have a matching "
        f"entry in _FIELD_SPECS. Missing: {sorted(missing)}. Add the field "
        "spec in ovid2.connections.registry._FIELD_SPECS."
    )


def test_every_connection_type_has_a_location_field_spec() -> None:
    """Every type in ``CONNECTION_TYPE_MAP`` must have a location spec."""
    conn_types = _connection_types()
    loc_types = _location_spec_types()
    missing = conn_types - loc_types
    assert not missing, (
        "Every connection type in CONNECTION_TYPE_MAP must have a matching "
        f"entry in _LOCATION_SPECS. Missing: {sorted(missing)}. Add the "
        "location spec in ovid2.connections.registry._LOCATION_SPECS."
    )


def test_address_type_map_subset_of_connection_type_map() -> None:
    """``ADDRESS_TYPE_MAP`` keys must all be registered connection types."""
    conn_types = _connection_types()
    address_types = set(ADDRESS_TYPE_MAP.keys())
    extra = address_types - conn_types
    assert not extra, (
        "ADDRESS_TYPE_MAP has entries that are not registered as connection "
        f"types: {sorted(extra)}. Either add them to CONNECTION_TYPE_MAP or "
        "remove them from ADDRESS_TYPE_MAP."
    )


def test_connection_to_src_name_covers_address_type_map() -> None:
    """Every pipeline-side address type must be reachable from a connection.

    ``CONNECTION_TO_SRC_NAME`` is the bridge the new-shape address loader
    (``{connection, location}``) uses to pick the concrete
    :class:`BaseAddress` implementation. Without an entry here, a W7
    address referencing an ``ADDRESS_TYPE_MAP`` type still raises the
    ``no matching legacy address class yet`` ValueError — the exact
    ``S3-Testing`` bug this test exists to prevent.
    """
    address_types = set(ADDRESS_TYPE_MAP.keys())
    bridge_types = set(CONNECTION_TO_SRC_NAME.keys())
    missing = address_types - bridge_types
    assert not missing, (
        "Every ADDRESS_TYPE_MAP entry must have a CONNECTION_TO_SRC_NAME "
        f"entry so W7-shape addresses can dereference. Missing: {sorted(missing)}."
    )


def test_connection_to_src_name_targets_valid_address_types() -> None:
    """Every ``CONNECTION_TO_SRC_NAME`` value must be a real address type."""
    address_types = set(ADDRESS_TYPE_MAP.keys())
    for conn_type, src_name in CONNECTION_TO_SRC_NAME.items():
        assert src_name in address_types, (
            f"CONNECTION_TO_SRC_NAME[{conn_type!r}] = {src_name!r} but "
            f"{src_name!r} is not in ADDRESS_TYPE_MAP. "
            f"Registered address types: {sorted(address_types)}."
        )


def test_field_spec_has_no_extras_beyond_connection_types() -> None:
    """A stale ``_FIELD_SPECS`` entry (leftover from a removed connector)
    is a latent bug — the UI would render a form for a type that can't
    be instantiated. Detect and refuse."""
    # We only compare against types we actually queried above; a spec that
    # doesn't correspond to a real connection type would still be caught by
    # ``list_connection_types`` returning the registry's key set.
    from ovid2.connections.registry import _FIELD_SPECS  # type: ignore[attr-defined]

    conn_types = _connection_types()
    extra = set(_FIELD_SPECS.keys()) - conn_types
    assert not extra, (
        "_FIELD_SPECS has entries for types that are not registered in "
        f"CONNECTION_TYPE_MAP: {sorted(extra)}. Either register the class "
        "or remove the stale field spec."
    )


def test_location_spec_has_no_extras_beyond_connection_types() -> None:
    """Same rule as above, for the location catalogue."""
    from ovid2.connections.registry import _LOCATION_SPECS  # type: ignore[attr-defined]

    conn_types = _connection_types()
    extra = set(_LOCATION_SPECS.keys()) - conn_types
    assert not extra, (
        "_LOCATION_SPECS has entries for types that are not registered in "
        f"CONNECTION_TYPE_MAP: {sorted(extra)}. Either register the class "
        "or remove the stale location spec."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Contract test; run with pytest.")
