"""Unit tests for :class:`ovid2.drivers.disc.driver.DiscDriver`."""

from __future__ import annotations

import os
from pathlib import Path

from ovid2.drivers.base import Driver, ProbeResult
from ovid2.drivers.container import ExtraMount
from ovid2.drivers.disc.driver import DiscDriver
from ovid2.drivers.registry import DRIVERS, instantiate, load_class


class TestNativeRegistration:
    """After D3.disc, ``DRIVERS['disc']`` must point at :class:`DiscDriver`.

    The legacy adapter is no longer authoritative for this type — its
    ``register_all_legacy_drivers`` skips ``disc`` because the native
    module self-registers first.
    """

    def test_registry_entry_is_native(self) -> None:
        assert DRIVERS.get("disc") == "ovid2.drivers.disc.driver.DiscDriver"

    def test_load_class_returns_disc_driver(self) -> None:
        cls = load_class("disc")
        assert cls is DiscDriver
        assert issubclass(cls, Driver)


class TestDiscDriverProbes:
    def test_probe_auth_ok_on_readable(self, tmp_path: Path) -> None:
        d = DiscDriver(args={"host_path": str(tmp_path), "mount_mode": "rw"})
        r = d.probe_auth()
        assert isinstance(r, ProbeResult)
        assert r.ok is True
        assert r.capability == "auth"

    def test_probe_read_ok_on_listable(self, tmp_path: Path) -> None:
        d = DiscDriver(args={"host_path": str(tmp_path)})
        assert d.probe_read().ok is True

    def test_probe_write_ok_on_writable(self, tmp_path: Path) -> None:
        d = DiscDriver(args={"host_path": str(tmp_path), "mount_mode": "rw"})
        assert d.probe_write().ok is True

    def test_probe_auth_config_when_no_host_path(self) -> None:
        d = DiscDriver(args={})
        assert d.probe_auth().code == "config"


class TestContainerRequirements:
    def test_returns_extra_mount(self, tmp_path: Path) -> None:
        d = DiscDriver(args={"host_path": str(tmp_path), "mount_mode": "rw"})
        reqs = d.container_requirements_for({})
        assert len(reqs) == 1
        m = reqs[0]
        assert isinstance(m, ExtraMount)
        assert m.host_path == str(tmp_path)
        assert m.container_path == str(tmp_path)

    def test_container_path_override(self, tmp_path: Path) -> None:
        d = DiscDriver(args={
            "host_path": str(tmp_path),
            "container_path": "/inside",
            "mount_mode": "ro",
        })
        reqs = d.container_requirements_for({})
        assert reqs[0].host_path == str(tmp_path)
        assert reqs[0].container_path == "/inside"
        assert reqs[0].mode == "ro"

    def test_empty_args_returns_empty(self) -> None:
        d = DiscDriver(args={})
        assert d.container_requirements_for({}) == ()


class TestLegacyBridge:
    def test_as_legacy_src_args_combines_root_and_sub_path(self, tmp_path: Path) -> None:
        d = DiscDriver(
            args={"host_path": str(tmp_path), "mount_mode": "rw"},
            location={"path": "subdir"},
        )
        src, args = d.as_legacy_src_args()
        assert src == "disc"
        assert args["path"] == os.path.join(str(tmp_path), "subdir")

    def test_as_legacy_src_args_forwards_format_and_lowercase(
        self, tmp_path: Path,
    ) -> None:
        d = DiscDriver(
            args={"host_path": str(tmp_path)},
            location={"path": "s", "format": "csv", "lowercase_columns": True},
        )
        _, args = d.as_legacy_src_args()
        assert args["format"] == "csv"
        assert args["lowercase_columns"] is True

    def test_container_path_wins_over_host_in_pipeline_view(
        self, tmp_path: Path,
    ) -> None:
        d = DiscDriver(
            args={
                "host_path": str(tmp_path),
                "container_path": "/inside",
            },
            location={"path": "sub"},
        )
        _, args = d.as_legacy_src_args()
        assert args["path"] == "/inside/sub"


class TestInstantiateViaRegistry:
    def test_registry_produces_disc_driver(self, tmp_path: Path) -> None:
        d = instantiate("disc", args={"host_path": str(tmp_path), "mount_mode": "rw"})
        assert isinstance(d, DiscDriver)
        assert d.probe_auth().ok is True


class TestMergedLocationView:
    """The internal helper that produces ``{root, format, lowercase_columns}``.

    Not a public method but the data-plane calls it, so a lightweight
    contract test locks in the shape.
    """

    def test_root_defaults_to_container_path(self, tmp_path: Path) -> None:
        d = DiscDriver(args={"host_path": str(tmp_path)}, location={"path": "sub"})
        view = d._merged_location_view()
        assert view["root"] == os.path.join(str(tmp_path), "sub")
        assert view["format"] == "csv"  # default
        assert view["lowercase_columns"] is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
