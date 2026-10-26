"""Native ``S3Driver`` — control-plane + data-plane in one class.

Replaces the D2-era :class:`~ovid2.drivers.legacy_adapter.LegacyDriverAdapter_s3`
entry in :data:`ovid2.drivers.registry.DRIVERS`. Metadata lives in
:mod:`ovid2.drivers.s3.metadata` (import-safe, no boto3, no pyspark);
probes live in :mod:`ovid2.drivers.s3.probe` (imports boto3 lazily);
data plane lives in :mod:`ovid2.drivers.s3.spark` (the *only* file
allowed to import pyspark under this package).

Legacy bridge: :meth:`S3Driver.as_legacy_src_args` returns the
``(src_name, src_args)`` shape :class:`~ovid2.drivers.s3.legacy.S3Address`
consumes so any caller still routing through
:data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` keeps working
unchanged.

Container requirements: S3 needs ``hadoop-aws`` + the AWS SDK bundle
on the JVM classpath. Those jars are baked into the Bitnami Spark
base image today, so :meth:`S3Driver.container_requirements` returns
an empty tuple — the declaration is a follow-up (adding a
:class:`JavaPackage` here would require the collector's
``spark.jars.packages`` wiring, which is the E2 milestone).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.drivers._table_listing import PrefixTableListingMixin
from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    LocationField,
    ProbeResult,
    StorageEntry,
)
from ovid2.drivers.s3.metadata import (
    build_address_config,
    resolve_auth_mode,
)
from ovid2.drivers.s3.metadata import (
    connection_fields as _connection_fields,
)
from ovid2.drivers.s3.metadata import (
    location_fields as _location_fields,
)
from ovid2.drivers.s3.probe import (
    list_dir_from_config,
    list_tables_from_config,
    probe_auth_from_config,
)

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.s3.spark import S3IO


class S3Driver(PrefixTableListingMixin, Driver):
    """Native driver for the ``s3`` peripheral.

    Constructor keeps the uniform ``(args, secrets, location)`` shape
    from :class:`Driver`. ``args`` carries the connection body
    (``region`` / ``endpoint_url`` / ``bucket_default`` /
    ``signature_version``); ``secrets`` carries the AWS credential
    triple; ``location`` carries the per-address payload
    (``bucket`` / ``path`` / ``format``).
    """

    type_name: str = "s3"

    # ------------------------------------------------------------------
    # Metadata (import-safe)
    # ------------------------------------------------------------------

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        """Return the S3 connection form field spec."""
        return _connection_fields()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        """Return the S3 location form field spec."""
        return _location_fields()

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def probe_auth(self) -> ProbeResult:
        """Probe by listing at most one object with :mod:`boto3`.

        Uses :func:`~ovid2.drivers.s3.probe.probe_auth_from_config` so
        the boto3 import stays lazy; a host without boto3 gets a
        ``code='skipped'`` result rather than an import error.
        """
        return probe_auth_from_config(dict(self.args), dict(self.secrets))

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List prefixes + objects one level under ``prefix``.

        Delegates to :func:`~ovid2.drivers.s3.probe.list_dir_from_config`
        so the boto3 import stays lazy.
        """
        return list_dir_from_config(
            dict(self.args), dict(self.secrets), dict(self.location), prefix
        )

    def _list_tables(self, sub_prefix: str) -> list[StorageEntry]:
        """Size-aggregated listing under ``sub_prefix`` (see :class:`PrefixTableListingMixin`)."""
        return list_tables_from_config(
            dict(self.args), dict(self.secrets), dict(self.location), sub_prefix
        )

    # ------------------------------------------------------------------
    # Introspection (debug / display)
    # ------------------------------------------------------------------

    def auth_mode(self) -> str:
        """Return the auth mode this driver would use for its data plane.

        Not part of the :class:`Driver` protocol — a convenience for
        the web tier's connection detail page and for the unit tests
        that lock in the classification.
        """
        return resolve_auth_mode(
            {
                "aws_access_key_id": self.secrets.get("aws_access_key_id"),
                "aws_secret_access_key": self.secrets.get("aws_secret_access_key"),
                "aws_session_token": self.secrets.get("aws_session_token"),
            }
        )

    # ------------------------------------------------------------------
    # Legacy bridge
    # ------------------------------------------------------------------

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        """Return the ``(src_name, src_args)`` shape ``S3Address`` consumes.

        Merges the connection args, secrets, and per-address location
        exactly the way
        :meth:`~ovid2.drivers.s3.legacy.S3Connection.as_address_config`
        did before the driver refactor.
        """
        merged = build_address_config(
            dict(self.args),
            dict(self.secrets),
            dict(self.location),
        )
        return "s3", merged

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    def _build_io(self) -> S3IO:
        """Construct a fresh :class:`S3IO` for the current args + location.

        Cached on the instance so successive ``read`` / ``write``
        calls preserve the ``_configured_sessions`` bookkeeping across
        invocations against the same driver.
        """
        cached = getattr(self, "_io_cache", None)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        # Local import so the driver module stays pyspark-free.
        from ovid2.drivers.s3.spark import S3IO

        _, merged = self.as_legacy_src_args()
        io = S3IO(merged)
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
        """Read a lazy DataFrame from S3 via the engine session."""
        del table_name  # unused; S3 reads always use file_name
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
        """Materialise ``df`` directly under the address location ``{prefix}/``."""
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
# ``LegacyDriverAdapter_s3`` fallback if this module's import order
# put it after the legacy sweep — no-ops otherwise.
from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("s3")
_register("s3", "ovid2.drivers.s3.driver.S3Driver")


__all__ = ["S3Driver"]
