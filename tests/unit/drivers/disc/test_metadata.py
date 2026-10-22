"""Unit tests for :mod:`ovid2.drivers.disc.metadata`."""

from __future__ import annotations

from ovid2.drivers.base import ConnectionFieldSpec, LocationField
from ovid2.drivers.container import ExtraMount
from ovid2.drivers.disc.metadata import (
    connection_fields,
    container_requirements_from,
    location_fields,
    normalise_disc_args,
)


class TestNormaliseDiscArgs:
    """Widening rules for the disc arg triple.

    Same behavioural contract as the pre-D3.disc
    :func:`ovid2.connections.disc.normalise_disc_args`; kept here so
    the disc-mount / path-picker regression tests keep passing
    verbatim.
    """

    def test_new_shape_round_trips(self) -> None:
        got = normalise_disc_args({
            "host_path": "/a", "container_path": "/b", "mount_mode": "ro",
        })
        assert got == {"host_path": "/a", "container_path": "/b", "mount_mode": "ro"}

    def test_container_defaults_to_host(self) -> None:
        got = normalise_disc_args({"host_path": "/a"})
        assert got == {"host_path": "/a", "container_path": "/a", "mount_mode": "rw"}

    def test_legacy_root_path_widens(self) -> None:
        got = normalise_disc_args({"root_path": "/legacy"})
        assert got == {
            "host_path": "/legacy",
            "container_path": "/legacy",
            "mount_mode": "rw",
        }

    def test_host_wins_over_root_path(self) -> None:
        got = normalise_disc_args({"host_path": "/new", "root_path": "/old"})
        assert got["host_path"] == "/new"
        assert got["container_path"] == "/new"

    def test_bad_mount_mode_falls_back_to_rw(self) -> None:
        got = normalise_disc_args({"host_path": "/x", "mount_mode": "chaos"})
        assert got["mount_mode"] == "rw"

    def test_all_missing_returns_empty_host(self) -> None:
        got = normalise_disc_args({})
        assert got == {"host_path": "", "container_path": "", "mount_mode": "rw"}


class TestConnectionFields:
    def test_returns_typed_spec(self) -> None:
        spec = connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)

    def test_args_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.args]
        assert names == ["host_path", "container_path", "mount_mode"]

    def test_host_path_uses_path_picker_widget(self) -> None:
        spec = connection_fields()
        host = next(f for f in spec.args if f.name == "host_path")
        assert host.required is True
        assert host.widget == "path_picker"

    def test_no_secret_fields(self) -> None:
        spec = connection_fields()
        assert spec.secrets == ()

    def test_note_mentions_managed_mount(self) -> None:
        spec = connection_fields()
        assert spec.note is not None
        assert "auto-generate" in spec.note.lower() or "bind mount" in spec.note.lower()


class TestLocationFields:
    def test_returns_typed_tuple(self) -> None:
        fields = location_fields()
        assert isinstance(fields, tuple)
        for f in fields:
            assert isinstance(f, LocationField)

    def test_names_match_legacy(self) -> None:
        names = [f.name for f in location_fields()]
        assert names == ["path", "format", "lowercase_columns", "single_file"]

    def test_format_has_parquet_default(self) -> None:
        fields = location_fields()
        fmt = next(f for f in fields if f.name == "format")
        assert fmt.default == "parquet"

    def test_lowercase_columns_is_bool_widget(self) -> None:
        fields = location_fields()
        f = next(f for f in fields if f.name == "lowercase_columns")
        assert f.widget == "bool"


class TestContainerRequirementsFrom:
    def test_w7_shape_produces_extra_mount(self) -> None:
        reqs = container_requirements_from({"host_path": "/a", "mount_mode": "rw"})
        assert len(reqs) == 1
        m = reqs[0]
        assert isinstance(m, ExtraMount)
        assert m.host_path == "/a"
        assert m.container_path == "/a"
        assert m.mode == "rw"

    def test_container_path_override_survives(self) -> None:
        reqs = container_requirements_from({
            "host_path": "/on-host", "container_path": "/inside", "mount_mode": "ro",
        })
        assert reqs[0].container_path == "/inside"
        assert reqs[0].mode == "ro"

    def test_legacy_root_path_widens(self) -> None:
        reqs = container_requirements_from({"root_path": "/legacy"})
        assert reqs[0].host_path == "/legacy"
        assert reqs[0].container_path == "/legacy"

    def test_pre_w7_address_shape_produces_mirror_mount(self) -> None:
        """``{path: /x}`` (no host_path / root_path) → mirror mount."""
        reqs = container_requirements_from({"path": "/pre_w7"})
        assert len(reqs) == 1
        assert reqs[0].host_path == "/pre_w7"
        assert reqs[0].container_path == "/pre_w7"

    def test_empty_args_returns_empty_tuple(self) -> None:
        assert container_requirements_from({}) == ()

    def test_empty_path_in_pre_w7_shape_returns_empty(self) -> None:
        assert container_requirements_from({"path": ""}) == ()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
