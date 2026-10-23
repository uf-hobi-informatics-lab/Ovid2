"""Native ``SnowflakeDriver`` — control-plane + data-plane in one class.

Replaces the D2-era
:class:`~ovid2.drivers.legacy_adapter.LegacyDriverAdapter_snowflake`
entry in :data:`ovid2.drivers.registry.DRIVERS`. Metadata lives in
:mod:`ovid2.drivers.snowflake.metadata` (import-safe, no snowflake-*,
no pyspark); probes live in :mod:`ovid2.drivers.snowflake.probe`
(imports snowflake-connector-python + cryptography lazily); data
plane lives in :mod:`ovid2.drivers.snowflake.spark` (the *only* file
allowed to import pyspark under this package).

Legacy bridge: :meth:`SnowflakeDriver.as_legacy_src_args` returns the
``(src_name, src_args)`` shape :class:`SnowflakeAddress` consumes so
any caller still routing through
:data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` keeps working
unchanged. The four-tier Snowflake resolver stays where it is (in
:mod:`ovid2.config.addresses`) — it's the loader's responsibility,
not the driver's, and is applied uniformly regardless of whether the
caller resolves through ``load_address`` or through
:func:`ovid2.drivers.registry.driver_from_address`.

D6.0 advisory type stamping: :meth:`SnowflakeDriver.declared_cdm`
returns ``location.get("cdm")`` so :func:`validate_route_types` can
warn when the source and target of a route disagree on the active
CDM. The pipeline-side :class:`SnowflakeAddress` reads the same
``cdm`` key from its config at ``__init__`` time (attribute
:attr:`_cdm_name`) so the C1 typed DDL uses the right namespace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    LocationField,
    ProbeResult,
    StorageEntry,
)
from ovid2.drivers.snowflake.metadata import (
    build_address_config,
)
from ovid2.drivers.snowflake.metadata import (
    connection_fields as _connection_fields,
)
from ovid2.drivers.snowflake.metadata import (
    location_fields as _location_fields,
)
from ovid2.drivers.snowflake.probe import (
    list_dir_from_config,
    list_schema_tables_from_config,
    probe_auth_from_config,
)

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.snowflake.spark import SnowflakeAddress


class SnowflakeDriver(Driver):
    """Native driver for the ``snowflake`` peripheral.

    Constructor keeps the uniform ``(args, secrets, location)`` shape
    from :class:`Driver`. ``args`` carries the connection body
    (``account`` / ``user`` / ``warehouse`` / ``role`` /
    ``authenticator`` / ``credentials_dir`` /
    ``private_key_file`` / ``private_key_passphrase_file``);
    ``secrets`` carries the optional ``password``; ``location``
    carries the per-address payload (``database`` / ``schema`` /
    optional ``cdm`` stamp).
    """

    type_name: str = "snowflake"

    # ------------------------------------------------------------------
    # Metadata (import-safe)
    # ------------------------------------------------------------------

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        """Return the snowflake connection form field spec."""
        return _connection_fields()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        """Return the snowflake location form field spec."""
        return _location_fields()

    @classmethod
    def declared_cdm(cls, location: dict[str, Any] | None) -> str | None:
        """Return the CDM stamp on ``location`` (D6.0 advisory type check).

        Snowflake is the primary CDM-carrying peripheral in the
        pipeline — writes land in a pipeline-owned schema whose typed
        DDL comes from the active CDM. The C1 phase threads the CDM
        name onto every address via ``ovid2 etl --cdm``; the D6.0
        route-validator uses this classmethod to warn when a route
        writes into a Snowflake address whose stamped CDM disagrees
        with the source's.
        """
        if not location:
            return None
        v = location.get("cdm")
        return str(v) if v else None

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def probe_auth(self) -> ProbeResult:
        """Probe via a ``SELECT 1`` round-trip against Snowflake.

        Uses :func:`~ovid2.drivers.snowflake.probe.probe_auth_from_config`
        so the ``snowflake-connector-python`` import stays lazy; a
        host without the driver gets a ``code='skipped'`` result
        rather than an ``ImportError`` at startup.
        """
        return probe_auth_from_config(dict(self.args), dict(self.secrets))

    # ------------------------------------------------------------------
    # Storage browse
    # ------------------------------------------------------------------

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """Browse the account as a database → schema → table hierarchy.

        Delegates to
        :func:`~ovid2.drivers.snowflake.probe.list_dir_from_config`
        so the ``snowflake-connector-python`` import stays lazy. See
        that function for the prefix grammar.
        """
        return list_dir_from_config(dict(self.args), dict(self.secrets), prefix)

    # ------------------------------------------------------------------
    # ``all`` expansion + size hints (control plane, ``SHOW`` only)
    # ------------------------------------------------------------------

    @classmethod
    def supports_expand_all(cls) -> bool:
        """A Snowflake schema is enumerable (``SHOW TABLES``), so ``all`` expands."""
        return True

    def _database_schema(self, location: dict[str, Any] | None = None) -> tuple[str, str]:
        """Return ``(database, schema)`` the read path targets.

        Same precedence as :func:`~ovid2.drivers.snowflake.metadata.build_address_config`:
        explicit ``database`` / ``schema`` keys win over a ``path`` of the
        form ``DATABASE[/SCHEMA]``. Schema defaults to ``PUBLIC`` (the
        connection default ``read`` falls back to).
        """
        loc = dict(location if location is not None else self.location)
        parts = [p for p in str(loc.get("path") or "").split("/") if p]
        database = str(loc.get("database") or (parts[0] if parts else "") or "")
        schema = str(loc.get("schema") or (parts[1] if len(parts) > 1 else "") or "PUBLIC")
        return database, schema

    def _schema_tables(self, location: dict[str, Any] | None = None) -> list[StorageEntry]:
        """List every table / view (with sizes) in the address's schema."""
        database, schema = self._database_schema(location)
        return list_schema_tables_from_config(dict(self.args), dict(self.secrets), database, schema)

    def expand_all_tables(
        self,
        location: dict[str, Any] | None = None,
        *,
        pipeline: str | None = None,
        batch_id: str | None = None,
    ) -> list[str] | None:
        """Expand ``all`` to every table + view in the address's schema.

        ``pipeline`` / ``batch_id`` are ignored: Snowflake reads come from
        the address's own ``database.schema`` (see
        :meth:`SnowflakeAddress.read`), not a batch-scoped path.

        Raises:
            ValueError: When the address has no database.
        """
        del pipeline, batch_id
        return [e.name for e in self._schema_tables(location)]

    def table_sizes(
        self,
        file_names: list[str],
        *,
        pipeline: str,
        batch_id: str,
    ) -> dict[str, int]:
        """Return ``SHOW TABLES`` ``bytes`` per table (case-insensitive match)."""
        del pipeline, batch_id
        by_upper = {
            e.name.upper(): e.size_bytes for e in self._schema_tables() if e.size_bytes is not None
        }
        out: dict[str, int] = {}
        for name in file_names:
            size = by_upper.get(name.upper())
            if size is not None:
                out[name] = size
        return out

    # ------------------------------------------------------------------
    # Legacy bridge
    # ------------------------------------------------------------------

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        """Return the ``(src_name, src_args)`` shape ``SnowflakeAddress`` consumes.

        Merges the connection args, secrets, and per-address location
        exactly the way
        :meth:`~ovid2.drivers.snowflake.legacy.SnowflakeConnection.as_address_config`
        did before the driver refactor (including the PEM → DER
        conversion of the private key material).
        """
        merged = build_address_config(
            dict(self.args),
            dict(self.secrets),
            dict(self.location),
        )
        return "snowflake", merged

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    def _build_address(self) -> SnowflakeAddress:
        """Construct a fresh :class:`SnowflakeAddress` for the current config.

        Cached on the instance so successive ``read`` / ``write``
        calls preserve state across invocations against the same
        driver.
        """
        cached = getattr(self, "_address_cache", None)
        if cached is not None:
            return cached  # type: ignore[no-any-return]
        # Local import so the driver module stays pyspark-free.
        from ovid2.drivers.snowflake.spark import SnowflakeAddress

        _, merged = self.as_legacy_src_args()
        addr = SnowflakeAddress(merged)
        self._address_cache = addr
        return addr

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
        """Read a Snowflake table via the engine session."""
        addr = self._build_address()
        return addr.read(
            session,  # type: ignore[arg-type]  # EngineSession wraps a SparkSession at runtime
            pipeline,
            batch_id,
            file_name,
            delimiter,
            table_name=table_name,
        )

    def write(
        self,
        df: Any,
        session: EngineSession,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write ``df`` into ``{database}.OVID_{pipeline}_{batch_id}.{table_name}``."""
        addr = self._build_address()
        addr.write(df, session, pipeline, batch_id, table_name, delimiter)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

# Register at module bottom so importing this module transparently
# populates the driver registry. Idempotent when the entry is already
# ours; refuses to overwrite a different path (see
# :func:`ovid2.drivers.registry.register`). We ``unregister`` first
# because the D2 :func:`~ovid2.drivers.legacy_adapter.register_all_legacy_drivers`
# runs during registry-init and may have inserted the
# ``LegacyDriverAdapter_snowflake`` fallback if this module's import
# order put it after the legacy sweep — no-ops otherwise.
from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("snowflake")
_register("snowflake", "ovid2.drivers.snowflake.driver.SnowflakeDriver")


__all__ = ["SnowflakeDriver"]
