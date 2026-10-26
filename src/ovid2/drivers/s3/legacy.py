"""Legacy connection + address classes for the S3 driver.

Keeps the pre-D3.s3 :class:`BaseConnection` /
:class:`BaseAddress` shape alive for the two callers that still need
them:

- :mod:`ovid2.config.connections` — instantiates a connection via
  :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` (dotted path).
- :mod:`ovid2.config.addresses` — instantiates an address via
  :data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` (dotted path).

Both catalogues are retargeted to point at the classes in this module
so every existing import site (``from ovid2.connections.s3 import
S3Connection`` or ``from ovid2.core.addresses.s3 import S3Address``)
becomes a one-liner rewrite ``from ovid2.drivers.s3 import
S3Connection`` / ``from ovid2.drivers.s3 import S3Address`` with no
behaviour change.

The pipeline-side :class:`S3Address` reader/writer delegates to
:class:`~ovid2.drivers.s3.spark.S3IO` so the pyspark import stays
confined to that single file, keeping ``test_no_pyspark_outside_engine``
green.
"""

from __future__ import annotations

from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult
from ovid2.core.addresses.base import BaseAddress
from ovid2.drivers.s3.metadata import build_address_config, resolve_auth_mode
from ovid2.drivers.s3.probe import probe_auth_from_config

# NOTE: this module deliberately does NOT import ``pyspark.sql`` — even
# under ``TYPE_CHECKING``. The contract test
# :mod:`tests.contract.test_no_pyspark_outside_engine` inspects imports
# via AST and does not distinguish TYPE_CHECKING guards. Signatures
# below use ``Any`` for the DataFrame / SparkSession types; the real
# type contract is enforced by :class:`BaseAddress`.


class S3Connection(BaseConnection):
    """S3 (and S3-compatible) endpoint. Secrets: AWS credential triplet.

    Args (from the JSON):
        region (str, required): AWS region (e.g. ``us-east-1``).
        endpoint_url (str, optional): Override for S3-compatible
            services (MinIO, R2, Spaces).
        bucket_default (str, optional): Default bucket the address
            picks up when its own ``location.bucket`` is blank.
        signature_version (str, optional): e.g. ``s3v4``.

    Secrets:
        aws_access_key_id (str, optional*).
        aws_secret_access_key (str, optional*).
        aws_session_token (str, optional): STS session token.

    *When neither ``aws_access_key_id`` nor ``aws_secret_access_key``
    is set, the pipeline falls back to the AWS SDK default provider
    chain (EC2 metadata, ECS/EKS task credentials, ``AWS_*`` env, …).
    """

    type_name = "s3"

    def test(self) -> ConnectionTestResult:
        """Probe by listing at most one object with :mod:`boto3`."""
        probe = probe_auth_from_config(dict(self.args), dict(self.secrets))
        return ConnectionTestResult(
            ok=probe.ok,
            code=probe.code,
            message=probe.message,
            detail=dict(probe.detail),
        )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Merge connection args + secrets with a per-address bucket + prefix."""
        return build_address_config(
            dict(self.args),
            dict(self.secrets),
            dict(location),
        )


class S3Address(BaseAddress):
    """S3 address backed by the Hadoop ``s3a://`` driver.

    Delegates every I/O method to :class:`~ovid2.drivers.s3.spark.S3IO`
    — the only class allowed to import pyspark under
    :mod:`ovid2.drivers.s3`. Config validation (``bucket`` required)
    happens eagerly in ``__init__`` so a mis-authored address raises
    before a Spark session is touched.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        # Import lazily so this module stays import-safe; the class
        # itself never imports pyspark.
        from ovid2.drivers.s3.spark import S3IO

        self._io: S3IO = S3IO(config)

        # Re-expose the fields the pre-D3.s3 test suite reads via
        # ``a.bucket``, ``a.prefix``, ``a.auth_mode``, etc. Keeps the
        # 43-case unit-test module green with a single import-path
        # change.
        self.bucket: str = self._io.bucket
        self.prefix: str = self._io.prefix
        self.region: str | None = self._io.region
        self.endpoint_url: str | None = self._io.endpoint_url
        self.signature_version: str | None = self._io.signature_version
        self.format: str = self._io.format
        self.lowercase_columns: bool = self._io.lowercase_columns
        self.auth_mode: str = self._io.auth_mode

        # Squirrel raw config away — the pre-refactor tests occasionally
        # read ``a._access_key_id`` via double-underscore-free names.
        self._access_key_id: str | None = self._io._access_key_id
        self._secret_access_key: str | None = self._io._secret_access_key
        self._session_token: str | None = self._io._session_token

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _nonempty(value: Any) -> str | None:
        """Return ``value`` as a stripped string or ``None`` when blank.

        Preserved as a static-method attribute of :class:`S3Address` for
        the pre-refactor tests that call ``S3Address._nonempty(...)``
        directly.
        """
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _resolve_auth_mode(self) -> str:
        """Delegate to :func:`resolve_auth_mode` for the config triple."""
        return resolve_auth_mode(
            {
                "aws_access_key_id": self._access_key_id,
                "aws_secret_access_key": self._secret_access_key,
                "aws_session_token": self._session_token,
            }
        )

    def _base_uri(self) -> str:
        """Return the ``s3a://<bucket>/`` prefix (no path)."""
        return self._io._base_uri()

    def _resolve_read_path(
        self,
        pipeline: str,
        batch_id: str,
        file_name: str,
    ) -> str:
        """Assemble the fully-qualified read path."""
        return self._io._resolve_read_path(pipeline, batch_id, file_name)

    def _resolve_write_dir(self, pipeline: str, batch_id: str) -> str:
        """Return the write directory URI (no trailing file name)."""
        return self._io._resolve_write_dir(pipeline, batch_id)

    def _configure_spark(self, spark: Any) -> None:
        """Push credentials + auth-provider settings onto ``spark``."""
        self._io._configure_spark(spark)

    # ------------------------------------------------------------------ read

    def read(
        self,
        spark: Any,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        """Read a lazy Spark DataFrame from S3.

        ``table_name`` is accepted for interface uniformity with
        :class:`SnowflakeAddress` but ignored — S3 reads always
        resolve through ``file_name`` and the bucket layout.
        """
        del table_name
        return self._io.read(spark, pipeline, batch_id, file_name, delimiter)

    # ----------------------------------------------------------------- write

    def write(
        self,
        df: Any,
        spark: Any,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write a DataFrame directly under the address location ``{prefix}/``."""
        self._io.write(df, spark, pipeline, batch_id, table_name, delimiter)


__all__ = ["S3Address", "S3Connection"]
