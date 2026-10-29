"""Driver ABC + shared value objects.

The public contract every peripheral must satisfy. Concrete drivers
live under ``ovid2.drivers.<type>.driver`` and register themselves in
``ovid2.drivers.registry.DRIVERS``.

Design rules:

1. **Data-only classmethods must import nothing heavy.** Anything the
   web tier can call (``connection_fields``, ``location_fields``,
   ``container_requirements``, ``cdm_dimension_name``,
   ``declared_cdm``) must be safe to invoke without importing
   pyspark, boto3, azure, snowflake, or the like. The contract test
   ``test_driver_metadata_import_safe`` scans for violations.

2. **Probe methods are optional.** Every driver must implement
   ``probe_auth``. ``probe_read`` and ``probe_write`` may raise
   :class:`NotImplementedError`; the standard harness treats that as
   *capability absent* rather than a failure.

3. **Data-plane methods (``read``, ``write``) may import Spark
   lazily.** Concrete implementations typically live in a separate
   ``spark.py`` module the driver imports at call time so
   ``ovid2.drivers.<type>.driver`` stays import-safe for the web
   tier.

4. **Constructor is uniform.** Every driver accepts
   ``(args, secrets, location)``. ``args`` and ``secrets`` come from
   the connection JSON; ``location`` is the per-address payload
   (path, database, bucket, container). ``location`` is ``None`` when
   the driver is being used purely for control-plane operations
   (test connection, list objects).

5. **Legacy bridge.** :meth:`Driver.as_legacy_src_args` returns the
   ``(src_name, src_args)`` tuple the pre-Driver-refactor code
   consumed. This lets us route the old ``BaseAddress`` machinery
   through a Driver instance until every consumer has migrated.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession
    from ovid2.drivers.container import ContainerRequirement


# ---------------------------------------------------------------------------
# Metadata value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One input on a driver form (connection or location).

    Mirrors the shape the pre-Driver-refactor UI already consumed
    (``_FIELD_SPECS`` / ``_LOCATION_SPECS``) so the JSON round-trip is
    byte-identical.
    """

    name: str
    required: bool = False
    default: Any = None
    help: str = ""
    #: Rendering hint for the HTML form. ``str`` (default), ``bool``,
    #: ``int``, ``multiline``, or ``path_picker``.
    widget: str = "str"
    #: Rare cases where a form field is a secret. Purely informational
    #: — secret storage is decided by which list the field appears in
    #: (``args`` vs ``secrets``) on :class:`ConnectionFieldSpec`.
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form the legacy catalogues emit."""
        payload: dict[str, Any] = {"name": self.name, "required": self.required}
        if self.default is not None:
            payload["default"] = self.default
        if self.help:
            payload["help"] = self.help
        if self.widget not in {"str", ""}:
            payload["widget"] = self.widget
        return payload


@dataclass(frozen=True)
class LocationField(FieldSpec):
    """Alias for :class:`FieldSpec` in location-form context.

    Same shape as :class:`FieldSpec` — the alias exists so type hints
    read naturally at call sites (``list[LocationField]`` says
    *location*, not *any field*). Location fields also carry an
    optional ``type`` string (``str`` / ``bool`` / ``int``) consumed
    by the HTML form renderer; :attr:`FieldSpec.widget` covers the
    same ground so the two catalogues can share this class.
    """


@dataclass(frozen=True)
class ConnectionFieldSpec:
    """Full description of the *connection* form a driver expects.

    ``args`` fields land in the connection JSON's ``args`` block
    (non-secret). ``secrets`` fields land in the ``secret_ref`` JSON
    under ``$XDG_CONFIG_HOME/ovid2/secrets.d/<ref>.json`` with mode
    ``0o600``. ``note`` renders above the form as connector-level
    guidance.
    """

    args: tuple[FieldSpec, ...] = ()
    secrets: tuple[FieldSpec, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON form the legacy catalogues emit."""
        payload: dict[str, Any] = {
            "args": [f.to_dict() for f in self.args],
            "secrets": [f.to_dict() for f in self.secrets],
        }
        if self.note:
            payload["note"] = self.note
        return payload


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    """Structured outcome of a driver probe.

    Superset of :class:`ovid2.connections.base.ConnectionTestResult`
    with an added ``capability`` field so a caller (or the standard
    harness) can distinguish *auth probe passed* from *read probe
    passed* when both run in sequence.
    """

    ok: bool
    code: str
    message: str
    capability: str = "auth"  # ``auth`` | ``read`` | ``write``
    detail: dict[str, Any] = field(default_factory=dict)

    def to_legacy_test_result(self) -> Any:
        """Return the ``ConnectionTestResult`` shape the web UI expects.

        The web tier's ``test_connection`` endpoint predates the
        capability concept; downgrade to the four-field shape by
        dropping ``capability``.
        """
        # Local import to keep this module free of the connections package.
        from ovid2.connections.base import ConnectionTestResult

        return ConnectionTestResult(
            ok=self.ok,
            code=self.code,
            message=self.message,
            detail=self.detail,
        )


