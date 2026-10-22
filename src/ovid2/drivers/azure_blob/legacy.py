"""Legacy connection + address classes for the Azure Blob driver.

Keeps the pre-D3.azure_blob :class:`BaseConnection` /
:class:`BaseAddress` shape alive for the two callers that still need
them:

- :mod:`ovid2.config.connections` — instantiates a connection via
  :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` (dotted path).
- :mod:`ovid2.config.addresses` — instantiates an address via
  :data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` (dotted path).

Both catalogues are retargeted to point at the classes in this module
so every existing import site
(``from ovid2.connections.azure_blob import AzureBlobConnection`` or
``from ovid2.core.addresses.azure_blob import AzureBlobAddress``)
becomes a one-liner rewrite
``from ovid2.drivers.azure_blob import AzureBlobConnection`` /
``from ovid2.drivers.azure_blob import AzureBlobAddress`` with no
behaviour change.

The pipeline-side :class:`AzureBlobAddress` reader/writer delegates
to :class:`~ovid2.drivers.azure_blob.spark.AzureBlobIO` so the pyspark
import stays confined to that single file, keeping
``test_no_pyspark_outside_engine`` green.
"""

from __future__ import annotations

from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult
from ovid2.core.addresses.base import BaseAddress
from ovid2.drivers.azure_blob.metadata import (
    build_address_config,
    resolve_address_auth_mode,
    resolve_connection_auth_mode,
)
from ovid2.drivers.azure_blob.probe import (
    _AUTH_HINTS,
    _PROBE_TIMEOUT_SECONDS,
    _build_client,
    _fast_fail_kwargs,
    probe_auth_from_config,
)

# NOTE: this module deliberately does NOT import ``pyspark.sql`` — even
# under ``TYPE_CHECKING``. The contract test
# :mod:`tests.contract.test_no_pyspark_outside_engine` inspects imports
# via AST and does not distinguish TYPE_CHECKING guards. Signatures
# below use ``Any`` for the DataFrame / SparkSession types; the real
# type contract is enforced by :class:`BaseAddress`.


