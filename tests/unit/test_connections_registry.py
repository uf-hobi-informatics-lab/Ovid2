"""Unit tests for the connections registry location-spec catalogue."""

from __future__ import annotations

import pytest

from ovid2.connections.registry import (
    CONNECTION_TYPE_MAP,
    connection_field_spec,
    location_field_spec,
)


@pytest.mark.unit
def test_location_spec_returns_a_list_of_dicts() -> None:
    """Every registered connector must return a list of field descriptors."""
    for type_name in CONNECTION_TYPE_MAP:
        spec = location_field_spec(type_name)
        assert isinstance(spec, list), f"{type_name!r} spec is not a list"
        for field in spec:
            assert isinstance(field, dict)
            assert isinstance(field.get("name"), str)


@pytest.mark.unit
def test_location_spec_is_a_shallow_copy() -> None:
    """Mutating the returned list must not affect subsequent calls."""
    spec = location_field_spec("disc")
    spec.append({"name": "polluted", "required": False})
    assert "polluted" not in {f["name"] for f in location_field_spec("disc")}


@pytest.mark.unit
def test_disc_location_has_path_field() -> None:
    """disc addresses view a sub-path off the connection's root."""
    spec = location_field_spec("disc")
    names = {f["name"] for f in spec}
    assert "path" in names
    assert "format" in names


@pytest.mark.unit
def test_snowflake_location_requires_database() -> None:
    """Snowflake addresses must scope to a database at minimum."""
    spec = location_field_spec("snowflake")
    by_name = {f["name"]: f for f in spec}
    assert "database" in by_name
    assert by_name["database"].get("required") is True
    assert "schema" in by_name
    assert by_name["schema"].get("required") is False


@pytest.mark.unit
def test_object_store_specs_share_shape() -> None:
    """s3 / gcs / azure_blob all expose bucket-or-container + path + format."""
    for type_name, container_key in (
        ("s3", "bucket"),
        ("gcs", "bucket"),
        ("azure_blob", "container"),
    ):
        spec = location_field_spec(type_name)
        names = {f["name"] for f in spec}
        assert container_key in names, f"{type_name!r} missing {container_key!r}"
        assert "path" in names, f"{type_name!r} missing 'path'"
        assert "format" in names, f"{type_name!r} missing 'format'"


@pytest.mark.unit
def test_database_specs_expose_schema() -> None:
    """postgres / mssql / databricks all expose an optional schema field."""
    for type_name in ("postgres", "mssql", "databricks"):
        spec = location_field_spec(type_name)
        names = {f["name"] for f in spec}
        assert "schema" in names, f"{type_name!r} missing 'schema'"


@pytest.mark.unit
def test_unknown_type_raises() -> None:
    """Asking for a type outside CONNECTION_TYPE_MAP is a KeyError."""
    with pytest.raises(KeyError, match="No location spec"):
        location_field_spec("not_a_real_connector")


@pytest.mark.unit
def test_no_location_field_names_overlap_with_secret_names() -> None:
    """Location keys and secret keys should never collide on a connector.

    Regression guard: if someone adds ``password`` to a location spec
    the address form would happily persist credentials in the address
    file — precisely the pre-W7 mistake this refactor undoes.
    """
    for type_name in CONNECTION_TYPE_MAP:
        loc_names = {f["name"] for f in location_field_spec(type_name)}
        spec = connection_field_spec(type_name)
        secret_names = {f["name"] for f in spec["secrets"]}
        overlap = loc_names & secret_names
        assert not overlap, (
            f"Connector '{type_name}' has fields shared between "
            f"location and secrets: {sorted(overlap)}"
        )
