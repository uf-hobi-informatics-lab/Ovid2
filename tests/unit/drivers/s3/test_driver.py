"""Unit tests for :class:`ovid2.drivers.s3.driver.S3Driver`.

Verifies that:

- The native driver is registered in :data:`DRIVERS` (the legacy
  adapter no longer owns the ``s3`` entry).
- ``connection_fields`` / ``location_fields`` return the same typed
  specs the metadata module provides.
- ``auth_mode`` classifies the credential triple as expected.
- ``as_legacy_src_args`` produces the ``(src_name, src_args)`` shape
  the pre-D3.s3 pipeline consumed.
- ``probe_auth`` returns a ``skipped`` result when boto3 is absent.
- ``instantiate(...)`` produces an :class:`S3Driver` and its data
  plane is wired through :class:`S3IO`.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from ovid2.drivers.base import ConnectionFieldSpec, Driver, ProbeResult
from ovid2.drivers.registry import DRIVERS, instantiate, load_class
from ovid2.drivers.s3.driver import S3Driver


class TestNativeRegistration:
    """After D3.s3, ``DRIVERS['s3']`` points at :class:`S3Driver`.

    The legacy adapter is no longer authoritative for this type — its
    :func:`register_all_legacy_drivers` skips ``s3`` because the
    native module self-registers first.
    """

    def test_registry_entry_is_native(self) -> None:
        assert DRIVERS.get("s3") == "ovid2.drivers.s3.driver.S3Driver"

    def test_load_class_returns_s3_driver(self) -> None:
        cls = load_class("s3")
        assert cls is S3Driver
        assert issubclass(cls, Driver)


class TestConnectionAndLocationFields:
    def test_connection_fields_typed(self) -> None:
        spec = S3Driver.connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)
        names = [f.name for f in spec.args]
        assert "region" in names
        secret_names = [f.name for f in spec.secrets]
        assert "aws_access_key_id" in secret_names

    def test_location_fields(self) -> None:
        fields = S3Driver.location_fields()
        names = [f.name for f in fields]
        assert names == ["bucket", "path", "format", "single_file"]


class TestAuthModeDisplay:
    """The ``auth_mode`` classifier reads from ``self.secrets``."""

    def test_session_mode(self) -> None:
        d = S3Driver(
            args={"region": "us-east-1"},
            secrets={
                "aws_access_key_id": "A",
                "aws_secret_access_key": "S",
                "aws_session_token": "T",
            },
        )
        assert d.auth_mode() == "session"

    def test_static_mode(self) -> None:
        d = S3Driver(
            args={"region": "us-east-1"},
            secrets={"aws_access_key_id": "A", "aws_secret_access_key": "S"},
        )
        assert d.auth_mode() == "static"

    def test_default_chain_when_no_secrets(self) -> None:
        d = S3Driver(args={"region": "us-east-1"}, secrets={})
        assert d.auth_mode() == "default_chain"


class TestAsLegacySrcArgs:
    """Legacy bridge: the tuple must match what ``S3Address`` consumes."""

    def test_full_shape(self) -> None:
        d = S3Driver(
            args={
                "region": "us-east-1",
                "endpoint_url": "https://minio.example.com",
                "bucket_default": "default-bucket",
                "signature_version": "s3v4",
            },
            secrets={
                "aws_access_key_id": "A",
                "aws_secret_access_key": "S",
                "aws_session_token": "T",
            },
            location={"bucket": "override", "path": "out", "format": "parquet"},
        )
        src, args = d.as_legacy_src_args()
        assert src == "s3"
        # Location bucket wins over bucket_default.
        assert args["bucket"] == "override"
        assert args["prefix"] == "out"
        assert args["format"] == "parquet"
        assert args["region"] == "us-east-1"
        assert args["endpoint_url"] == "https://minio.example.com"
        assert args["signature_version"] == "s3v4"
        assert args["aws_access_key_id"] == "A"
        assert args["aws_secret_access_key"] == "S"
        assert args["aws_session_token"] == "T"

    def test_bucket_default_used_when_location_bucket_blank(self) -> None:
        d = S3Driver(
            args={"region": "us-east-1", "bucket_default": "default-bucket"},
            secrets={},
            location={},
        )
        _, args = d.as_legacy_src_args()
        assert args["bucket"] == "default-bucket"

    def test_no_bucket_produces_no_bucket_key(self) -> None:
        d = S3Driver(args={"region": "us-east-1"}, secrets={}, location={})
        _, args = d.as_legacy_src_args()
        assert "bucket" not in args


class TestProbeAuth:
    """The probe routes through :func:`probe_auth_from_config`."""

    def test_boto3_absent_gives_skipped(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "boto3", None)
        d = S3Driver(args={"region": "us-east-1"}, secrets={})
        r = d.probe_auth()
        assert isinstance(r, ProbeResult)
        assert r.code == "skipped"
        assert r.capability == "auth"


class TestInstantiateViaRegistry:
    """``instantiate('s3', ...)`` returns an :class:`S3Driver`."""

    def test_returns_native_driver(self) -> None:
        d = instantiate(
            "s3",
            args={"region": "us-east-1"},
            secrets={"aws_access_key_id": "A", "aws_secret_access_key": "S"},
            location={"bucket": "b"},
        )
        assert isinstance(d, S3Driver)


class TestBuildIoCache:
    """``_build_io`` caches an :class:`S3IO` per driver instance."""

    def test_same_io_returned_twice(self) -> None:
        d = S3Driver(
            args={"region": "us-east-1"},
            secrets={},
            location={"bucket": "b"},
        )
        io1 = d._build_io()
        io2 = d._build_io()
        assert io1 is io2

    def test_missing_bucket_raises(self) -> None:
        d = S3Driver(args={"region": "us-east-1"}, secrets={}, location={})
        with pytest.raises(ValueError, match="missing 'bucket'"):
            d._build_io()


class TestDataPlaneRouting:
    """``read`` / ``write`` delegate to :class:`S3IO`."""

    def test_read_calls_io_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        d = S3Driver(
            args={"region": "us-east-1"},
            secrets={},
            location={"bucket": "b"},
        )

        calls: list[tuple[Any, ...]] = []

        class FakeIO:
            def read(
                self,
                spark: Any,
                pipeline: str,
                batch_id: str,
                file_name: str,
                delimiter: str | None = None,
            ) -> str:
                calls.append(("read", spark, pipeline, batch_id, file_name, delimiter))
                return "df"

            def write(self, *a: Any, **kw: Any) -> None:
                calls.append(("write",) + a)

        d._io_cache = FakeIO()  # type: ignore[assignment]
        result = d.read("spark", "acme", "20260610", "HASH_TOKEN.csv")  # type: ignore[arg-type]
        assert result == "df"
        assert calls[0][0] == "read"
        assert calls[0][3] == "20260610"

    def test_write_calls_io_write(self) -> None:
        d = S3Driver(
            args={"region": "us-east-1"},
            secrets={},
            location={"bucket": "b"},
        )

        calls: list[tuple[Any, ...]] = []

        class FakeIO:
            def read(self, *a: Any, **kw: Any) -> None:
                pass

            def write(
                self,
                df: Any,
                spark: Any,
                pipeline: str,
                batch_id: str,
                table_name: str,
                delimiter: str | None = None,
            ) -> None:
                calls.append((df, spark, pipeline, batch_id, table_name, delimiter))

        d._io_cache = FakeIO()  # type: ignore[assignment]
        d.write("df", "spark", "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]
        assert calls == [("df", "spark", "acme", "20260610", "HASH_TOKEN", None)]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
