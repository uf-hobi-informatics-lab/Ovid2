"""Unit tests for :mod:`ovid2.drivers.access.metadata`."""

from __future__ import annotations

from ovid2.drivers.access.metadata import (
    connection_fields,
    container_requirements_from,
    location_fields,
    normalise_access_args,
    parse_table_list,
)
from ovid2.drivers.base import ConnectionFieldSpec, LocationField
from ovid2.drivers.container import ExtraMount


class TestNormaliseAccessArgs:
    """Widening rules for the access arg triple (default mode ``ro``)."""

    def test_new_shape_round_trips(self) -> None:
        got = normalise_access_args({"host_path": "/a", "container_path": "/b", "mount_mode": "rw"})
        assert got == {
            "host_path": "/a",
            "container_path": "/b",
            "mount_mode": "rw",
        }

    def test_container_defaults_to_host_and_ro(self) -> None:
        got = normalise_access_args({"host_path": "/a"})
        assert got == {
            "host_path": "/a",
            "container_path": "/a",
            "mount_mode": "ro",
        }

    def test_legacy_root_path_widens_ro(self) -> None:
        got = normalise_access_args({"root_path": "/legacy"})
        assert got == {
            "host_path": "/legacy",
            "container_path": "/legacy",
            "mount_mode": "ro",
        }

    def test_invalid_mode_falls_back_to_ro(self) -> None:
        got = normalise_access_args({"host_path": "/a", "mount_mode": "bogus"})
        assert got["mount_mode"] == "ro"

    def test_host_path_wins_over_root_path(self) -> None:
        got = normalise_access_args({"host_path": "/h", "root_path": "/r"})
        assert got["host_path"] == "/h"


class TestFieldSpecs:
    """Field-spec shape sanity."""

    def test_connection_fields_shape(self) -> None:
        spec = connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)
        names = [f.name for f in spec.args]
        assert names == ["host_path", "container_path", "mount_mode"]
        assert spec.secrets == ()
        # host_path is required + uses the path picker widget.
        host = spec.args[0]
        assert host.required is True
        assert host.widget == "path_picker"
        # default mount mode is read-only.
        assert spec.args[2].default == "ro"

    def test_location_fields_shape(self) -> None:
        fields = location_fields()
        assert all(isinstance(f, LocationField) for f in fields)
        names = [f.name for f in fields]
        assert names == [
            "path",
            "tables",
            "exclude_tables",
            "lowercase_columns",
        ]
        assert fields[0].required is True  # path is required


class TestParseTableList:
    """Comma-list parsing for tables / exclude_tables."""

    def test_none_yields_empty(self) -> None:
        assert parse_table_list(None) == ()

    def test_comma_string(self) -> None:
        assert parse_table_list("A, B ,C") == ("A", "B", "C")

    def test_list_input(self) -> None:
        assert parse_table_list(["X", "Y"]) == ("X", "Y")

    def test_dedups_case_insensitively_preserving_first(self) -> None:
        assert parse_table_list("Drug, drug, DRUG, Name") == ("Drug", "Name")

    def test_blanks_dropped(self) -> None:
        assert parse_table_list(" , A, ,") == ("A",)


class TestContainerRequirements:
    """Bind-mount derivation — read-only by default."""

    def test_w7_args_produce_ro_mount(self) -> None:
        reqs = container_requirements_from({"host_path": "/data/refdb"})
        assert reqs == (
            ExtraMount(
                host_path="/data/refdb",
                container_path="/data/refdb",
                mode="ro",
            ),
        )

    def test_distinct_container_path(self) -> None:
        reqs = container_requirements_from(
            {
                "host_path": "/host/multum",
                "container_path": "/mnt/multum",
                "mount_mode": "ro",
            }
        )
        assert reqs == (
            ExtraMount(
                host_path="/host/multum",
                container_path="/mnt/multum",
                mode="ro",
            ),
        )

    def test_legacy_path_to_file_mounts_parent_dir(self) -> None:
        reqs = container_requirements_from({"path": "/data/refdb/drug_reference v2026.accdb"})
        assert reqs == (
            ExtraMount(
                host_path="/data/refdb",
                container_path="/data/refdb",
                mode="ro",
            ),
        )

    def test_legacy_path_to_dir_mounts_dir(self) -> None:
        reqs = container_requirements_from({"path": "/data/refdb"})
        assert reqs == (
            ExtraMount(
                host_path="/data/refdb",
                container_path="/data/refdb",
                mode="ro",
            ),
        )

    def test_empty_args_yield_no_mount(self) -> None:
        assert container_requirements_from({}) == ()
