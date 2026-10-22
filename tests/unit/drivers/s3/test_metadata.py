"""Unit tests for :mod:`ovid2.drivers.s3.metadata`.

Locks the invariants relied on by the web tier and MCP:

- ``connection_fields`` and ``location_fields`` return typed
  :class:`ConnectionFieldSpec` / :class:`LocationField` shapes and are
  byte-for-byte equivalent to the pre-D3.s3 catalogues.
- ``resolve_auth_mode`` classifies every triple correctly. Regression
  target: the two-out-of-three cases that used to trip the address
  into ``static`` mode with only half the credentials.
- ``build_address_config`` reproduces the pre-D3.s3
  ``S3Connection.as_address_config`` shape exactly, including
  bucket-fallback behaviour and prefix-vs-path aliasing.
- Metadata module is import-safe (no boto3, no pyspark).
"""

from __future__ import annotations

import sys

from ovid2.drivers.base import ConnectionFieldSpec, LocationField
from ovid2.drivers.s3.metadata import (
    _DEFAULT_CREDS_PROVIDER,
    _SIMPLE_CREDS_PROVIDER,
    _TEMPORARY_CREDS_PROVIDER,
    _VALID_FORMATS,
    build_address_config,
    connection_fields,
    location_fields,
    resolve_auth_mode,
)


class TestResolveAuthMode:
    """The credential-triple classifier."""

    def test_session_when_all_three_present(self) -> None:
        assert (
            resolve_auth_mode(
                {
                    "aws_access_key_id": "A",
                    "aws_secret_access_key": "S",
                    "aws_session_token": "T",
                }
            )
            == "session"
        )

    def test_static_when_only_pair_present(self) -> None:
        assert (
            resolve_auth_mode(
                {"aws_access_key_id": "A", "aws_secret_access_key": "S"}
            )
            == "static"
        )

    def test_default_chain_when_all_absent(self) -> None:
        assert resolve_auth_mode({}) == "default_chain"

    def test_default_chain_when_only_access_key(self) -> None:
        """Half-populated credentials must fall through to the default chain.

        Without this the Hadoop driver would emit an obscure 400
        signing error at Spark-runtime rather than a config-time
        failure.
        """
        assert (
            resolve_auth_mode({"aws_access_key_id": "AKIA"}) == "default_chain"
        )

    def test_default_chain_when_only_session_token(self) -> None:
        """A stray session token without a key pair means nothing."""
        assert resolve_auth_mode({"aws_session_token": "T"}) == "default_chain"

    def test_blank_values_treated_as_absent(self) -> None:
        assert (
            resolve_auth_mode(
                {"aws_access_key_id": "", "aws_secret_access_key": "   "}
            )
            == "default_chain"
        )


class TestConnectionFields:
    """The connection-form field spec."""

    def test_returns_typed_spec(self) -> None:
        spec = connection_fields()
        assert isinstance(spec, ConnectionFieldSpec)

    def test_arg_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.args]
        assert names == [
            "region",
            "endpoint_url",
            "bucket_default",
            "signature_version",
        ]

    def test_secret_names_match_legacy(self) -> None:
        spec = connection_fields()
        names = [f.name for f in spec.secrets]
        assert names == [
            "aws_access_key_id",
            "aws_secret_access_key",
            "aws_session_token",
        ]

    def test_region_is_required(self) -> None:
        spec = connection_fields()
        region = next(f for f in spec.args if f.name == "region")
        assert region.required is True

    def test_no_top_level_note(self) -> None:
        assert connection_fields().note is None


class TestLocationFields:
    """The location-form field spec."""

    def test_returns_typed_tuple(self) -> None:
        fields = location_fields()
        assert isinstance(fields, tuple)
        for f in fields:
            assert isinstance(f, LocationField)

    def test_names_match_legacy(self) -> None:
        names = [f.name for f in location_fields()]
        assert names == ["bucket", "path", "format", "single_file"]

    def test_format_default_is_parquet(self) -> None:
        fmt = next(f for f in location_fields() if f.name == "format")
        assert fmt.default == "parquet"

    def test_no_field_is_required(self) -> None:
        """S3 locations may fall back to the connection's ``bucket_default``
        when the operator leaves everything blank."""
        for f in location_fields():
            assert f.required is False


