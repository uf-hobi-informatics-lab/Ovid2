"""Unit tests for :mod:`ovid2.drivers.access.probe`."""

from __future__ import annotations

from ovid2.drivers.access.probe import (
    probe_auth_from_host_path,
    probe_read_from_file,
)


class TestProbeAuth:
    """Directory-level auth probe."""

    def test_missing_host_path(self) -> None:
        result = probe_auth_from_host_path("", {})
        assert not result.ok
        assert result.code == "config"
        assert result.capability == "auth"

    def test_nonexistent_dir(self) -> None:
        result = probe_auth_from_host_path("/no/such/dir/xyz", {})
        assert not result.ok
        assert result.code == "unreachable"

    def test_readable_dir(self, tmp_path) -> None:
        result = probe_auth_from_host_path(str(tmp_path), {})
        assert result.ok
        assert result.code == "ok"


class TestProbeReadFile:
    """File-level read probe."""

    def test_missing_path(self) -> None:
        result = probe_read_from_file("", {})
        assert not result.ok
        assert result.code == "config"
        assert result.capability == "read"

    def test_directory_with_access_file(self, tmp_path) -> None:
        (tmp_path / "db.accdb").write_bytes(b"x")
        result = probe_read_from_file(str(tmp_path), {})
        assert result.ok
        assert "1 Access file" in result.message

    def test_directory_without_access_file(self, tmp_path) -> None:
        (tmp_path / "notes.txt").write_text("hi")
        result = probe_read_from_file(str(tmp_path), {})
        assert not result.ok
        assert result.code == "unreachable"

    def test_file_missing(self, tmp_path) -> None:
        result = probe_read_from_file(str(tmp_path / "gone.accdb"), {})
        assert not result.ok
        assert result.code == "unreachable"

    def test_wrong_suffix(self, tmp_path) -> None:
        f = tmp_path / "data.csv"
        f.write_text("a,b")
        result = probe_read_from_file(str(f), {})
        assert not result.ok
        assert result.code == "config"

    def test_readable_accdb(self, tmp_path) -> None:
        f = tmp_path / "vantage.accdb"
        f.write_bytes(b"Standard ACE DB")
        result = probe_read_from_file(str(f), {})
        assert result.ok
        assert result.code == "ok"

    def test_mdb_suffix_accepted(self, tmp_path) -> None:
        f = tmp_path / "legacy.mdb"
        f.write_bytes(b"x")
        result = probe_read_from_file(str(f), {})
        assert result.ok
