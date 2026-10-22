"""Unit tests for :func:`ovid2.drivers.s3.spark.finalize_csv_multipart`.

Drives the S3 multipart-copy finalizer with a fake ``boto3`` client so
we can:

- Verify part 1 is the header (uploaded inline via ``UploadPart``).
- Verify parts 2..N are the body objects (uploaded via
  ``UploadPartCopy``), in sorted key order.
- Verify ``CompleteMultipartUpload`` receives every ETag.
- Verify a failure between ``CreateMultipartUpload`` and
  ``CompleteMultipartUpload`` triggers an abort (no leaked MPUs).

Also covers the ``S3IO`` write dispatch so we don't accidentally
regress the ``coalesce`` removal (``single_file=False`` must write
the Spark-native prefix with **no** ``coalesce`` call).
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fake boto3 client + install helper
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """Records every S3 call and returns deterministic replies.

    Only the surface ``finalize_csv_multipart`` and
    ``S3IO._delete_prefix`` touch: ``list_objects_v2``,
    ``create_multipart_upload``, ``get_object``, ``upload_part``,
    ``upload_part_copy``, ``complete_multipart_upload``,
    ``abort_multipart_upload``, ``delete_objects``.
    """

    def __init__(
        self,
        listings: dict[str, list[str]] | None = None,
        header_body: bytes = b"col_a,col_b\n",
        sizes: dict[str, int] | None = None,
        bodies: dict[str, bytes] | None = None,
    ) -> None:
        self.listings = listings or {}
        self.header_body = header_body
        # Per-key ``Size`` for ``list_objects_v2`` results. Absent
        # keys default to a large-enough value that the finalize
        # takes the server-side-copy fast path — mirrors the
        # pre-fix test expectations while letting individual tests
        # opt into the streaming path by supplying explicit sizes.
        self.sizes: dict[str, int] = sizes or {}
        # Per-key body override for ``get_object``. Absent keys
        # return ``header_body`` (matches the old behaviour).
        self.bodies: dict[str, bytes] = bodies or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.completed: dict[str, Any] | None = None
        self.aborted: bool = False
        self.upload_id: str = "upload-1234"

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_objects_v2", kwargs)
        prefix = kwargs["Prefix"]
        keys = self.listings.get(prefix, [])
        # Default body-part size to a value comfortably above the 5 MB
        # ``_MIN_MULTIPART_COPY_BYTES`` threshold so listings that don't
        # pin a size take the fast copy path.
        default_size = 128 * 1024 * 1024
        return {
            "Contents": [{"Key": k, "Size": self.sizes.get(k, default_size)} for k in keys],
        }

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_multipart_upload", kwargs)
        return {"UploadId": self.upload_id}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_object", kwargs)
        # Emulate the streaming body the real API returns. Per-key
        # override wins so tests can round-trip byte-level identity.
        body = MagicMock()
        body.read.return_value = self.bodies.get(kwargs["Key"], self.header_body)
        return {"Body": body}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        self._record("upload_part", kwargs)
        return {"ETag": f"etag-{kwargs['PartNumber']}"}

    def upload_part_copy(self, **kwargs: Any) -> dict[str, Any]:
        self._record("upload_part_copy", kwargs)
        return {"CopyPartResult": {"ETag": f"copy-etag-{kwargs['PartNumber']}"}}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._record("complete_multipart_upload", kwargs)
        self.completed = kwargs
        return {"Location": "https://s3.example/{Bucket}/{Key}"}

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self._record("abort_multipart_upload", kwargs)
        self.aborted = True
        return {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_objects", kwargs)
        return {}


def _install_fake_boto3(monkeypatch: pytest.MonkeyPatch, client: _FakeS3Client) -> None:
    """Register a fake ``boto3`` module that returns ``client``."""
    fake = types.ModuleType("boto3")
    fake.client = lambda service, **kwargs: client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake)


# ---------------------------------------------------------------------------
# finalize_csv_multipart — the module-level stitch helper.
# ---------------------------------------------------------------------------


class TestFinalizeCsvMultipart:
    """Direct coverage of the module-level multipart stitch."""

    def test_uploads_header_then_body_parts_in_sorted_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fast path: every body part is ≥ 5 MB.

        The finalizer downloads the header + the first body object
        to build a single part-1 (so part-1 clears the 5 MB minimum
        despite the header being tiny), then falls back to
        server-side ``UploadPartCopy`` for the remaining body
        objects.
        """
        from ovid2.drivers.s3 import spark as s3_spark
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        # Shrink the multipart minimum to something the mock can
        # realistically produce so we exercise "one body object
        # already clears the floor" without allocating megabytes.
        monkeypatch.setattr(s3_spark, "_MIN_MULTIPART_COPY_BYTES", 64)

        # Bodies for the three body objects — each ≥ 64 bytes so
        # a single body get_object clears the floor.
        body_bytes = b"x" * 128
        client = _FakeS3Client(
            listings={
                "staging/body/": [
                    "staging/body/part-00000-abc.csv",
                    "staging/body/part-00002-def.csv",
                    "staging/body/part-00001-xyz.csv",
                ],
            },
            sizes={
                "staging/body/part-00000-abc.csv": 128,
                "staging/body/part-00001-xyz.csv": 128,
                "staging/body/part-00002-def.csv": 128,
            },
            bodies={
                "staging/body/part-00000-abc.csv": body_bytes,
                "staging/body/part-00001-xyz.csv": body_bytes,
                "staging/body/part-00002-def.csv": body_bytes,
            },
        )
        _install_fake_boto3(monkeypatch, client)

        finalize_csv_multipart(
            bucket="b",
            target_key="p/b/table.csv",
            header_key="staging/_header.csv",
            body_prefix="staging/body/",
            aws_kwargs={"region_name": "us-east-1"},
        )

        # Order of API calls: list → get_object(header) →
        # create_mpu → get_object(first body) → upload_part(1) →
        # upload_part_copy(2) → upload_part_copy(3) → complete.
        names = [n for n, _ in client.calls]
        assert names[0] == "list_objects_v2"
        assert "create_multipart_upload" in names
        assert names.count("get_object") == 2  # header + first body
        assert names.count("upload_part") == 1  # part 1 only
        assert names.count("upload_part_copy") == 2  # parts 2 + 3
        assert names[-1] == "complete_multipart_upload"

        # Body copy calls target the sorted keys — parts 2, 3 pull
        # the two body objects that weren't consumed by part-1.
        # Under the parallel finalize the copy calls may complete
        # in either order; assert on the set of (PartNumber, Key)
        # pairs rather than list ordering.
        copies = [k for n, k in client.calls if n == "upload_part_copy"]
        pn_to_src = {c["PartNumber"]: c["CopySource"]["Key"] for c in copies}
        assert pn_to_src == {
            2: "staging/body/part-00001-xyz.csv",
            3: "staging/body/part-00002-def.csv",
        }

        # Complete carries every ETag in order.
        assert client.completed is not None
        parts = client.completed["MultipartUpload"]["Parts"]
        assert [p["PartNumber"] for p in parts] == [1, 2, 3]
        assert parts[0]["ETag"] == "etag-1"
        assert parts[1]["ETag"] == "copy-etag-2"
        assert parts[2]["ETag"] == "copy-etag-3"

    def test_missing_body_parts_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        client = _FakeS3Client(listings={"staging/body/": []})
        _install_fake_boto3(monkeypatch, client)

        with pytest.raises(FileNotFoundError, match="No body part files"):
            finalize_csv_multipart(
                bucket="b",
                target_key="table.csv",
                header_key="staging/_header.csv",
                body_prefix="staging/body/",
            )
        # No multipart upload should have been created.
        names = [n for n, _ in client.calls]
        assert "create_multipart_upload" not in names

    def test_body_prefix_trailing_slash_enforced(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        client = _FakeS3Client(
            listings={
                "staging/body/": ["staging/body/part-00000-x.csv"],
            },
        )
        _install_fake_boto3(monkeypatch, client)

        finalize_csv_multipart(
            bucket="b",
            target_key="t.csv",
            header_key="staging/_header.csv",
            body_prefix="staging/body",  # no trailing slash
        )

        # The listing prefix must have been normalised to "staging/body/".
        listing = client.calls[0][1]
        assert listing["Prefix"] == "staging/body/"

    def test_ignores_non_part_files_under_body_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``_SUCCESS`` markers and CRC sidecars must not become body parts."""
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        # Spark writes _SUCCESS and .part._SUCCESS.crc alongside parts.
        client = _FakeS3Client(
            listings={
                "staging/body/": [
                    "staging/body/_SUCCESS",
                    "staging/body/part-00000-x.csv",
                    "staging/body/.part-00000-x.csv.crc",
                ],
            },
        )
        _install_fake_boto3(monkeypatch, client)

        finalize_csv_multipart(
            bucket="b",
            target_key="t.csv",
            header_key="staging/_header.csv",
            body_prefix="staging/body/",
        )

        # Only ``part-00000-x.csv`` should have been read as a body
        # object — either via ``upload_part_copy`` (fast path,
        # non-terminal ≥ 5 MB) or via ``get_object`` (streaming /
        # single-body fold-in). The other two entries are markers
        # and must be ignored. With a single body entry, the
        # terminal-fold-in path consumes it into part 1; assert
        # that ``get_object`` was called on the real body key and
        # never on the marker keys.
        body_gets = [
            k for n, k in client.calls if n == "get_object" and k["Key"] != "staging/_header.csv"
        ]
        assert len(body_gets) == 1
        assert body_gets[0]["Key"] == "staging/body/part-00000-x.csv"

        marker_reads = [
            k
            for n, k in client.calls
            if n in {"get_object", "upload_part_copy"}
            and k.get("Key", k.get("CopySource", {}).get("Key"))
            in {
                "staging/body/_SUCCESS",
                "staging/body/.part-00000-x.csv.crc",
            }
        ]
        assert not marker_reads, f"Marker files leaked into finalize: {marker_reads}"

    def test_abort_on_complete_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        client = _FakeS3Client(
            listings={
                "staging/body/": ["staging/body/part-00000-x.csv"],
            },
        )

        def _boom(**kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("S3 was mean")

        # Patch complete to fail so we exercise the abort path.
        client.complete_multipart_upload = _boom  # type: ignore[method-assign]
        _install_fake_boto3(monkeypatch, client)

        with pytest.raises(RuntimeError, match="multipart finalize failed"):
            finalize_csv_multipart(
                bucket="b",
                target_key="t.csv",
                header_key="staging/_header.csv",
                body_prefix="staging/body/",
            )
        assert client.aborted, "AbortMultipartUpload must run on failure."

    def test_streaming_fallback_for_small_body_parts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When body parts are tiny, everything streams through the
        driver so each non-terminal part clears the 5 MB minimum.

        Regression: pre-fix, ``finalize_csv_multipart`` treated
        the header as its own multipart part (always < 5 MB) then
        used ``UploadPartCopy`` for each body object. S3 rejected
        that with ``EntityTooSmall`` because the header part is
        not terminal. This test simulates a 5-part-file/35 MB
        Spark write (like the ACME HASH_TOKEN reproducer, scaled
        down for the unit test) and confirms every part is
        assembled correctly.
        """
        from ovid2.drivers.s3 import spark as s3_spark
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        # Shrink the minimum so the unit test doesn't allocate
        # megabytes of test data. Every body part is 20 bytes; the
        # header is 12 bytes.
        monkeypatch.setattr(s3_spark, "_MIN_MULTIPART_COPY_BYTES", 48)

        keys = [
            "staging/body/part-00000-abc.csv",
            "staging/body/part-00001-abc.csv",
            "staging/body/part-00002-abc.csv",
            "staging/body/part-00003-abc.csv",
            "staging/body/part-00004-abc.csv",
        ]
        client = _FakeS3Client(
            listings={"staging/body/": keys},
            header_body=b"col_a,col_b\n",  # 12 bytes
            sizes=dict.fromkeys(keys, 20),  # each body: 20 bytes (< 48)
            bodies={k: f"body{i:>02}--payload\n".encode() for i, k in enumerate(keys)},
        )
        _install_fake_boto3(monkeypatch, client)

        finalize_csv_multipart(
            bucket="b",
            target_key="t.csv",
            header_key="staging/_header.csv",
            body_prefix="staging/body/",
        )

        # No UploadPartCopy calls — everything streamed.
        names = [n for n, _ in client.calls]
        assert "upload_part_copy" not in names, (
            "All body parts are < 5 MB; the finalizer must stay in "
            "the streaming path and never emit UploadPartCopy."
        )

        # Every ``upload_part`` body except the terminal one must
        # clear the (shrunken) 48-byte floor.
        uploads = [k for n, k in client.calls if n == "upload_part"]
        assert len(uploads) >= 1
        for u in uploads[:-1]:
            assert len(u["Body"]) >= 48, (
                f"Non-terminal part {u['PartNumber']} is {len(u['Body'])} bytes — must be ≥ 48."
            )

        # PartNumbers are contiguous and start at 1.
        part_numbers = [u["PartNumber"] for u in uploads]
        assert part_numbers == list(range(1, len(uploads) + 1))

        # Byte integrity: header + body(0) + body(1) + … must round-
        # trip verbatim through the uploaded parts.
        concatenated = b"".join(u["Body"] for u in uploads)
        expected = b"col_a,col_b\n" + b"".join(f"body{i:>02}--payload\n".encode() for i in range(5))
        assert concatenated == expected

        # And Complete carries every ETag.
        assert client.completed is not None
        parts = client.completed["MultipartUpload"]["Parts"]
        assert [p["PartNumber"] for p in parts] == part_numbers

    def test_terminal_part_may_be_smaller_than_minimum(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The last body object is legal even when it's < 5 MB.

        S3 imposes the 5 MB floor only on non-terminal parts. The
        finalize must not stall waiting for a "big enough" trailing
        part.
        """
        from ovid2.drivers.s3 import spark as s3_spark
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        monkeypatch.setattr(s3_spark, "_MIN_MULTIPART_COPY_BYTES", 64)

        big_key = "staging/body/part-00000-big.csv"
        small_key = "staging/body/part-00001-tail.csv"
        client = _FakeS3Client(
            listings={"staging/body/": [big_key, small_key]},
            sizes={big_key: 512, small_key: 8},
            bodies={big_key: b"A" * 512, small_key: b"z" * 8},
        )
        _install_fake_boto3(monkeypatch, client)

        finalize_csv_multipart(
            bucket="b",
            target_key="t.csv",
            header_key="staging/_header.csv",
            body_prefix="staging/body/",
        )

        # Fast path: part-1 = header + big body (both consumed to
        # cross the 64-byte floor). Part-2 = small tail, uploaded
        # via UploadPartCopy — terminal part can be tiny.
        names = [n for n, _ in client.calls]
        assert names.count("upload_part_copy") == 1
        assert client.completed is not None
        assert [p["PartNumber"] for p in client.completed["MultipartUpload"]["Parts"]] == [1, 2]

    def test_parallel_copy_dispatches_across_thread_pool(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fast-path copies fan out across a thread pool.

        Regression: pre-parallelisation, the finalize invoked
        ``UploadPartCopy`` sequentially in the main thread — for a
        400-part table that added ~3 min of wall clock at the tail
        of an otherwise-parallel Spark write. This test drives 20
        copy-eligible body parts and verifies (a) every copy runs,
        (b) the ``PartNumber`` → ``Key`` mapping is preserved
        despite out-of-order completion, and (c) the resulting
        ``CompleteMultipartUpload`` sees a contiguous ascending
        ``PartNumber`` list.
        """
        from ovid2.drivers.s3 import spark as s3_spark
        from ovid2.drivers.s3.spark import finalize_csv_multipart

        # Shrunken min so 128-byte bodies fit the fast path.
        monkeypatch.setattr(s3_spark, "_MIN_MULTIPART_COPY_BYTES", 64)
        # Wide fan-out so the pool actually runs in parallel.
        monkeypatch.setattr(s3_spark, "_FINALIZE_COPY_CONCURRENCY", 8)

        keys = [f"staging/body/part-{i:05}.csv" for i in range(20)]
        client = _FakeS3Client(
            listings={"staging/body/": keys},
            sizes=dict.fromkeys(keys, 128),
            bodies={k: (f"row_{i:02}\n").encode() * 20 for i, k in enumerate(keys)},
        )
        _install_fake_boto3(monkeypatch, client)

        finalize_csv_multipart(
            bucket="b",
            target_key="t.csv",
            header_key="staging/_header.csv",
            body_prefix="staging/body/",
        )

        # Exactly 19 upload_part_copy calls — the first body is
        # consumed into part-1 alongside the header.
        copies = [k for n, k in client.calls if n == "upload_part_copy"]
        assert len(copies) == 19

        # PartNumber ↔ key mapping is byte-consistent with the
        # sorted listing (parts 2..20 → keys[1..20]).
        pn_to_src = {c["PartNumber"]: c["CopySource"]["Key"] for c in copies}
        expected = {i + 2: keys[i + 1] for i in range(19)}
        assert pn_to_src == expected

        # Complete carries every part in ascending order.
        assert client.completed is not None
        part_numbers = [p["PartNumber"] for p in client.completed["MultipartUpload"]["Parts"]]
        assert part_numbers == list(range(1, 21))


# ---------------------------------------------------------------------------
# S3IO.write dispatch — verify the coalesce removal and format branch.
# ---------------------------------------------------------------------------


class _FakeSparkSession:
    """Just enough of a SparkSession for ``_configure_spark`` to work."""

    def __init__(self) -> None:
        self.conf = MagicMock()


class _FakeWriter:
    """Records the chain of writer calls (``.option`` / ``.mode`` / ``.save``)."""

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
    """Records ``.coalesce`` / ``.write`` on a DataFrame."""

    def __init__(self) -> None:
        self.log: list[tuple[str, tuple[Any, ...]]] = []
        self.write = _FakeWriter(self.log)

    def coalesce(self, n: int) -> _FakeDF:
        self.log.append(("coalesce", (n,)))
        return self


class TestS3IOWriteChain:
    """The CSV write chain no longer contains a ``coalesce`` call."""

    def _make(self, **overrides: Any) -> Any:
        """Build an S3IO with a valid bucket + region."""
        from ovid2.drivers.s3.spark import S3IO

        cfg: dict[str, Any] = {"bucket": "b", "region": "us-east-1"}
        cfg.update(overrides)
        return S3IO(cfg)

    def test_csv_write_no_coalesce_by_default(self) -> None:
        io = self._make()  # single_file defaults to False.
        df = _FakeDF()
        io.write(df, _FakeSparkSession(), "demo", "20260101", "table")  # type: ignore[arg-type]
        names = [n for n, _ in df.log]
        assert "coalesce" not in names
        # Chain: format(csv) → optionx4 → mode(overwrite) → save(...)
        assert names[0] == "format"
        assert ("format", ("csv",)) in df.log
        assert ("mode", ("overwrite",)) in df.log
        assert ("save", ("s3a://b/demo/20260101/table.csv",)) in df.log

    def test_single_file_true_uses_finalize_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When single_file is on, ``_write_csv_single_file`` is used."""
        from ovid2.drivers.s3 import spark as s3_spark

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

        monkeypatch.setattr(s3_spark.S3IO, "_write_csv_single_file", fake)
        io.write(_FakeDF(), _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        assert called == {"pipeline": "p", "table": "t", "delimiter": "\t"}

    def test_parquet_write_unchanged(self) -> None:
        io = self._make(format="parquet")
        df = _FakeDF()
        io.write(df, _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        # parquet path: df.write.mode('overwrite').parquet(target)
        assert ("mode", ("overwrite",)) in df.log
        assert ("parquet", ("s3a://b/p/b/t.parquet",)) in df.log

    def test_json_write_unchanged(self) -> None:
        io = self._make(format="json")
        df = _FakeDF()
        io.write(df, _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]
        assert ("json", ("s3a://b/p/b/t.json",)) in df.log

    def test_unknown_format_raises(self) -> None:
        io = self._make(format="avro")
        with pytest.raises(ValueError, match="Unsupported format"):
            io.write(_FakeDF(), _FakeSparkSession(), "p", "b", "t")  # type: ignore[arg-type]


class TestBoto3KwargsMapping:
    """``_boto3_kwargs`` mirrors the Hadoop credential legs."""

    def _make(self, **overrides: Any) -> Any:
        from ovid2.drivers.s3.spark import S3IO

        cfg: dict[str, Any] = {"bucket": "b"}
        cfg.update(overrides)
        return S3IO(cfg)

    def test_default_chain_returns_only_geo_settings(self) -> None:
        io = self._make(region="us-west-2", endpoint_url="http://minio:9000")
        kwargs = io._boto3_kwargs()
        assert kwargs == {
            "region_name": "us-west-2",
            "endpoint_url": "http://minio:9000",
        }

    def test_static_forwards_key_pair(self) -> None:
        io = self._make(
            aws_access_key_id="AKIA",
            aws_secret_access_key="secret",
        )
        kwargs = io._boto3_kwargs()
        assert kwargs["aws_access_key_id"] == "AKIA"
        assert kwargs["aws_secret_access_key"] == "secret"
        assert "aws_session_token" not in kwargs

    def test_session_forwards_key_pair_and_token(self) -> None:
        io = self._make(
            aws_access_key_id="AKIA",
            aws_secret_access_key="secret",
            aws_session_token="tok",
        )
        kwargs = io._boto3_kwargs()
        assert kwargs["aws_session_token"] == "tok"


class TestS3IOSingleFileFlag:
    """The ``single_file`` config knob threads onto the instance."""

    def test_default_is_false(self) -> None:
        from ovid2.drivers.s3.spark import S3IO

        io = S3IO({"bucket": "b"})
        assert io.single_file is False

    def test_true_when_set(self) -> None:
        from ovid2.drivers.s3.spark import S3IO

        io = S3IO({"bucket": "b", "single_file": True})
        assert io.single_file is True

    def test_missing_bucket_raises(self) -> None:
        from ovid2.drivers.s3.spark import S3IO

        with pytest.raises(ValueError, match="missing 'bucket'"):
            S3IO({})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
