"""Unit tests for :class:`ovid2.drivers.s3.S3Address` (legacy shim).

Locks the invariants added by the S3-address port and preserved by
the D3.s3 driver migration. The address class now lives at
:mod:`ovid2.drivers.s3.legacy` (re-exported from
:mod:`ovid2.drivers.s3`) and delegates every I/O method to
:class:`ovid2.drivers.s3.spark.S3IO`; test values against the legacy
shape must remain byte-identical.

Invariants exercised:

- Bucket is required; empty / missing bucket raises ``ValueError``
  before a session is touched.
- Path assembly honours ``prefix`` / ``pipeline`` / ``batch_id`` /
  ``file_name`` and drops empty parts (so a missing ``prefix`` doesn't
  emit ``s3a://bucket//pipeline/…``).
- Auth-mode resolution picks ``session`` when a session token is
  present, ``static`` when only the access-key pair is present, and
  ``default_chain`` otherwise. No credentials on the config → the
  address falls back to the AWS SDK default provider chain.
- :meth:`_configure_spark` pushes exactly the right Hadoop ``s3a``
  conf keys for each mode and never re-configures the same session
  twice.
- ``endpoint_url`` toggles path-style access; ``region`` and
  ``signature_version`` are forwarded when set.
- ``read`` / ``write`` route through the format switch and honour
  ``lowercase_columns``.

Tests avoid a real Spark session by driving a lightweight fake that
records ``.conf.set(...)`` calls; the read/write path tests build a
fake DataFrame-shaped chain so no JVM is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from ovid2.drivers.s3 import S3Address
from ovid2.drivers.s3.metadata import (
    _DEFAULT_CREDS_PROVIDER,
    _SIMPLE_CREDS_PROVIDER,
    _TEMPORARY_CREDS_PROVIDER,
)


# ---------------------------------------------------------------------------
# Fakes: a Spark session that records every ``conf.set(…)`` call.
# ---------------------------------------------------------------------------


class _RecordingConf:
    def __init__(self) -> None:
        self.entries: dict[str, str] = {}

    def set(self, key: str, value: Any) -> None:
        self.entries[key] = str(value)


class _RecordingSpark:
    def __init__(self) -> None:
        self.conf = _RecordingConf()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    """Config validation and default handling."""

    def test_minimum_config(self) -> None:
        a = S3Address({"bucket": "b1"})
        assert a.bucket == "b1"
        assert a.prefix == ""
        assert a.format == "csv"
        assert a.lowercase_columns is False
        assert a.auth_mode == "default_chain"

    def test_missing_bucket_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'bucket'"):
            S3Address({})

    def test_blank_bucket_raises(self) -> None:
        """A whitespace-only bucket is treated as missing."""
        with pytest.raises(ValueError, match="missing 'bucket'"):
            S3Address({"bucket": "   "})

    def test_prefix_is_stripped_of_slashes(self) -> None:
        """Callers may or may not supply leading/trailing slashes; strip both."""
        a = S3Address({"bucket": "b", "prefix": "//foo/bar//"})
        assert a.prefix == "foo/bar"

    def test_static_auth_resolves_when_pair_present(self) -> None:
        a = S3Address(
            {
                "bucket": "b",
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "sekrit",
            }
        )
        assert a.auth_mode == "static"

    def test_session_auth_resolves_when_token_present(self) -> None:
        a = S3Address(
            {
                "bucket": "b",
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "sekrit",
                "aws_session_token": "TOKEN",
            }
        )
        assert a.auth_mode == "session"

    def test_session_token_alone_does_not_activate_session_mode(self) -> None:
        """Without the key pair a stray session token is ignored — the
        default provider chain will decide what to do."""
        a = S3Address({"bucket": "b", "aws_session_token": "T"})
        assert a.auth_mode == "default_chain"

    def test_only_access_key_id_falls_back_to_default_chain(self) -> None:
        """Half-populated static credentials must not trip the static mode
        (or the driver would emit an obscure 400 signing error)."""
        a = S3Address({"bucket": "b", "aws_access_key_id": "AKIA"})
        assert a.auth_mode == "default_chain"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestPathResolution:
    """``_resolve_read_path`` / ``_resolve_write_dir`` layout."""

    def test_read_path_with_prefix(self) -> None:
        a = S3Address({"bucket": "b", "prefix": "out"})
        assert (
            a._resolve_read_path("acme", "20260610", "HASH_TOKEN.csv")
            == "s3a://b/out/acme/20260610/HASH_TOKEN.csv"
        )

    def test_read_path_without_prefix(self) -> None:
        a = S3Address({"bucket": "b"})
        assert (
            a._resolve_read_path("acme", "20260610", "HASH_TOKEN.csv")
            == "s3a://b/acme/20260610/HASH_TOKEN.csv"
        )

    def test_write_dir_without_prefix(self) -> None:
        a = S3Address({"bucket": "b"})
        assert a._resolve_write_dir("acme", "20260610") == "s3a://b/acme/20260610"

    def test_write_dir_with_multi_segment_prefix(self) -> None:
        a = S3Address({"bucket": "b", "prefix": "region1/out"})
        assert (
            a._resolve_write_dir("acme", "20260610")
            == "s3a://b/region1/out/acme/20260610"
        )


# ---------------------------------------------------------------------------
# Spark conf wiring
# ---------------------------------------------------------------------------


class TestConfigureSpark:
    """`_configure_spark` emits the correct Hadoop keys for each mode."""

    def test_static_credentials_conf(self) -> None:
        a = S3Address(
            {
                "bucket": "b",
                "aws_access_key_id": "AKIA",
                "aws_secret_access_key": "sekrit",
            }
        )
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        conf = spark.conf.entries
        assert conf["fs.s3a.aws.credentials.provider"] == _SIMPLE_CREDS_PROVIDER
        assert conf["fs.s3a.access.key"] == "AKIA"
        assert conf["fs.s3a.secret.key"] == "sekrit"
        assert "fs.s3a.session.token" not in conf

    def test_session_credentials_conf(self) -> None:
        a = S3Address(
            {
                "bucket": "b",
                "aws_access_key_id": "A",
                "aws_secret_access_key": "S",
                "aws_session_token": "T",
            }
        )
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        conf = spark.conf.entries
        assert conf["fs.s3a.aws.credentials.provider"] == _TEMPORARY_CREDS_PROVIDER
        assert conf["fs.s3a.access.key"] == "A"
        assert conf["fs.s3a.secret.key"] == "S"
        assert conf["fs.s3a.session.token"] == "T"

    def test_default_chain_conf(self) -> None:
        a = S3Address({"bucket": "b"})
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        conf = spark.conf.entries
        assert conf["fs.s3a.aws.credentials.provider"] == _DEFAULT_CREDS_PROVIDER
        # No access.key / secret.key when relying on the default chain.
        assert "fs.s3a.access.key" not in conf
        assert "fs.s3a.secret.key" not in conf

    def test_region_forwarded_when_set(self) -> None:
        a = S3Address({"bucket": "b", "region": "us-east-1"})
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        assert spark.conf.entries["fs.s3a.endpoint.region"] == "us-east-1"

    def test_endpoint_url_toggles_path_style(self) -> None:
        """Non-AWS S3-compatible services need path-style addressing.

        Without this, requests would go to
        ``bucket.minio.example.com`` (virtual-host style) which is
        exactly what non-AWS providers refuse to route.
        """
        a = S3Address({"bucket": "b", "endpoint_url": "https://minio.example.com"})
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        assert spark.conf.entries["fs.s3a.endpoint"] == "https://minio.example.com"
        assert spark.conf.entries["fs.s3a.path.style.access"] == "true"

    def test_signature_version_forwarded_when_set(self) -> None:
        a = S3Address({"bucket": "b", "signature_version": "s3v4"})
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        assert spark.conf.entries["fs.s3a.signing-algorithm"] == "s3v4"

    def test_repeat_configure_is_idempotent(self) -> None:
        """Second call must not re-emit conf entries.

        Ovid2 workflows may create a single ``SparkSession`` and route
        multiple addresses through it; every address should touch conf
        exactly once per session for observability.
        """
        a = S3Address({"bucket": "b"})
        spark = _RecordingSpark()
        a._configure_spark(spark)  # type: ignore[arg-type]
        spark.conf.entries.clear()  # simulate someone else touching it
        a._configure_spark(spark)  # type: ignore[arg-type]
        assert spark.conf.entries == {}


# ---------------------------------------------------------------------------
# Read / write dispatch
# ---------------------------------------------------------------------------


class _FakeDF:
    """A DataFrame-shaped fake that records every call in ``calls``."""

    def __init__(self, calls: list[tuple[str, tuple[Any, ...]]]):
        self.calls = calls
        self.write = self  # so ``df.write.mode(...)`` etc. resolve to us
        self.columns = ["A", "B"]

    def _record(self, name: str, *args: Any) -> "_FakeDF":
        self.calls.append((name, args))
        return self

    # Reader-side chain
    def option(self, *a: Any, **kw: Any) -> "_FakeDF":  # noqa: ARG002
        return self._record("option", *a)

    def csv(self, path: str) -> "_FakeDF":
        return self._record("csv", path)

    def parquet(self, path: str) -> "_FakeDF":
        return self._record("parquet", path)

    def json(self, path: str) -> "_FakeDF":
        return self._record("json", path)

    def toDF(self, *columns: str) -> "_FakeDF":  # noqa: N802 — mimic Spark API
        return self._record("toDF", *columns)

    # Writer-side chain
    def coalesce(self, n: int) -> "_FakeDF":
        return self._record("coalesce", n)

    def format(self, fmt: str) -> "_FakeDF":
        return self._record("format", fmt)

    def mode(self, m: str) -> "_FakeDF":
        return self._record("mode", m)

    def save(self, path: str) -> None:
        self._record("save", path)


class _RecordingReadSpark(_RecordingSpark):
    """Extends the recording spark to carry a ``read`` façade."""

    def __init__(self) -> None:
        super().__init__()
        self.read_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.read = _FakeDF(self.read_calls)


class TestRead:
    """`read` routes to the format-specific ``spark.read.<fmt>``."""

    def test_read_csv_default_delimiter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Freeze the module-level ``CSV_DELIMITER`` for a stable assert.
        # CSV_DELIMITER lives on drivers/s3/spark.py post-D3.s3.
        from ovid2.drivers.s3 import spark as s3_mod

        monkeypatch.setattr(s3_mod, "CSV_DELIMITER", ",")

        a = S3Address({"bucket": "b"})
        spark = _RecordingReadSpark()
        a.read(spark, "acme", "20260610", "HASH_TOKEN.csv")  # type: ignore[arg-type]
        call_names = [c[0] for c in spark.read_calls]
        assert call_names[-1] == "csv"
        assert spark.read_calls[-1][1] == ("s3a://b/acme/20260610/HASH_TOKEN.csv",)
        # And the auth conf was pushed.
        assert "fs.s3a.aws.credentials.provider" in spark.conf.entries

    def test_read_parquet(self) -> None:
        a = S3Address({"bucket": "b", "format": "parquet"})
        spark = _RecordingReadSpark()
        a.read(spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]
        parquet_calls = [c for c in spark.read_calls if c[0] == "parquet"]
        assert parquet_calls == [("parquet", ("s3a://b/acme/20260610/HASH_TOKEN",))]

    def test_read_json(self) -> None:
        a = S3Address({"bucket": "b", "format": "json"})
        spark = _RecordingReadSpark()
        a.read(spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]
        json_calls = [c for c in spark.read_calls if c[0] == "json"]
        assert json_calls == [("json", ("s3a://b/acme/20260610/HASH_TOKEN",))]

    def test_read_unknown_format(self) -> None:
        a = S3Address({"bucket": "b", "format": "avro"})
        spark = _RecordingReadSpark()
        with pytest.raises(ValueError, match="Unsupported format"):
            a.read(spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]

    def test_read_lowercase_columns(self) -> None:
        a = S3Address({"bucket": "b", "lowercase_columns": True})
        spark = _RecordingReadSpark()
        a.read(spark, "acme", "20260610", "HASH_TOKEN.csv")  # type: ignore[arg-type]
        # The ``toDF`` renaming call is the last one on the chain.
        assert any(c[0] == "toDF" for c in spark.read_calls)


class TestWrite:
    """`write` dispatches by format and applies the target path."""

    def test_write_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ovid2.drivers.s3 import spark as s3_mod

        monkeypatch.setattr(s3_mod, "CSV_DELIMITER", ",")

        a = S3Address({"bucket": "b"})
        spark = _RecordingSpark()
        calls: list[tuple[str, tuple[Any, ...]]] = []
        df = _FakeDF(calls)
        a.write(df, spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]

        # Trace (2026-08 refactor): format(csv) -> option x4 -> mode -> save.
        # No leading coalesce — S3 now writes at Spark's natural
        # partitioning; single_file merging (opt-in, default False)
        # is handled server-side by ``finalize_csv_multipart``.
        assert calls[0] == ("format", ("csv",))
        assert not any(c[0] == "coalesce" for c in calls)
        modes = [c for c in calls if c[0] == "mode"]
        saves = [c for c in calls if c[0] == "save"]
        assert modes == [("mode", ("overwrite",))]
        assert saves == [("save", ("s3a://b/acme/20260610/HASH_TOKEN.csv",))]

    def test_write_parquet(self) -> None:
        a = S3Address({"bucket": "b", "format": "parquet"})
        spark = _RecordingSpark()
        calls: list[tuple[str, tuple[Any, ...]]] = []
        df = _FakeDF(calls)
        a.write(df, spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]
        # parquet path: df.write.mode('overwrite').parquet(target)
        assert ("mode", ("overwrite",)) in calls
        assert ("parquet", ("s3a://b/acme/20260610/HASH_TOKEN.parquet",)) in calls

    def test_write_json(self) -> None:
        a = S3Address({"bucket": "b", "format": "json"})
        spark = _RecordingSpark()
        calls: list[tuple[str, tuple[Any, ...]]] = []
        df = _FakeDF(calls)
        a.write(df, spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]
        assert ("json", ("s3a://b/acme/20260610/HASH_TOKEN.json",)) in calls

    def test_write_unknown_format(self) -> None:
        a = S3Address({"bucket": "b", "format": "avro"})
        spark = _RecordingSpark()
        df = _FakeDF([])
        with pytest.raises(ValueError, match="Unsupported format"):
            a.write(df, spark, "acme", "20260610", "HASH_TOKEN")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    """`s3` is wired into the address / connection-shape registries."""

    def test_address_type_map_includes_s3(self) -> None:
        from ovid2.config.addresses import ADDRESS_TYPE_MAP

        assert ADDRESS_TYPE_MAP["s3"] == "ovid2.drivers.s3.S3Address"

    def test_connection_to_src_name_includes_s3(self) -> None:
        from ovid2.config.addresses import CONNECTION_TO_SRC_NAME

        assert CONNECTION_TO_SRC_NAME["s3"] == "s3"

    def test_load_address_imports_class(self, tmp_path, monkeypatch) -> None:
        """`load_address` can import S3Address via ``ADDRESS_TYPE_MAP``.

        Uses an on-disk legacy-shape address so we exercise the
        ``ADDRESS_TYPE_MAP`` code path without needing a Connection
        JSON on disk.
        """
        import json

        configs = tmp_path / "configs"
        configs.mkdir()
        (configs / "s3_direct.json").write_text(
            json.dumps(
                {
                    "src_name": "s3",
                    "src_args": {"bucket": "b", "aws_access_key_id": "A",
                                 "aws_secret_access_key": "S"},
                }
            )
        )
        monkeypatch.setenv("OVID2_CONFIGS_DIR", str(configs))

        from ovid2.config.addresses import load_address

        addr = load_address("s3_direct")
        assert type(addr).__name__ == "S3Address"
        assert addr.bucket == "b"  # type: ignore[attr-defined]
        assert addr.auth_mode == "static"  # type: ignore[attr-defined]