# ---------------------------------------------------------------------------
# Storage browse value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StorageEntry:
    """One entry returned by :meth:`Driver.list_dir`.

    A uniform shape across every storage backend so the web browser
    can render a tree without knowing whether it is looking at a
    filesystem directory, an S3 prefix, a Snowflake schema, or a
    database table.

    Attributes:
        name: The leaf name (folder / file / schema / table name).
        kind: One of ``dir`` (a navigable container — folder, prefix,
            schema, database), ``file`` (a leaf object — file, blob),
            or ``table`` (a database/warehouse table leaf).
        path: The full path/prefix to pass back to :meth:`list_dir`
            to descend into this entry (for ``dir`` kinds) or to
            reference it (for ``file`` / ``table`` kinds). Backend-
            specific but always round-trippable by the same driver.
        size_bytes: Storage size of a ``file`` / ``table`` leaf when the
            backend reports it for free in its listing metadata (file
            ``stat``, blob / object size, Snowflake ``SHOW TABLES``
            ``bytes``). ``None`` when unknown — never computed by
            scanning data.
        modified_at: ISO-8601 UTC timestamp of the last modification
            when the listing reports it for free (file ``mtime``, object
            ``LastModified``, warehouse ``last_altered``). ``None`` when
            unknown.
        row_count: Row count when the listing reports it for free
            (Snowflake ``SHOW TABLES`` ``rows``). ``None`` when unknown.
    """

    name: str
    kind: str  # ``dir`` | ``file`` | ``table``
    path: str
    size_bytes: int | None = None
    modified_at: str | None = None
    row_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict (optional fields only when known)."""
        payload: dict[str, Any] = {"name": self.name, "kind": self.kind, "path": self.path}
        if self.size_bytes is not None:
            payload["size_bytes"] = self.size_bytes
        if self.modified_at is not None:
            payload["modified_at"] = self.modified_at
        if self.row_count is not None:
            payload["row_count"] = self.row_count
        return payload


# ---------------------------------------------------------------------------
# Table introspection value objects (Data Space explorer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnInfo:
    """One column / field of a table.

    Attributes:
        name: Column name as stored.
        type: Backend-reported type (``VARCHAR(16777216)``, ``int64``),
            or an inferred one for headered text files (``string``,
            ``integer``, ``decimal``, ``date``).
        nullable: ``True`` / ``False`` when the backend reports it,
            ``None`` when unknown (text files).
    """

    name: str
    type: str = ""
    nullable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {"name": self.name, "type": self.type, "nullable": self.nullable}


@dataclass(frozen=True)
class TableInfo:
    """Metadata of one table / data file, as shown on the table detail page.

    Every field except ``name`` / ``path`` is optional: a backend fills
    what it can report *cheaply* (metadata queries, file footers,
    ``stat``) and never scans the data to do so. A full row count for
    headered text files is :meth:`Driver.count_rows`, run on demand.

    Attributes:
        name: Table or file name.
        path: The round-trippable listing path (same as the
            :class:`StorageEntry` path it came from).
        kind: ``table`` | ``view`` | ``file``.
        format: ``csv`` | ``parquet`` | ``json`` | ``table`` | ``view`` |
            ``unknown``.
        columns: The fields, in storage order.
        row_count: Row count when cheaply known.
        row_count_exact: ``False`` when ``row_count`` is an estimate.
        size_bytes: Storage size.
        modified_at: ISO-8601 UTC last-modified timestamp.
        created_at: ISO-8601 UTC creation timestamp (warehouses).
        ddl: The table definition (warehouses / databases only).
        delimiter: Detected field delimiter (text files only).
        parts: Number of part files (Spark-written directory tables).
        previewable: ``False`` when rows cannot be read (unknown file
            type); the explorer then shows stats only.
        message: Human-facing note (why something is missing).
    """

    name: str
    path: str
    kind: str = "table"
    format: str = "unknown"
    columns: tuple[ColumnInfo, ...] = ()
    row_count: int | None = None
    row_count_exact: bool = True
    size_bytes: int | None = None
    modified_at: str | None = None
    created_at: str | None = None
    ddl: str | None = None
    delimiter: str | None = None
    parts: int | None = None
    previewable: bool = True
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict."""
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "format": self.format,
            "columns": [c.to_dict() for c in self.columns],
            "row_count": self.row_count,
            "row_count_exact": self.row_count_exact,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "created_at": self.created_at,
            "ddl": self.ddl,
            "delimiter": self.delimiter,
            "parts": self.parts,
            "previewable": self.previewable,
            "message": self.message,
        }


