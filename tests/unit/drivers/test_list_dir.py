"""Phase 4 — driver ``list_dir`` capability (disc) unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ovid2.drivers import StorageEntry
from ovid2.drivers.disc.driver import DiscDriver


def _disc(host_path: str) -> DiscDriver:
    return DiscDriver(
        args={"host_path": host_path, "container_path": "/data", "mount_mode": "rw"},
        secrets={},
        location={},
    )


@pytest.mark.unit
def test_storage_entry_to_dict() -> None:
    e = StorageEntry(name="demo", kind="dir", path="demo")
    assert e.to_dict() == {"name": "demo", "kind": "dir", "path": "demo"}


@pytest.mark.unit
def test_disc_list_dir_root(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "acme").mkdir()
    (tmp_path / "readme.txt").write_text("x")
    (tmp_path / ".hidden").write_text("x")
    entries = _disc(str(tmp_path)).list_dir()
    names = [(e.name, e.kind) for e in entries]
    # dirs first (sorted), then files; hidden skipped.
    assert names == [("acme", "dir"), ("demo", "dir"), ("readme.txt", "file")]


@pytest.mark.unit
def test_disc_list_dir_descends(tmp_path: Path) -> None:
    batch = tmp_path / "demo" / "20260101"
    batch.mkdir(parents=True)
    (batch / "demographic.csv").write_text("x")
    entries = _disc(str(tmp_path)).list_dir("demo/20260101")
    assert [(e.name, e.kind, e.path) for e in entries] == [
        ("demographic.csv", "file", "demo/20260101/demographic.csv")
    ]


@pytest.mark.unit
def test_disc_list_dir_path_roundtrips(tmp_path: Path) -> None:
    (tmp_path / "demo" / "20260101").mkdir(parents=True)
    driver = _disc(str(tmp_path))
    root = driver.list_dir()
    assert root[0].path == "demo"
    child = driver.list_dir(root[0].path)
    assert child[0].name == "20260101"


@pytest.mark.unit
def test_disc_list_dir_rejects_escape(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    with pytest.raises(ValueError, match="escapes"):
        _disc(str(tmp_path)).list_dir("../..")


@pytest.mark.unit
def test_disc_list_dir_missing_root(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        _disc(str(tmp_path / "nope")).list_dir()


@pytest.mark.unit
def test_disc_list_dir_non_directory_prefix(tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        _disc(str(tmp_path)).list_dir("afile")


@pytest.mark.unit
def test_base_driver_list_dir_not_implemented() -> None:
    """The default Driver.list_dir raises NotImplementedError."""
    from ovid2.drivers.s3.driver import S3Driver

    # An s3 driver with no bucket resolvable surfaces the capability-
    # absent path (NotImplementedError), not a crash.
    driver = S3Driver(args={}, secrets={}, location={})
    with pytest.raises((NotImplementedError, ValueError)):
        driver.list_dir()
