"""Native ``AzureBlobDriver`` — control-plane + data-plane in one class.

Replaces the D2-era
:class:`~ovid2.drivers.legacy_adapter.LegacyDriverAdapter_azure_blob`
entry in :data:`ovid2.drivers.registry.DRIVERS`. Metadata lives in
:mod:`ovid2.drivers.azure_blob.metadata` (import-safe, no azure-*,
no pyspark); probes live in :mod:`ovid2.drivers.azure_blob.probe`
(imports azure-storage-blob + azure-identity lazily); data plane
lives in :mod:`ovid2.drivers.azure_blob.spark` (the *only* file
allowed to import pyspark under this package).

Legacy bridge: :meth:`AzureBlobDriver.as_legacy_src_args` returns the
``(src_name, src_args)`` shape
:class:`~ovid2.drivers.azure_blob.legacy.AzureBlobAddress` consumes
so any caller still routing through
:data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` keeps working
unchanged.

Container requirements: the ABFS driver ships as part of the Bitnami
Spark base image today, so :meth:`AzureBlobDriver.container_requirements`
returns an empty tuple — a :class:`JavaPackage` declaration is
deferred until the E2 ``spark.jars.packages`` wiring lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.drivers.azure_blob.metadata import (
    build_address_config,
    resolve_address_auth_mode,
    resolve_connection_auth_mode,
)
from ovid2.drivers.azure_blob.metadata import (
    connection_fields as _connection_fields,
)
from ovid2.drivers.azure_blob.metadata import (
    location_fields as _location_fields,
)
from ovid2.drivers.azure_blob.probe import (
    list_dir_from_config,
    probe_auth_from_config,
)
from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    LocationField,
    ProbeResult,
    StorageEntry,
)

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.azure_blob.spark import AzureBlobIO


class AzureBlobDriver(Driver):
    """Native driver for the ``azure_blob`` peripheral.

    Constructor keeps the uniform ``(args, secrets, location)`` shape
    from :class:`Driver`. ``args`` carries the connection body
    (``account_name`` / ``container_default`` / ``endpoint_suffix``);
    ``secrets`` carries the credential dict (any of connection_string,
    account_key, sas_token, or SPN trio); ``location`` carries the
    per-address payload (``container`` / ``path`` / ``format``).
    """

    type_name: str = "azure_blob"

    # ------------------------------------------------------------------
    # Metadata (import-safe)
    # ------------------------------------------------------------------

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        """Return the azure_blob connection form field spec."""
        return _connection_fields()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        """Return the azure_blob location form field spec."""
        return _location_fields()

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def probe_auth(self) -> ProbeResult:
        """Probe using the Azure SDK — see :func:`probe_auth_from_config`."""
        return probe_auth_from_config(dict(self.args), dict(self.secrets))

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List blob prefixes + blobs one level under ``prefix``.

        Delegates to
        :func:`~ovid2.drivers.azure_blob.probe.list_dir_from_config` so
        the ``azure-storage-blob`` import stays lazy.
        """
        return list_dir_from_config(
            dict(self.args), dict(self.secrets), dict(self.location), prefix
        )

    # ------------------------------------------------------------------
    # Introspection (debug / display)
    # ------------------------------------------------------------------

    def auth_mode(self) -> str:
        """Return the auth mode this driver's data plane would use.

        Not part of the :class:`Driver` protocol — a convenience for
        the web tier's connection detail page and for the unit tests
        that lock in the classification. Uses the address-side
        classifier so the fifth leg reads as ``managed_identity``
        (the driver runs the pipeline; the probe uses the
        connection-side classifier's ``default_chain`` label).
        """
        # Fuse secrets + args because the classifier reads its inputs
        # from a merged dict.
        merged: dict[str, Any] = {}
        for k in (
            "connection_string",
            "account_key",
            "sas_token",
            "tenant_id",
            "client_id",
            "client_secret",
        ):
            v = self.secrets.get(k)
            if v is not None:
                merged[k] = v
        return resolve_address_auth_mode(merged)

    def probe_auth_mode(self) -> str:
        """Return the connection-side auth mode label.

        Distinct from :meth:`auth_mode` on the fifth leg only:
        ``default_chain`` here vs ``managed_identity`` there. Matches
        :meth:`AzureBlobConnection._auth_mode`.
        """
        return resolve_connection_auth_mode(dict(self.secrets))

    # ------------------------------------------------------------------
    # Legacy bridge
    # ------------------------------------------------------------------

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        """Return the ``(src_name, src_args)`` shape ``AzureBlobAddress`` consumes.

        Merges the connection args, secrets, and per-address location
        exactly the way
        :meth:`AzureBlobConnection.as_address_config` did before the
        driver refactor.
        """
        merged = build_address_config(
            dict(self.args),
            dict(self.secrets),
            dict(self.location),
        )
        return "azure_blob", merged

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    def _build_io(self) -> AzureBlobIO:
        """Construct a fresh :class:`AzureBlobIO` for the current config.

        Cached on the instance so successive ``read`` / ``write``
        calls preserve the ``_configured_sessions`` bookkeeping across
        invocations against the same driver.
        """
        cached = getattr(self, "_io_cache", None)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        # Local import so the driver module stays pyspark-free.
        from ovid2.drivers.azure_blob.spark import AzureBlobIO

        _, merged = self.as_legacy_src_args()
        io = AzureBlobIO(merged)
        self._io_cache = io
        return io

    def read(
        self,
        session: EngineSession,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        """Read a lazy DataFrame from ADLS Gen2 via the engine session."""
        del table_name  # unused; blob reads always use file_name
        io = self._build_io()
        return io.read(session, pipeline, batch_id, file_name, delimiter)  # type: ignore[arg-type]

    def write(
        self,
        df: Any,
        session: EngineSession,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Materialise ``df`` under ``{prefix}/{pipeline}/{batch_id}/``."""
        io = self._build_io()
        io.write(df, session, pipeline, batch_id, table_name, delimiter)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

# Register at module bottom so importing this module transparently
# populates the driver registry. Idempotent when the entry is already
# ours; refuses to overwrite a different path (see
# :func:`ovid2.drivers.registry.register`). We ``unregister`` first
# because the D2 :func:`~ovid2.drivers.legacy_adapter.register_all_legacy_drivers`
# runs during registry-init and may have inserted the
# ``LegacyDriverAdapter_azure_blob`` fallback if this module's import
# order put it after the legacy sweep — no-ops otherwise.
from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("azure_blob")
_register("azure_blob", "ovid2.drivers.azure_blob.driver.AzureBlobDriver")


__all__ = ["AzureBlobDriver"]
