"""Contract: every registered connection type publishes a location spec.

Addresses (W7 shape) are ``{connection, location}`` — the ``location``
half is what the UI form asks the operator for. The catalogue in
:data:`ovid2.connections.registry._LOCATION_SPECS` is what the form
consumes. If a new connector is added to
:data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` without a
matching entry in ``_LOCATION_SPECS``, the address form breaks. This
test refuses that drift.

Pairs with :mod:`tests.contract.test_engine_registry_complete` — same
pattern, different resource.
"""

from __future__ import annotations

import pytest

from ovid2.connections.registry import (
    CONNECTION_TYPE_MAP,
    location_field_spec,
)


@pytest.mark.contract
def test_every_connection_type_has_a_location_spec() -> None:
    """A ``location_field_spec()`` call must succeed for every registered type."""
    missing: list[str] = []
    for type_name in sorted(CONNECTION_TYPE_MAP):
        try:
            location_field_spec(type_name)
        except KeyError:
            missing.append(type_name)
    assert not missing, (
        f"Connection types missing a _LOCATION_SPECS entry: {missing}. "
        "Every entry in CONNECTION_TYPE_MAP must ship a matching "
        "location field spec so the address form can render."
    )


@pytest.mark.contract
def test_location_spec_shape_is_stable() -> None:
    """Every descriptor is a dict with a string ``name`` and a boolean ``required``."""
    for type_name in sorted(CONNECTION_TYPE_MAP):
        spec = location_field_spec(type_name)
        assert isinstance(spec, list), (
            f"location_field_spec('{type_name}') must return a list, "
            f"got {type(spec).__name__}."
        )
        for field in spec:
            assert isinstance(field, dict), (
                f"Connection '{type_name}' has a non-dict location field: {field}"
            )
            assert "name" in field and isinstance(field["name"], str), (
                f"Connection '{type_name}' field is missing a string 'name': {field}"
            )
            assert isinstance(field.get("required", False), bool), (
                f"Connection '{type_name}' field '{field['name']}' has "
                f"non-bool 'required': {field.get('required')!r}"
            )
            field_type = field.get("type", "str")
            assert field_type in {"str", "bool", "int"}, (
                f"Connection '{type_name}' field '{field['name']}' has "
                f"unknown type '{field_type}'. Use one of str/bool/int."
            )


@pytest.mark.contract
def test_no_sensitive_keys_in_location_spec() -> None:
    """Locations must not carry credentials — those live on the connection."""
    forbidden = {
        "password", "passwd", "token", "secret",
        "private_key", "private_key_passphrase",
        "aws_secret_access_key", "aws_access_key_id",
        "service_account_json", "connection_string",
        "account_key", "sas_token", "client_secret",
    }
    for type_name in sorted(CONNECTION_TYPE_MAP):
        for field in location_field_spec(type_name):
            name = str(field["name"]).lower()
            assert name not in forbidden, (
                f"Connection '{type_name}' location field '{field['name']}' "
                "looks like a credential. Move it to the connection's "
                "_FIELD_SPECS['secrets'] list; addresses never carry secrets."
            )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