@dataclass(frozen=True)
class TableSample:
    """The first rows of a table, stringified for display / export.

    Attributes:
        columns: Column names, in order.
        rows: Row values (``None`` for SQL ``NULL`` / empty cells).
        truncated: ``True`` when the table holds more rows than returned.
    """

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...] = ()
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict (values coerced to JSON scalars)."""
        return {
            "columns": list(self.columns),
            "rows": [[json_scalar(v) for v in row] for row in self.rows],
            "truncated": self.truncated,
        }


def json_scalar(value: Any) -> Any:
    """Coerce one cell value into a JSON-friendly scalar.

    ``None`` / ``bool`` / ``int`` / ``float`` / ``str`` pass through;
    dates, decimals, bytes and everything else are stringified so a
    sample / export never fails on an exotic type.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, (bytes, bytearray)):
        return value.hex() if len(value) <= 64 else value[:64].hex() + "…"
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return str(iso())
    return str(value)


# ---------------------------------------------------------------------------
# Driver ABC
# ---------------------------------------------------------------------------


class Driver(ABC):
    """The Ovid2 peripheral driver.

    One subclass per storage technology. See the module docstring for
    the file layout and import-safety rules.

    Subclasses **must** set :attr:`type_name` (the short registry key)
    and implement the abstract classmethods + instance methods below.
    Optional capabilities (``probe_read``, ``probe_write``, ``write``,
    ``create_complementary_tables``) may raise
    :class:`NotImplementedError`; the standard probe harness and the
    pipeline both treat that as "capability absent" rather than a
    failure.
    """

    #: Short machine name registered in :data:`ovid2.drivers.registry.DRIVERS`.
    type_name: str = ""

    # ------------------------------------------------------------------
    # Metadata (classmethods, no heavy imports)
    # ------------------------------------------------------------------

    @classmethod
    @abstractmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        """Describe the *connection* form fields for this driver.

        Consumed by the web control panel to render the new-connection
        form and by MCP tools to validate submissions.
        """

    @classmethod
    @abstractmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        """Describe the *location* form fields for this driver.

        Location payloads never carry secrets — credentials live on
        the connection. Empty tuple for drivers with no per-address
        knobs.
        """

    @classmethod
    def container_requirements(cls) -> tuple[ContainerRequirement, ...]:
        """Return the runtime pieces this driver adds to every container.

        Examples:

        - S3 → :class:`JavaPackage` for ``hadoop-aws`` + ``aws-java-sdk-bundle``.
        - Snowflake → nothing (the JDBC connector ships in the base image).
        - Disc → :class:`ExtraMount` per address (populated at
          resolution time by an override).

        Default is an empty tuple. Drivers that need *per-instance*
        requirements (like disc, whose mount targets change per
        address) may override :meth:`container_requirements_for`
        instead.
        """
        return ()

    def container_requirements_for(
        self,
        location: dict[str, Any] | None = None,
    ) -> tuple[ContainerRequirement, ...]:
        """Return per-instance container requirements.

        Default implementation returns :meth:`container_requirements`
        verbatim. Drivers whose requirements depend on the address's
        location (currently only ``disc``, whose bind-mount targets
        vary per instance) override this instead.
        """
        return type(self).container_requirements()

    @classmethod
    def cdm_dimension_name(cls) -> str:
        """Return the ODS dimension name for this driver.

        Defaults to :attr:`type_name`. Override only if the driver's
        ODS-facing name differs from the registry key.
        """
        return cls.type_name

    @classmethod
    def declared_cdm(cls, location: dict[str, Any] | None) -> str | None:
        """Return the CDM this driver declares for the given location.

        Used by D6 route validation. Default is ``None`` (untyped).
        Snowflake overrides to inspect a ``cdm`` key on the location.
        """
        return None

    @classmethod
    def workspace_database(cls, location: dict[str, Any] | None) -> str | None:
        """Return the warehouse database this location lives in, if any.

        Compute engines that create scratch objects *inside* a warehouse
        (Snowpark's temp views / transient staging tables) use this as a
        fallback namespace when their own configuration names no
        database. Pure metadata: no credentials, no I/O. The address is
        never written through this — it only names a database the role
        is expected to be able to use.

        Default ``None`` (file / object stores have no database).
        """
        del location
        return None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
        location: dict[str, Any] | None = None,
    ) -> None:
        """Store raw configuration for later use.

        Args:
            args: Non-secret connection arguments (host, region, …).
            secrets: Secret material (password, token, key material).
                May be ``None`` for drivers that need none (``disc``).
            location: Per-address payload (path, bucket, database, …).
                ``None`` when the driver is being used for pure
                control-plane operations.
        """
        self.args: dict[str, Any] = dict(args or {})
        self.secrets: dict[str, Any] = dict(secrets or {})
        self.location: dict[str, Any] = dict(location or {})

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    @abstractmethod
    def probe_auth(self) -> ProbeResult:
        """Cheap round-trip that validates credentials.

        Examples: ``HEAD`` on a bucket, ``SELECT 1`` on a database,
        ``ls`` on a directory. Called by the *Test connection* UI
        button and by the standard harness.
        """

    def probe_read(self) -> ProbeResult:
        """Optional: cheap round-trip that validates read access.

        Default raises :class:`NotImplementedError`. The standard
        harness (:mod:`ovid2.drivers.probe`) treats that as
        *capability absent* — it emits a ``code='not_implemented'``
        result but does not fail the overall probe.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement probe_read.")

    def probe_write(self) -> ProbeResult:
        """Optional: cheap round-trip that validates write access.

        Default raises :class:`NotImplementedError`. See
        :meth:`probe_read`.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement probe_write.")

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

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
        """Read data as a lazy engine DataFrame.

        Default raises :class:`NotImplementedError`. Native drivers
        override; the ``LegacyDriverAdapter`` delegates to the
        wrapped :class:`BaseAddress`.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement read().")

    def write(
        self,
        df: Any,
        session: EngineSession,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write a DataFrame to this driver's destination.

        The **only** step that materialises the lazy plan. Default
        raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement write().")

    def create_complementary_tables(
        self,
        session: EngineSession,
        pipeline: str,
        batch_id: str,
    ) -> None:
        """Create empty CDM placeholders for tables missing from this run.

        Default is a no-op; databases that need placeholders (Snowflake)
        override.
        """
        return None

    # ------------------------------------------------------------------
    # Browse (control plane — no engine session required)
    # ------------------------------------------------------------------

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List the contents of this storage account at ``prefix``.

        A control-plane capability used by the web panel to build a
        folder view of a storage account so the operator can pick a
        pipeline / batch / table graphically at pipeline-submit time.

        Args:
            prefix: The path / prefix / schema to list. ``None`` or
                empty lists the storage account's root (the connection
                root for a filesystem, the default bucket for object
                stores, the accessible schemas for a database).

        Returns:
            A list of :class:`StorageEntry`. ``dir`` entries are
            navigable (pass ``entry.path`` back as ``prefix``); ``file``
            / ``table`` entries are leaves.

        Raises:
            NotImplementedError: Default. Drivers that support browsing
                override this. The standard harness / web service
                treats the exception as *capability absent* and
                surfaces a clear "browsing not supported for <type>"
                message rather than a 500.

        Implementations must load heavy SDKs lazily inside the method
        body so the module import stays web-safe.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement list_dir.")

    @classmethod
    def supports_expand_all(cls) -> bool:
        """Return ``True`` when this driver type can expand an ``all`` request.

        A *static* capability flag (no config, no I/O, no secrets) the
        web tier can consult to decide whether to accept ``all`` for a
        ``copy`` / ``move`` source. The concrete expansion happens later
        in the executor via :meth:`expand_all_tables`; this only says
        whether that expansion is *possible* for the driver type.

        Defaults to ``False`` (the historical "reject ``all``"
        behaviour). Drivers whose source is a self-describing container
        of tables override it to ``True``.
        """
        return False

    def expand_all_tables(
        self,
        location: dict[str, Any] | None = None,
        *,
        pipeline: str | None = None,
        batch_id: str | None = None,
    ) -> list[str] | None:
        """Return the concrete table list this source expands ``all`` to.

        ``copy`` / ``move`` take an *explicit* ``--tables`` list because
        the underlying CLI has no ``all`` sentinel. A driver whose source
        can enumerate its own tables (an Access ``.accdb`` file, a
        Snowflake schema, a disc / object-store batch folder) overrides
        this so ``all`` can be expanded — by the web tier at draft time
        (via the ``ovid2 tools list-source-tables`` executor broker) or
        by the copy executor at run time.

        Args:
            location: The per-address location payload (falls back to
                ``self.location``). Lets a driver honour per-address
                allow/deny filters when expanding.
            pipeline: Accepted for interface stability; ignored. The
                address is the exact location — folder sources list
                ``{root}/{location.path}/`` the same way :meth:`read`
                does, warehouses list ``database.schema``.
            batch_id: Accepted for interface stability; ignored.

        Returns:
            The concrete list of ``--tables`` specs to copy. Each entry
            is a bare logical name or a ``LOGICAL=SOURCE`` split
            (see :mod:`ovid2.tools.table_specs`) — folder sources return
            ``demographic=demographic.csv`` so the destination stem
            drops the extension. ``None`` when this driver cannot expand
            ``all`` (the default).

        Raises:
            FileNotFoundError / ValueError: When the source location
                cannot be listed (missing folder, bad schema). Callers
                surface the message rather than silently copying
                nothing.

        Implementations must load heavy SDKs lazily inside the method
        body so the module import stays web-safe.
        """
        del location, pipeline, batch_id
        return None

    def existing_tables(self, location: dict[str, Any] | None = None) -> list[str] | None:
        """Return the tables already present at this address, for the overwrite gate.

        The address is the exact location a run writes to, so a non-empty
        result means the run would overwrite existing data and needs an
        explicit confirmation (``--overwrite`` / ``confirm_overwrite``).

        The default derives the answer from :meth:`expand_all_tables`:

        - a missing folder / schema (``FileNotFoundError``) → ``[]``;
        - a driver that cannot enumerate (``None`` / ``NotImplementedError``)
          → ``None`` (*unknown* — callers must not block on it).

        Returns:
            Logical table names already present (``[]`` when empty), or
            ``None`` when occupancy cannot be determined.
        """
        if not type(self).supports_expand_all():
            return None
        try:
            specs = self.expand_all_tables(location)
        except FileNotFoundError:
            return []
        except NotImplementedError:
            return None
        if specs is None:
            return None
        return sorted({str(s).split("=", 1)[0] for s in specs if str(s).strip()})

    def table_sizes(
        self,
        file_names: list[str],
        *,
        pipeline: str,
        batch_id: str,
    ) -> dict[str, int]:
        """Return ``{file_name: size_bytes}`` for the tables about to be copied.

        Metadata-only (file ``stat``, blob / object listing, Snowflake
        ``SHOW TABLES``) — never a data scan, never a Spark action. Used
        by ``ovid2 tools copy`` to schedule the largest tables first so
        the longest single table starts at t=0 rather than last
        (longest-processing-time-first; see
        :func:`ovid2.tools.copy.order_largest_first`).

        Args:
            file_names: Source-side lookup keys (``TableSpec.file_name``).
            pipeline: Ignored (the address is the exact location).
            batch_id: Ignored (the address is the exact location).

        Returns:
            A mapping for the names whose size is known. Missing keys
            mean "unknown" — the caller keeps their original order. The
            default returns ``{}`` (no size information).
        """
        del file_names, pipeline, batch_id
        return {}

    # ------------------------------------------------------------------
    # Table introspection (control plane — Data Space explorer)
    # ------------------------------------------------------------------
    #
    # Four optional capabilities, all keyed on the *listing path* a
    # ``file`` / ``table`` :class:`StorageEntry` carries (``entry.path``
    # from :meth:`list_dir`). They run in the executor tier (``ovid2
    # tools describe-table`` / ``sample-table`` / ``count-rows`` /
    # ``export-table``) and never need an engine session: file stores
    # read headers / parquet footers with pyarrow, warehouses run
    # metadata queries. The default raises ``NotImplementedError`` —
    # the web tier renders "not available for <type>".

    def describe_table(self, path: str) -> TableInfo:
        """Return the metadata of the table / data file at listing ``path``.

        Must be cheap: metadata queries, file ``stat``, parquet footers,
        a CSV header line. Never a full data scan.

        Raises:
            NotImplementedError: Default — capability absent.
            FileNotFoundError: When ``path`` does not exist.
            ValueError: When ``path`` is not a table / data file.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement describe_table.")

    def sample_rows(self, path: str, limit: int = 100) -> TableSample:
        """Return at most ``limit`` leading rows of the table at ``path``.

        Raises:
            NotImplementedError: Default — capability absent.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement sample_rows.")

    def iter_rows(
        self, path: str, limit: int | None = None, *, batch_size: int = 5000
    ) -> tuple[list[str], Iterator[list[tuple[Any, ...]]]]:
        """Stream the rows of the table at ``path`` (for export).

        Returns ``(columns, batches)`` where ``batches`` yields lists of
        row tuples. ``limit=None`` streams the whole table.

        The default derives a bounded stream from :meth:`sample_rows`
        (so a driver implementing only ``sample_rows`` still exports up
        to ``limit`` rows); drivers override with a true stream.

        Raises:
            NotImplementedError: When neither is implemented, or when
                ``limit`` is ``None`` and only ``sample_rows`` exists.
        """
        if limit is None:
            raise NotImplementedError(
                f"Driver '{self.type_name}' does not implement full-table streaming."
            )
        sample = self.sample_rows(path, limit)
        rows = list(sample.rows)

        def _batches() -> Iterator[list[tuple[Any, ...]]]:
            for i in range(0, len(rows), max(1, batch_size)):
                yield rows[i : i + batch_size]

        return list(sample.columns), _batches()

    def count_rows(self, path: str) -> int:
        """Return the exact row count of the table at ``path``.

        May scan the data (a CSV line count, ``SELECT COUNT(*)``) — the
        explorer only calls it when the operator presses *Count rows*.

        Raises:
            NotImplementedError: Default — capability absent.
        """
        raise NotImplementedError(f"Driver '{self.type_name}' does not implement count_rows.")

    # ------------------------------------------------------------------
    # Legacy bridge
    # ------------------------------------------------------------------

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        """Return ``(src_name, src_args)`` for pre-Driver-refactor consumers.

        The default assumes ``src_name == type_name`` and returns the
        merger of ``args``, ``secrets``, and ``location`` — which
        matches the historical shape :class:`BaseAddress` subclasses
        expected. Native drivers may override to reshape.
        """
        merged: dict[str, Any] = {}
        merged.update(self.args)
        merged.update(self.secrets)
        merged.update(self.location)
        return self.type_name, merged


__all__ = [
    "ColumnInfo",
    "ConnectionFieldSpec",
    "Driver",
    "FieldSpec",
    "LocationField",
    "ProbeResult",
    "StorageEntry",
    "TableInfo",
    "TableSample",
    "json_scalar",
]
