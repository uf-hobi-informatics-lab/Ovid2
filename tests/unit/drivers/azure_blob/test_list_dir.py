"""Unit tests for azure_blob storage browse (``list_dir``).

Blob storage is a flat namespace; ``walk_blobs(delimiter='/')`` collapses
everything below the next ``/`` into a ``BlobPrefix`` (rendered ``dir``)
and yields leaf blobs (rendered ``file``). This mirrors the s3 driver's
``CommonPrefixes`` handling.

The Azure SDK is optional: this suite installs minimal fakes for
``azure.storage.blob`` in :data:`sys.modules` via ``monkeypatch.setitem``
so the tests run regardless of whether the ``ovid2[azure]`` extra is
installed, mirroring ``test_probe.py``.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from ovid2.drivers.azure_blob.driver import AzureBlobDriver
from ovid2.drivers.azure_blob.probe import list_dir_from_config


class _FakeBlobPrefix:
    """Stand-in for ``azure.storage.blob.BlobPrefix`` (a virtual dir)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeBlob:
    """Stand-in for a leaf ``BlobProperties`` item."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeContainerClient:
    def __init__(self, items: list[Any]) -> None:
        self._items = items
        self.walk_calls: list[dict[str, Any]] = []

    def walk_blobs(
        self, name_starts_with: str | None = None, delimiter: str = "/"
    ) -> list[Any]:
        self.walk_calls.append(
            {"name_starts_with": name_starts_with, "delimiter": delimiter}
        )
        return self._items


class _FakeServiceClient:
    def __init__(self, container_client: _FakeContainerClient) -> None:
        self._cc = container_client
        self.url = "https://acct.blob.core.windows.net"

    def get_container_client(self, name: str) -> _FakeContainerClient:
        self._cc.container = name  # type: ignore[attr-defined]
        return self._cc


def _install_fake_azure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_sdk: bool = True,
) -> None:
    """Register a minimal ``azure.storage.blob`` exposing ``BlobPrefix``."""
    if not with_sdk:
        # Ensure any real/previously-stubbed module is hidden.
        monkeypatch.setitem(sys.modules, "azure.storage.blob", None)
        return
    azure_pkg = types.ModuleType("azure")
    storage_pkg = types.ModuleType("azure.storage")
    blob_pkg = types.ModuleType("azure.storage.blob")
    blob_pkg.BlobPrefix = _FakeBlobPrefix  # type: ignore[attr-defined]
    # BlobServiceClient is referenced by _build_client; give it a shell.
    blob_pkg.BlobServiceClient = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "azure", azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.storage", storage_pkg)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", blob_pkg)


ARGS = {"account_name": "acct", "container_default": "data"}
SECRETS = {"account_key": "k"}


def _patch_build_client(
    monkeypatch: pytest.MonkeyPatch, service: _FakeServiceClient
) -> None:
    monkeypatch.setattr(
        "ovid2.drivers.azure_blob.probe._build_client",
        lambda mode, args, secrets, url: service,
    )


@pytest.mark.unit
def test_list_dir_root_dirs_first(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_azure(monkeypatch)
    cc = _FakeContainerClient(
        [
            _FakeBlob("readme.txt"),
            _FakeBlobPrefix("beta/"),
            _FakeBlobPrefix("alpha/"),
        ]
    )
    _patch_build_client(monkeypatch, _FakeServiceClient(cc))
    entries = list_dir_from_config(ARGS, SECRETS, {"container": "data"}, None)
    assert [(e.name, e.kind, e.path) for e in entries] == [
        ("alpha", "dir", "alpha"),
        ("beta", "dir", "beta"),
        ("readme.txt", "file", "readme.txt"),
    ]
    # Root browse passes no name_starts_with.
    assert cc.walk_calls[0]["name_starts_with"] is None


@pytest.mark.unit
def test_list_dir_descends_and_roundtrips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_azure(monkeypatch)
    cc = _FakeContainerClient([_FakeBlob("demo/20260101/demographic.csv")])
    _patch_build_client(monkeypatch, _FakeServiceClient(cc))
    entries = list_dir_from_config(
        ARGS, SECRETS, {"container": "data"}, "demo/20260101"
    )
    assert [(e.name, e.kind, e.path) for e in entries] == [
        ("demographic.csv", "file", "demo/20260101/demographic.csv")
    ]
    assert cc.walk_calls[0]["name_starts_with"] == "demo/20260101/"


@pytest.mark.unit
def test_list_dir_path_relative_to_location_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``location.path`` is stripped so ``path`` round-trips as prefix."""
    _install_fake_azure(monkeypatch)
    cc = _FakeContainerClient([_FakeBlobPrefix("root/demo/")])
    _patch_build_client(monkeypatch, _FakeServiceClient(cc))
    entries = list_dir_from_config(
        ARGS, SECRETS, {"container": "data", "path": "root"}, None
    )
    assert [(e.name, e.kind, e.path) for e in entries] == [
        ("demo", "dir", "demo")
    ]
    assert cc.walk_calls[0]["name_starts_with"] == "root/"


@pytest.mark.unit
def test_list_dir_no_container_is_capability_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_azure(monkeypatch)
    with pytest.raises(NotImplementedError, match="No container"):
        list_dir_from_config({}, SECRETS, {}, None)


@pytest.mark.unit
def test_list_dir_missing_sdk_is_capability_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_azure(monkeypatch, with_sdk=False)
    with pytest.raises(NotImplementedError, match="azure-storage-blob"):
        list_dir_from_config(ARGS, SECRETS, {"container": "data"}, None)


@pytest.mark.unit
def test_list_dir_sdk_error_is_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_azure(monkeypatch)

    class _Boom(_FakeServiceClient):
        def get_container_client(self, name: str) -> Any:
            raise RuntimeError("kaboom")

    _patch_build_client(
        monkeypatch, _Boom(_FakeContainerClient([]))
    )
    with pytest.raises(ValueError, match="Azure list failed"):
        list_dir_from_config(ARGS, SECRETS, {"container": "data"}, None)


@pytest.mark.unit
def test_driver_list_dir_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_azure(monkeypatch)
    cc = _FakeContainerClient([_FakeBlobPrefix("demo/")])
    _patch_build_client(monkeypatch, _FakeServiceClient(cc))
    driver = AzureBlobDriver(
        args=ARGS, secrets=SECRETS, location={"container": "data"}
    )
    entries = driver.list_dir(None)
    assert [(e.name, e.kind) for e in entries] == [("demo", "dir")]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