class AzureBlobConnection(BaseConnection):
    """Azure Blob Storage endpoint.

    Non-secret args:
        ``account_name``, ``container_default``, ``endpoint_suffix``.

    Secrets (any one auth mode):
        ``connection_string`` | ``account_key`` | ``sas_token`` |
        (``tenant_id`` + ``client_id`` + ``client_secret``).

    Preserves the pre-D3.azure_blob private-method surface
    (``_auth_mode``, ``_has_service_principal``, ``_build_client``,
    ``_fast_fail_kwargs``) that the existing unit-test suite reads.
    """

    type_name = "azure_blob"

    # ------------------------------------------------------------------
    # Auth helpers (preserved private surface — tests read these)
    # ------------------------------------------------------------------

    def _has_service_principal(self) -> bool:
        """Return True when all three SPN secrets are populated."""
        return bool(
            self.secrets.get("tenant_id")
            and self.secrets.get("client_id")
            and self.secrets.get("client_secret")
        )

    def _auth_mode(self) -> str:
        """Return the auth mode the connector will use for the probe.

        One of ``connection_string``, ``account_key``, ``sas_token``,
        ``service_principal``, ``default_chain``.
        """
        return resolve_connection_auth_mode(dict(self.secrets))

    def _build_client(self, url: str) -> Any:
        """Return a ready ``BlobServiceClient`` for the configured mode.

        Delegates to :func:`~ovid2.drivers.azure_blob.probe._build_client`
        so the SDK-lazy pattern is defined in exactly one place.
        """
        return _build_client(
            self._auth_mode(),
            dict(self.args),
            dict(self.secrets),
            url,
        )

    # ------------------------------------------------------------------
    # Probe
    # ------------------------------------------------------------------

    def test(self) -> ConnectionTestResult:
        """Probe the account (and default container, if any)."""
        probe = probe_auth_from_config(dict(self.args), dict(self.secrets))
        return ConnectionTestResult(
            ok=probe.ok,
            code=probe.code,
            message=probe.message,
            detail=dict(probe.detail),
        )

    # ------------------------------------------------------------------
    # Legacy-address bridge
    # ------------------------------------------------------------------

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Merge account credentials with per-address container + path."""
        return build_address_config(
            dict(self.args),
            dict(self.secrets),
            dict(location),
        )


class AzureBlobAddress(BaseAddress):
    """ADLS Gen2 address backed by the Hadoop ABFSS driver.

    Delegates every I/O method to
    :class:`~ovid2.drivers.azure_blob.spark.AzureBlobIO` — the only
    class allowed to import pyspark under
    :mod:`ovid2.drivers.azure_blob`. Config validation happens
    eagerly in ``__init__`` so a mis-authored address raises before a
    Spark session is touched.

    Preserves the pre-D3.azure_blob attribute surface the existing
    unit-test suite reads (``auth_mode``, ``prefix``, ``format``,
    ``lowercase_columns``, the six private cred fields, and the four
    private path/config helpers).
    """

    def __init__(self, config: dict[str, Any]) -> None:
        # Import lazily so this module stays import-safe; the class
        # itself never imports pyspark.
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        self._io: AzureBlobIO = AzureBlobIO(config)

        # Re-expose the fields the pre-D3.azure_blob test suite reads
        # via ``addr.auth_mode``, ``addr.prefix``, etc.
        self.account_name: str = self._io.account_name
        self.container: str = self._io.container
        self.prefix: str = self._io.prefix
        self.endpoint_suffix: str = self._io.endpoint_suffix
        self.format: str = self._io.format
        self.lowercase_columns: bool = self._io.lowercase_columns
        self.auth_mode: str = self._io.auth_mode

        # Squirrel raw config away — the pre-refactor tests occasionally
        # read the ``_connection_string`` / ``_account_key`` / etc.
        # attributes directly.
        self._connection_string: str | None = self._io._connection_string
        self._account_key: str | None = self._io._account_key
        self._sas_token: str | None = self._io._sas_token
        self._tenant_id: str | None = self._io._tenant_id
        self._client_id: str | None = self._io._client_id
        self._client_secret: str | None = self._io._client_secret

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _nonempty(value: Any) -> str | None:
        """Return ``value`` as a stripped string or ``None`` when blank.

        Preserved as a static-method attribute of :class:`AzureBlobAddress`
        for the pre-refactor tests that call ``AzureBlobAddress._nonempty(...)``
        directly.
        """
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _resolve_auth_mode(self) -> str:
        """Delegate to :func:`resolve_address_auth_mode`."""
        return resolve_address_auth_mode(
            {
                "connection_string": self._connection_string,
                "account_key": self._account_key,
                "sas_token": self._sas_token,
                "tenant_id": self._tenant_id,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
        )

    def _fs_host(self) -> str:
        """Return the ``<account>.dfs.<suffix>`` fully-qualified host."""
        return self._io._fs_host()

    def _base_uri(self) -> str:
        """Return the ``abfss://<container>@<host>/`` prefix (no path)."""
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
        """Read a lazy Spark DataFrame from ADLS Gen2.

        ``table_name`` is accepted for interface uniformity with
        :class:`SnowflakeAddress` but ignored — blob reads resolve
        through ``file_name`` and the container layout.
        """
        del table_name  # unused; blob reads always use file_name
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
        """Write a DataFrame under ``{prefix}/{pipeline}/{batch_id}/``."""
        self._io.write(df, spark, pipeline, batch_id, table_name, delimiter)


# Re-export private helpers the pre-D3.azure_blob tests import.
__all__ = [
    "_AUTH_HINTS",
    "_PROBE_TIMEOUT_SECONDS",
    "AzureBlobAddress",
    "AzureBlobConnection",
    "_build_client",
    "_fast_fail_kwargs",
]
