"""Unit tests for :class:`ovid2.drivers.access.driver.AccessDriver`."""

from __future__ import annotations

import sys
import types

import pytest

from ovid2.drivers.access.driver import AccessDriver
from ovid2.drivers.base import StorageEntry
from ovid2.drivers.container import ExtraMount
from ovid2.drivers.registry import DRIVERS, instantiate


def _install_fake_spark(monkeypatch, tables, captured):
    """Install a fake ``ovid2.drivers.access.spark`` module.

    Records the read arguments in ``captured`` and returns ``tables``
    from ``list_tables`` so tests avoid needing a real JVM.
    """
    fake = types.ModuleType("ovid2.drivers.access.spark")

    def list_tables(session, path):
        captured["list_path"] = path
        return list(tables)

    def read_table(session, path, table_name, lowercase_columns):
        captured["read"] = {
            "path": path,
            "table_name": table_name,
            "lowercase_columns": lowercase_columns,
        }
        return f"DF({table_name})"

    def get_active_session():
        return object()

    fake.list_tables = list_tables
    fake.read_table = read_table
    fake.get_active_session = get_active_session
    monkeypatch.setitem(sys.modules, "ovid2.drivers.access.spark", fake)


class TestRegistration:
    def test_registered_native(self) -> None:
        assert DRIVERS["access"] == "ovid2.drivers.access.driver.AccessDriver"

    def test_instantiate(self) -> None:
        d = instantiate("access", {"host_path": "/tmp"}, {}, {"path": "x.accdb"})
        assert isinstance(d, AccessDriver)
        assert d.type_name == "access"


class TestContainerRequirements:
    def test_ro_mount_for_connection_dir(self) -> None:
        d = AccessDriver({"host_path": "/data/multum"}, {}, {"path": "v.accdb"})
        assert d.container_requirements_for() == (
            ExtraMount(
                host_path="/data/multum",
                container_path="/data/multum",
                mode="ro",
            ),
        )


class TestPathResolution:
    def test_container_path_joins_location(self) -> None:
        d = AccessDriver(
            {"host_path": "/host", "container_path": "/mnt"},
            {},
            {"path": "sub/v.accdb"},
        )
        assert d._container_file_path() == "/mnt/sub/v.accdb"

    def test_host_path_used_for_probe(self) -> None:
        d = AccessDriver(
            {"host_path": "/host", "container_path": "/mnt"},
            {},
            {"path": "v.accdb"},
        )
        assert d._host_file_path() == "/host/v.accdb"


