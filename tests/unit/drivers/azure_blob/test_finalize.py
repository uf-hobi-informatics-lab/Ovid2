"""Unit tests for :func:`ovid2.drivers.azure_blob.spark.finalize_csv_block_commit`.

Drives the ADLS block-commit finalizer with a fake Azure Storage SDK
client so we can:

- Verify block 1 is the header (staged inline via ``stage_block``).
- Verify blocks 2..N are the body blobs (staged via
  ``stage_block_from_url``), in sorted key order.
- Verify ``commit_block_list`` receives the full ordered id list.
- Verify a missing body prefix raises loudly.

Also covers the ``AzureBlobIO`` write dispatch: the CSV write chain
no longer contains a ``.coalesce()`` call.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake azure-storage-blob SDK
# ---------------------------------------------------------------------------


class _FakeBlobClient:
    """Records ``stage_block`` / ``stage_block_from_url`` / ``commit`` calls."""

    def __init__(self, name: str, header_body: bytes = b"c1,c2\n") -> None:
        self.name = name
        self.header_body = header_body
        self.staged: list[dict[str, Any]] = []
        self.committed: list[str] | None = None

    def stage_block(self, block_id: str, data: bytes) -> None:
        self.staged.append({"kind": "block", "block_id": block_id, "data": data})

    def stage_block_from_url(self, block_id: str, source_url: str) -> None:
        self.staged.append(
            {"kind": "block_from_url", "block_id": block_id, "source_url": source_url},
        )

    def commit_block_list(self, block_ids: list[str]) -> None:
        self.committed = list(block_ids)

    def download_blob(self) -> Any:
        download = MagicMock()
        download.readall.return_value = self.header_body
        return download


class _FakeContainerClient:
    """Records what the finalizer asks of the container."""

    def __init__(
        self,
        listings: dict[str, list[str]] | None = None,
        header_body: bytes = b"h1,h2\n",
    ) -> None:
        self.listings = listings or {}
        self.header_body = header_body
        self.blob_clients: dict[str, _FakeBlobClient] = {}
        self.deleted: list[list[str]] = []

    def list_blobs(self, name_starts_with: str = "") -> list[Any]:
        return [types.SimpleNamespace(name=n) for n in self.listings.get(name_starts_with, [])]

    def get_blob_client(self, name: str) -> _FakeBlobClient:
        if name not in self.blob_clients:
            self.blob_clients[name] = _FakeBlobClient(name, self.header_body)
        return self.blob_clients[name]

    def delete_blobs(self, *blobs: str) -> None:
        self.deleted.append(list(blobs))


def _install_fake_azure_sdk(
    monkeypatch: pytest.MonkeyPatch,
    container: _FakeContainerClient,
) -> None:
    """Register a fake ``azure.storage.blob`` module."""
    # Namespace tree so ``from azure.storage.blob import ...`` works.
    azure = types.ModuleType("azure")
    azure_storage = types.ModuleType("azure.storage")
    azure_storage_blob = types.ModuleType("azure.storage.blob")

    class BlobClient:
        """Marker class only used for isinstance checks in the SUT."""

    azure_storage_blob.BlobClient = BlobClient
    azure_storage_blob.ContainerClient = lambda account_url, container_name, credential: container
    azure.storage = azure_storage
    azure_storage.blob = azure_storage_blob

    monkeypatch.setitem(sys.modules, "azure", azure)
    monkeypatch.setitem(sys.modules, "azure.storage", azure_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", azure_storage_blob)


# ---------------------------------------------------------------------------
# finalize_csv_block_commit — the module-level stitch helper.
# ---------------------------------------------------------------------------


class TestFinalizeCsvBlockCommit:
    """Direct coverage of the module-level block-commit stitch."""

    def test_stages_header_then_body_blocks_in_sorted_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.azure_blob.spark import finalize_csv_block_commit

        header_key = "staging/_header.csv/part-00000-hdr.csv"
        container = _FakeContainerClient(
            listings={
                "staging/body/": [
                    "staging/body/part-00002-def.csv",
                    "staging/body/part-00000-abc.csv",
                    "staging/body/part-00001-xyz.csv",
                ],
                "staging/_header.csv/": [header_key],
            },
        )
        _install_fake_azure_sdk(monkeypatch, container)

        finalize_csv_block_commit(
            account_name="acct",
            container="c",
            target_blob="p/b/t.csv",
            header_blob_dir="staging/_header.csv",
            body_prefix="staging/body/",
            endpoint_suffix="core.windows.net",
            credential="fake-key",
        )

        target = container.blob_clients["p/b/t.csv"]

        # Block 1 is the header, staged inline.
        assert target.staged[0]["kind"] == "block"
        assert target.staged[0]["data"] == b"h1,h2\n"

        # Blocks 2..4 are body copies, sorted by key.
        copies = target.staged[1:]
        assert [c["kind"] for c in copies] == ["block_from_url"] * 3
        urls = [c["source_url"] for c in copies]
        assert urls == [
            "https://acct.blob.core.windows.net/c/staging/body/part-00000-abc.csv",
            "https://acct.blob.core.windows.net/c/staging/body/part-00001-xyz.csv",
            "https://acct.blob.core.windows.net/c/staging/body/part-00002-def.csv",
        ]

        # commit_block_list receives every id in staging order.
        assert target.committed == [s["block_id"] for s in target.staged]

    def test_missing_body_parts_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.azure_blob.spark import finalize_csv_block_commit

        container = _FakeContainerClient(
            listings={"staging/body/": [], "staging/_header.csv/": []},
        )
        _install_fake_azure_sdk(monkeypatch, container)
        with pytest.raises(FileNotFoundError, match="No body part blobs"):
            finalize_csv_block_commit(
                account_name="acct",
                container="c",
                target_blob="t.csv",
                header_blob_dir="staging/_header.csv",
                body_prefix="staging/body/",
                endpoint_suffix="core.windows.net",
                credential="k",
            )

    def test_missing_header_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.azure_blob.spark import finalize_csv_block_commit

        container = _FakeContainerClient(
            listings={
                "staging/body/": ["staging/body/part-00000-x.csv"],
                "staging/_header.csv/": [],
            },
        )
        _install_fake_azure_sdk(monkeypatch, container)
        with pytest.raises(FileNotFoundError, match="No header part blob"):
            finalize_csv_block_commit(
                account_name="acct",
                container="c",
                target_blob="t.csv",
                header_blob_dir="staging/_header.csv",
                body_prefix="staging/body/",
                endpoint_suffix="core.windows.net",
                credential="k",
            )

    def test_body_prefix_trailing_slash_enforced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.azure_blob.spark import finalize_csv_block_commit

        container = _FakeContainerClient(
            listings={
                "staging/body/": ["staging/body/part-00000-x.csv"],
                "staging/_header.csv/": ["staging/_header.csv/part-00000-hdr.csv"],
            },
        )
        _install_fake_azure_sdk(monkeypatch, container)
        # No trailing slash on body_prefix — the SUT should normalise.
        finalize_csv_block_commit(
            account_name="acct",
            container="c",
            target_blob="t.csv",
            header_blob_dir="staging/_header.csv",
            body_prefix="staging/body",
            endpoint_suffix="core.windows.net",
            credential="k",
        )
        # Success == no raise; verify the blob was committed.
        target = container.blob_clients["t.csv"]
        assert target.committed is not None
        assert len(target.committed) == 2  # header + one part

    def test_ignores_non_part_blobs(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.azure_blob.spark import finalize_csv_block_commit

        container = _FakeContainerClient(
            listings={
                "staging/body/": [
                    "staging/body/_SUCCESS",
                    "staging/body/part-00000-x.csv",
                    "staging/body/.part-00000-x.csv.crc",
                ],
                "staging/_header.csv/": [
                    "staging/_header.csv/part-00000-hdr.csv",
                ],
            },
        )
        _install_fake_azure_sdk(monkeypatch, container)
        finalize_csv_block_commit(
            account_name="acct",
            container="c",
            target_blob="t.csv",
            header_blob_dir="staging/_header.csv",
            body_prefix="staging/body/",
            endpoint_suffix="core.windows.net",
            credential="k",
        )
        # Only one body part copy.
        target = container.blob_clients["t.csv"]
        copies = [s for s in target.staged if s["kind"] == "block_from_url"]
        assert len(copies) == 1
        assert copies[0]["source_url"].endswith("/staging/body/part-00000-x.csv")


class TestB64BlockId:
    """Block ids are equal-length base64 strings (Azure requirement)."""

    def test_ids_are_equal_length(self) -> None:
        from ovid2.drivers.azure_blob.spark import _b64_block_id

        ids = [_b64_block_id(i) for i in range(1, 10)]
        assert len({len(b) for b in ids}) == 1, (
            "Azure requires every block id in a commit to be equal length."
        )

    def test_ids_are_ascii_only(self) -> None:
        from ovid2.drivers.azure_blob.spark import _b64_block_id

        for i in range(20):
            b = _b64_block_id(i)
            assert b.encode("ascii").decode("ascii") == b


# ---------------------------------------------------------------------------
# AzureBlobIO.write dispatch — verify no coalesce in the chain.
# ---------------------------------------------------------------------------


class _FakeSparkSession:
    def __init__(self) -> None:
        self.conf = MagicMock()


class _FakeWriter:
    def __init__(self, log: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._log = log

    def _rec(self, name: str, *args: Any) -> _FakeWriter:
        self._log.append((name, args))
        return self

    def option(self, *a: Any) -> _FakeWriter:
        return self._rec("option", *a)

    def format(self, *a: Any) -> _FakeWriter:
        return self._rec("format", *a)

    def mode(self, *a: Any) -> _FakeWriter:
        return self._rec("mode", *a)

    def save(self, *a: Any) -> None:
        self._rec("save", *a)

    def parquet(self, *a: Any) -> None:
        self._rec("parquet", *a)

    def json(self, *a: Any) -> None:
        self._rec("json", *a)


class _FakeDF:
    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.write = _FakeWriter(self.log)

    def coalesce(self, n: int) -> _FakeDF:
        self.log.append(("coalesce", (n,)))
        return self


class TestAzureBlobIOWriteChain:
    """CSV write no longer contains a coalesce call."""

    def _make(self, **overrides: Any) -> Any:
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        cfg: dict[str, Any] = {"account_name": "acct", "container": "c"}
        cfg.update(overrides)
        return AzureBlobIO(cfg)

    def test_csv_write_no_coalesce_by_default(self) -> None:
        io = self._make()  # single_file defaults to False.
        df = _FakeDF()
        io.write(df, _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        names = [n for n, _ in df.log]
        assert "coalesce" not in names
        assert ("format", ("csv",)) in df.log
        assert ("save", ("abfss://c@acct.dfs.core.windows.net/t.csv",)) in df.log

    def test_single_file_true_uses_finalize_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.azure_blob import spark as azb_spark

        io = self._make(single_file=True)
        called: dict[str, Any] = {}

        def fake(
            self: Any,
            df: Any,
            pipeline: str,
            batch_id: str,
            table_name: str,
            delimiter: str,
        ) -> None:
            called["pipeline"] = pipeline
            called["table"] = table_name
            called["delimiter"] = delimiter

        monkeypatch.setattr(azb_spark.AzureBlobIO, "_write_csv_single_file", fake)
        io.write(_FakeDF(), _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        assert called == {"pipeline": "p", "table": "t", "delimiter": "\t"}

    def test_parquet_write_unchanged(self) -> None:
        io = self._make(format="parquet")
        df = _FakeDF()
        io.write(df, _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        assert ("parquet", ("abfss://c@acct.dfs.core.windows.net/t.parquet",)) in df.log

    def test_json_write_unchanged(self) -> None:
        io = self._make(format="json")
        df = _FakeDF()
        io.write(df, _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        assert ("json", ("abfss://c@acct.dfs.core.windows.net/t.json",)) in df.log

    def test_unknown_format_raises(self) -> None:
        io = self._make(format="avro")
        with pytest.raises(ValueError, match="Unsupported format"):
            io.write(_FakeDF(), _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]


class TestAzureBlobIOSingleFileFlag:
    """The ``single_file`` config knob threads onto the instance."""

    def test_default_is_false(self) -> None:
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        io = AzureBlobIO({"account_name": "a", "container": "c"})
        assert io.single_file is False

    def test_true_when_set(self) -> None:
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        io = AzureBlobIO({"account_name": "a", "container": "c", "single_file": True})
        assert io.single_file is True

    def test_missing_account_raises(self) -> None:
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        with pytest.raises(ValueError, match="missing required 'account_name'"):
            AzureBlobIO({"container": "c"})

    def test_missing_container_raises(self) -> None:
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        with pytest.raises(ValueError, match="missing required 'container'"):
            AzureBlobIO({"account_name": "a"})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