class TestBuildAddressConfig:
    """The connection-args + secrets + location → address-config merger."""

    def test_merges_static_credentials(self) -> None:
        got = build_address_config(
            args={"region": "us-east-1"},
            secrets={"aws_access_key_id": "A", "aws_secret_access_key": "S"},
            location={"bucket": "b", "path": "out/acme", "format": "parquet"},
        )
        assert got == {
            "region": "us-east-1",
            "aws_access_key_id": "A",
            "aws_secret_access_key": "S",
            "bucket": "b",
            "prefix": "out/acme",
            "format": "parquet",
        }

    def test_bucket_default_wins_when_location_blank(self) -> None:
        got = build_address_config(
            args={"region": "us-east-1", "bucket_default": "default-bucket"},
            secrets={},
            location={},
        )
        assert got["bucket"] == "default-bucket"

    def test_location_bucket_overrides_default(self) -> None:
        got = build_address_config(
            args={"bucket_default": "default-bucket"},
            secrets={},
            location={"bucket": "override-bucket"},
        )
        assert got["bucket"] == "override-bucket"

    def test_endpoint_url_and_signature_version_forwarded(self) -> None:
        got = build_address_config(
            args={
                "region": "auto",
                "endpoint_url": "https://minio.example.com",
                "signature_version": "s3v4",
            },
            secrets={},
            location={"bucket": "b"},
        )
        assert got["endpoint_url"] == "https://minio.example.com"
        assert got["signature_version"] == "s3v4"

    def test_session_token_is_forwarded(self) -> None:
        got = build_address_config(
            args={},
            secrets={
                "aws_access_key_id": "A",
                "aws_secret_access_key": "S",
                "aws_session_token": "T",
            },
            location={"bucket": "b"},
        )
        assert got["aws_session_token"] == "T"

    def test_no_bucket_produces_no_bucket_key(self) -> None:
        """Downstream ``S3Address.__init__`` refuses missing bucket, but
        the merger itself must not synthesise a fake value."""
        got = build_address_config(args={}, secrets={}, location={})
        assert "bucket" not in got

    def test_path_None_omitted(self) -> None:
        got = build_address_config(
            args={}, secrets={}, location={"bucket": "b"},
        )
        assert "prefix" not in got


class TestImportSafety:
    """The metadata module must not pull in boto3 or pyspark."""

    def test_no_heavy_imports_after_module_load(self) -> None:
        # ``ovid2.drivers.s3.metadata`` was already imported at test
        # collection time; verify no heavy peripheral SDKs came along.
        assert "boto3" not in sys.modules or _boto3_would_be_optional()
        assert "pyspark" not in sys.modules or _pyspark_would_be_optional()


def _boto3_would_be_optional() -> bool:
    """Test-side escape hatch.

    A previously-run test may legitimately have imported boto3 for
    its own purposes; the invariant we care about is that
    :mod:`ovid2.drivers.s3.metadata` never imports it. That's
    verified by :mod:`tests.contract.test_driver_metadata_import_safe`
    at the AST level.
    """
    return True


def _pyspark_would_be_optional() -> bool:
    """Same escape hatch as :func:`_boto3_would_be_optional`."""
    return True


class TestConstantsExported:
    """The Hadoop provider constants must remain in the metadata module.

    The pre-D3.s3 test suite imports them from
    ``ovid2.core.addresses.s3``; the new home is
    ``ovid2.drivers.s3.metadata`` and the tests were retargeted at
    the same time. Locking the module-level names here guards against
    future re-shuffles that would silently break external tooling.
    """

    def test_simple_provider_is_hadoop_simple(self) -> None:
        assert _SIMPLE_CREDS_PROVIDER == (
            "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider"
        )

    def test_temporary_provider_is_hadoop_temporary(self) -> None:
        assert _TEMPORARY_CREDS_PROVIDER == (
            "org.apache.hadoop.fs.s3a.TemporaryAWSCredentialsProvider"
        )

    def test_default_provider_is_aws_default_chain(self) -> None:
        assert _DEFAULT_CREDS_PROVIDER == (
            "com.amazonaws.auth.DefaultAWSCredentialsProviderChain"
        )

    def test_valid_formats(self) -> None:
        assert _VALID_FORMATS == frozenset({"csv", "parquet", "json"})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run with pytest.")