class TestListDir:
    def test_directory_lists_access_files_as_navigable(self, tmp_path) -> None:
        (tmp_path / "a.accdb").write_bytes(b"x")
        (tmp_path / "b.mdb").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("skip me")
        (tmp_path / "sub").mkdir()
        d = AccessDriver({"host_path": str(tmp_path)}, {}, {})
        entries = d.list_dir()
        names = {e.name: e.kind for e in entries}
        assert names["a.accdb"] == "dir"
        assert names["b.mdb"] == "dir"
        assert names["sub"] == "dir"
        assert "notes.txt" not in names  # non-access files hidden

    def test_file_lists_tables(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "v.accdb"
        f.write_bytes(b"x")
        captured: dict = {}
        _install_fake_spark(monkeypatch, ["Drugs", "Ndc"], captured)
        d = AccessDriver({"host_path": str(tmp_path)}, {}, {})
        entries = d.list_dir("v.accdb")
        assert all(isinstance(e, StorageEntry) for e in entries)
        assert [e.kind for e in entries] == ["table", "table"]
        assert [e.name for e in entries] == ["Drugs", "Ndc"]
        # path round-trips as file::table
        assert entries[0].path == "v.accdb::Drugs"

    def test_escape_rejected(self, tmp_path) -> None:
        d = AccessDriver({"host_path": str(tmp_path)}, {}, {})
        with pytest.raises(ValueError, match="escapes"):
            d.list_dir("../..")


class TestTablesToRead:
    def test_all_tables_by_default(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_spark(monkeypatch, ["Drugs", "Ndc", "Rxcui"], captured)
        d = AccessDriver({"host_path": "/h", "container_path": "/c"}, {}, {"path": "v.accdb"})
        assert d.tables_to_read(object()) == ["Drugs", "Ndc", "Rxcui"]
        assert captured["list_path"] == "/c/v.accdb"

    def test_allow_list_filters(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_spark(monkeypatch, ["Drugs", "Ndc", "Rxcui"], captured)
        d = AccessDriver(
            {"host_path": "/h"}, {}, {"path": "v.accdb", "tables": "ndc, rxcui"}
        )
        assert d.tables_to_read(object()) == ["Ndc", "Rxcui"]

    def test_exclude_list_filters(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_spark(monkeypatch, ["Drugs", "Ndc", "Rxcui"], captured)
        d = AccessDriver(
            {"host_path": "/h"}, {}, {"path": "v.accdb", "exclude_tables": "Drugs"}
        )
        assert d.tables_to_read(object()) == ["Ndc", "Rxcui"]


class TestRead:
    def test_read_uses_table_name(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_spark(monkeypatch, [], captured)
        d = AccessDriver(
            {"host_path": "/h", "container_path": "/c"},
            {},
            {"path": "v.accdb", "lowercase_columns": True},
        )
        out = d.read(object(), "pl", "batch", "ignored", table_name="Drugs")
        assert out == "DF(Drugs)"
        assert captured["read"] == {
            "path": "/c/v.accdb",
            "table_name": "Drugs",
            "lowercase_columns": True,
        }

    def test_read_falls_back_to_file_name(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_spark(monkeypatch, [], captured)
        d = AccessDriver({"host_path": "/c"}, {}, {"path": "v.accdb"})
        out = d.read(object(), "pl", "batch", "Ndc")
        assert out == "DF(Ndc)"
        assert captured["read"]["table_name"] == "Ndc"

    def test_read_requires_a_table(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_spark(monkeypatch, [], captured)
        d = AccessDriver({"host_path": "/c"}, {}, {"path": "v.accdb"})
        with pytest.raises(ValueError, match="requires a table_name"):
            d.read(object(), "pl", "batch", "")


class TestLegacyBridge:
    def test_as_legacy_src_args(self) -> None:
        d = AccessDriver(
            {"host_path": "/h", "container_path": "/c"},
            {},
            {"path": "v.accdb", "tables": "A,B", "lowercase_columns": True},
        )
        name, args = d.as_legacy_src_args()
        assert name == "access"
        assert args == {
            "path": "/c/v.accdb",
            "tables": "A,B",
            "lowercase_columns": True,
        }


def _install_fake_reader(monkeypatch, tables, captured=None):
    """Install a fake ``reader.list_tables_no_spark`` returning ``tables``.

    When ``tables`` is the sentinel :class:`NotImplementedError`, the
    stub raises it so the driver falls back to the Spark path.
    """
    import ovid2.drivers.access.driver as driver_mod

    def _stub(file_path):
        if captured is not None:
            captured["reader_path"] = file_path
        if tables is NotImplementedError:
            raise NotImplementedError("no jaydebeapi in test")
        return list(tables)

    # Patch the lazy import target used inside ``_enumerate_tables``.
    import ovid2.drivers.access.reader as reader_mod

    monkeypatch.setattr(reader_mod, "list_tables_no_spark", _stub)
    return driver_mod


class TestSupportsExpandAll:
    def test_static_flag(self) -> None:
        assert AccessDriver.supports_expand_all() is True

    def test_expand_uses_spark_free_reader(self, monkeypatch) -> None:
        captured: dict = {}
        _install_fake_reader(monkeypatch, ["Drugs", "Ndc", "Rxcui"], captured)
        d = AccessDriver(
            {"host_path": "/h", "container_path": "/c"}, {}, {"path": "v.accdb"}
        )
        assert d.expand_all_tables() == ["Drugs", "Ndc", "Rxcui"]
        # Enumeration keys on the *host* path (control plane, no mount).
        assert captured["reader_path"] == "/h/v.accdb"

    def test_expand_honours_exclude_list(self, monkeypatch) -> None:
        _install_fake_reader(monkeypatch, ["Drugs", "Ndc", "Rxcui"])
        d = AccessDriver(
            {"host_path": "/h"}, {}, {"path": "v.accdb", "exclude_tables": "Drugs"}
        )
        assert d.expand_all_tables() == ["Ndc", "Rxcui"]

    def test_expand_honours_allow_list(self, monkeypatch) -> None:
        _install_fake_reader(monkeypatch, ["Drugs", "Ndc", "Rxcui"])
        d = AccessDriver(
            {"host_path": "/h"}, {}, {"path": "v.accdb", "tables": "ndc"}
        )
        assert d.expand_all_tables() == ["Ndc"]

    def test_expand_returns_none_when_unavailable(self, monkeypatch) -> None:
        # No jaydebeapi (reader raises) and no active Spark session →
        # ``expand_all_tables`` returns ``None`` so the caller keeps
        # rejecting ``all`` rather than copying nothing.
        _install_fake_reader(monkeypatch, NotImplementedError)

        fake = types.ModuleType("ovid2.drivers.access.spark")

        def get_active_session():
            raise NotImplementedError("no session")

        fake.get_active_session = get_active_session
        fake.list_tables = lambda s, p: []
        monkeypatch.setitem(sys.modules, "ovid2.drivers.access.spark", fake)

        d = AccessDriver({"host_path": "/h"}, {}, {"path": "v.accdb"})
        assert d.expand_all_tables() is None

    def test_list_dir_prefers_spark_free_reader(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "v.accdb"
        f.write_bytes(b"x")
        _install_fake_reader(monkeypatch, ["Alpha", "Beta"])
        d = AccessDriver({"host_path": str(tmp_path)}, {}, {})
        entries = d.list_dir("v.accdb")
        assert [e.name for e in entries] == ["Alpha", "Beta"]
        assert [e.kind for e in entries] == ["table", "table"]
        assert entries[0].path == "v.accdb::Alpha"
