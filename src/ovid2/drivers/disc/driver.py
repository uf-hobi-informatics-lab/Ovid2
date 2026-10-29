"""Native ``DiscDriver`` — control-plane + data-plane in one class.

Replaces the D2-era :class:`~ovid2.drivers.legacy_adapter.LegacyDriverAdapter_disc`
entry in :data:`ovid2.drivers.registry.DRIVERS`. Metadata and
container-requirement logic live in :mod:`ovid2.drivers.disc.metadata`
(import-safe, no pyspark); probes live in
:mod:`ovid2.drivers.disc.probe`; data plane lives in
:mod:`ovid2.drivers.disc.spark` (the *only* file allowed to import
pyspark under this package).

Legacy bridge: :meth:`DiscDriver.as_legacy_src_args` returns the
``(src_name, src_args)`` shape :class:`~ovid2.drivers.disc.legacy.DiscAddress`
consumes so any caller still routing through
:data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` keeps working
unchanged.
"""

from __future__ import annotations

import os
from typing import IO, TYPE_CHECKING, Any

from ovid2.drivers._tabular import TabularFile, TabularIntrospectionMixin, iso_utc
from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    LocationField,
    ProbeResult,
    StorageEntry,
)
from ovid2.drivers.container import ContainerRequirement
from ovid2.drivers.disc.metadata import (
    connection_fields as _connection_fields,
)
from ovid2.drivers.disc.metadata import (
    container_requirements_from,
    normalise_disc_args,
)
from ovid2.drivers.disc.metadata import (
    location_fields as _location_fields,
)
from ovid2.drivers.disc.probe import (
    probe_auth_from_host_path,
    probe_read_from_host_path,
    probe_write_from_host_path,
)

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession


class DiscDriver(TabularIntrospectionMixin, Driver):
    """Native driver for the ``disc`` peripheral.

    Constructor keeps the uniform ``(args, secrets, location)`` shape
    from :class:`Driver`. ``secrets`` is always empty for disc.

    :attr:`args` carries the connection JSON body
    (``host_path`` / ``container_path`` / ``mount_mode`` OR legacy
    ``root_path``); :attr:`location` carries the per-address payload
    (``path`` sub-directory, ``format``, ``lowercase_columns``).
    """

    type_name: str = "disc"

    # ------------------------------------------------------------------
    # Metadata (import-safe)
    # ------------------------------------------------------------------

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        """Return the disc connection form field spec."""
        return _connection_fields()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        """Return the disc location form field spec."""
        return _location_fields()

    def container_requirements_for(
        self,
        location: dict[str, Any] | None = None,
    ) -> tuple[ContainerRequirement, ...]:
        """Return the auto-mount(s) this disc address contributes.

        Delegates to :func:`ovid2.drivers.disc.metadata.container_requirements_from`
        so the D5 collector's legacy path (which builds a driver from
        raw ``src_args``) produces byte-identical output.

        Reserved / forbidden container-path filtering is done by
        :mod:`ovid2.drivers.collector`, not here — that keeps the
        policy uniform across every peripheral type.
        """
        return container_requirements_from(dict(self.args), dict(location or {}))

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def _widened(self) -> dict[str, Any]:
        """Return the widened arg triple for probe display."""
        return normalise_disc_args(self.args)

    def probe_auth(self) -> ProbeResult:
        """Verify ``host_path`` exists and is readable."""
        normalised = self._widened()
        return probe_auth_from_host_path(normalised["host_path"], normalised)

    def probe_read(self) -> ProbeResult:
        """Verify ``host_path`` is listable."""
        normalised = self._widened()
        return probe_read_from_host_path(normalised["host_path"], normalised)

    def probe_write(self) -> ProbeResult:
        """Attempt a tiny file create + delete under ``host_path``.

        Returns a ``skipped`` result when ``mount_mode == 'ro'``.
        """
        normalised = self._widened()
        return probe_write_from_host_path(normalised["host_path"], normalised)

    # ------------------------------------------------------------------
    # Browse (control plane)
    # ------------------------------------------------------------------

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List directories + files under ``host_path`` (+ optional prefix).

        The ``host_path`` from the connection is the browse root;
        ``prefix`` descends into a sub-directory. The resolved target is
        confined to the root (``..`` escapes are rejected) so browsing
        can never leak the wider filesystem via a storage account.
        Uses :func:`os.scandir` — no heavy imports.
        """
        normalised = self._widened()
        root = os.path.realpath(str(normalised["host_path"] or ""))
        if not root or not os.path.isdir(root):
            raise NotImplementedError(f"disc host_path {root!r} is not a listable directory.")
        sub = (prefix or "").strip().lstrip("/")
        target = os.path.realpath(os.path.join(root, sub)) if sub else root
        # Confinement: target must be the root or beneath it.
        root_prefix = root.rstrip("/") + "/"
        if target != root and not target.startswith(root_prefix):
            raise ValueError(f"prefix {prefix!r} escapes the storage-account root.")
        if not os.path.isdir(target):
            raise ValueError(f"prefix {prefix!r} is not a directory.")

        entries: list[StorageEntry] = []
        with os.scandir(target) as scanner:
            for entry in scanner:
                if entry.name.startswith("."):
                    continue
                # Path returned to the caller is relative to the root so
                # it round-trips as the next ``prefix``.
                rel = os.path.relpath(os.path.join(target, entry.name), root)
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                size: int | None = None
                mtime: float | None = None
                try:
                    st = entry.stat()
                    mtime = st.st_mtime
                    if not is_dir:
                        size = st.st_size
                except OSError:
                    size = None
                entries.append(
                    StorageEntry(
                        name=entry.name,
                        kind="dir" if is_dir else "file",
                        path=rel,
                        size_bytes=size,
                        modified_at=iso_utc(mtime),
                    )
                )
        entries.sort(key=lambda e: (0 if e.kind == "dir" else 1, e.name.lower()))
        return entries

    # ------------------------------------------------------------------
    # Table introspection (control plane, pyarrow / stdlib — no Spark)
    # ------------------------------------------------------------------

    def _tabular_source(self) -> _DiscTabularSource:
        """Return the confined :class:`TabularSource` for this account."""
        normalised = self._widened()
        root = os.path.realpath(str(normalised["host_path"] or ""))
        if not root or not os.path.isdir(root):
            raise NotImplementedError(f"disc host_path {root!r} is not a listable directory.")
        return _DiscTabularSource(root)

    # ------------------------------------------------------------------
    # ``all`` expansion + size hints (control plane, stdlib only)
    # ------------------------------------------------------------------

    @classmethod
    def supports_expand_all(cls) -> bool:
        """A disc batch folder is enumerable, so ``all`` is expandable."""
        return True

    def _data_root(self, location: dict[str, Any] | None = None) -> str:
        """Return the address root visible to *this* process.

        Inside the container the ``container_path`` mount exists; on the
        host (the web tier's executor broker) only ``host_path`` does.
        The per-address ``location.path`` is appended in both cases.
        """
        loc = dict(location if location is not None else self.location)
        normalised = self._widened()
        sub = str(loc.get("path") or "")
        for base in (normalised["container_path"], normalised["host_path"]):
            if not base:
                continue
            candidate = os.path.join(base, sub) if sub else base
            if os.path.isdir(candidate):
                return candidate
        base = normalised["container_path"] or normalised["host_path"]
        return os.path.join(base, sub) if sub else base

    def _batch_dir(self, location: dict[str, Any] | None = None) -> str | None:
        """Return the exact directory this address reads from, or ``None``.

        The address *is* the location of the data: no ``{pipeline}`` /
        ``{batch_id}`` segment is appended. ``None`` when the directory
        does not exist.
        """
        root = self._data_root(location)
        return root if os.path.isdir(root) else None

    @staticmethod
    def _list_batch_dir(path: str) -> list[StorageEntry]:
        """List ``path`` one level deep with sizes (dirs = sum of their files)."""
        entries: list[StorageEntry] = []
        with os.scandir(path) as scanner:
            for entry in scanner:
                size: int | None
                try:
                    is_dir = entry.is_dir()
                    if is_dir:
                        size = 0
                        with os.scandir(entry.path) as inner:
                            for child in inner:
                                if child.is_file():
                                    size += child.stat().st_size
                    else:
                        size = entry.stat().st_size
                except OSError:
                    is_dir, size = False, None
                entries.append(
                    StorageEntry(
                        name=entry.name,
                        kind="dir" if is_dir else "file",
                        path=entry.path,
                        size_bytes=size,
                    )
                )
        entries.sort(key=lambda e: e.name.lower())
        return entries

    def expand_all_tables(
        self,
        location: dict[str, Any] | None = None,
        *,
        pipeline: str | None = None,
        batch_id: str | None = None,
    ) -> list[str] | None:
        """Expand ``all`` to every data file directly in the address folder.

        Returns ``LOGICAL=SOURCE`` specs (``demographic=demographic.csv``)
        so destination stems drop the extension. ``pipeline`` /
        ``batch_id`` are ignored — the address is the exact location.

        Raises:
            FileNotFoundError: When the address folder does not exist.
        """
        del pipeline, batch_id
        from ovid2.drivers._table_listing import entries_to_table_specs

        path = self._batch_dir(location)
        if path is None:
            raise FileNotFoundError(f"address folder {self._data_root(location)!r} does not exist")
        return entries_to_table_specs(self._list_batch_dir(path))

    def table_sizes(
        self,
        file_names: list[str],
        *,
        pipeline: str,
        batch_id: str,
    ) -> dict[str, int]:
        """Return on-disk sizes for ``file_names`` (globs allowed) via ``stat``."""
        del pipeline, batch_id
        from ovid2.drivers._table_listing import sizes_for

        path = self._batch_dir()
        if path is None:
            return {}
        return sizes_for(self._list_batch_dir(path), file_names)

    # ------------------------------------------------------------------
    # Legacy bridge
    # ------------------------------------------------------------------

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        """Return the ``(src_name, src_args)`` shape ``DiscAddress`` consumes.

        Combines the widened connection args with the per-address
        location payload — same shape
        :meth:`DiscConnection.as_address_config` used to produce.
        """
        normalised = self._widened()
        root = normalised["container_path"] or normalised["host_path"]
        sub = str(self.location.get("path") or "")
        combined = os.path.join(root, sub) if sub else root
        merged: dict[str, Any] = {"path": combined}
        if "format" in self.location:
            merged["format"] = self.location["format"]
        if "lowercase_columns" in self.location:
            merged["lowercase_columns"] = bool(self.location["lowercase_columns"])
        if "single_file" in self.location:
            merged["single_file"] = bool(self.location["single_file"])
        return "disc", merged

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
        """Read a lazy DataFrame from disc via the engine session.

        Delegates to :mod:`ovid2.drivers.disc.spark` — the only file
        in this package allowed to import pyspark.
        """
        del table_name  # unused; disc reads always use file_name
        from ovid2.drivers.disc.spark import read_dataframe

        merged = self._merged_location_view()
        del pipeline, batch_id  # the address is the exact location
        return read_dataframe(
            session,  # type: ignore[arg-type]
            merged["root"],
            file_name,
            delimiter,
            merged["format"],
            merged["lowercase_columns"],
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
        """Materialise ``df`` directly under the address directory."""
        del session, pipeline, batch_id  # the address is the exact location
        from ovid2.drivers.disc.spark import write_dataframe

        merged = self._merged_location_view()
        write_dataframe(
            df,
            merged["root"],
            table_name,
            delimiter,
            merged["format"],
            single_file=merged["single_file"],
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _merged_location_view(self) -> dict[str, Any]:
        """Return ``{root, format, lowercase_columns, single_file}`` for the data plane.

        Mirrors what
        :meth:`~ovid2.drivers.disc.legacy.DiscConnection.as_address_config`
        produced before the driver refactor: the container-visible
        root path plus the per-address format knobs. ``single_file``
        defaults to ``True`` (matches the pre-2026-08 disc contract).
        """
        normalised = self._widened()
        root_base = normalised["container_path"] or normalised["host_path"]
        sub = str(self.location.get("path") or "")
        root = os.path.join(root_base, sub) if sub else root_base
        single_file_raw = self.location.get("single_file")
        single_file = True if single_file_raw is None else bool(single_file_raw)
        return {
            "root": root,
            "format": str(self.location.get("format") or "csv"),
            "lowercase_columns": bool(self.location.get("lowercase_columns") or False),
            "single_file": single_file,
        }


# ---------------------------------------------------------------------------
# Tabular source (Data Space explorer)
# ---------------------------------------------------------------------------


class _DiscTabularSource:
    """:class:`~ovid2.drivers._tabular.TabularSource` confined to a disc root."""

    def __init__(self, root: str) -> None:
        self._root = root

    def _abs(self, path: str) -> str:
        """Resolve ``path`` under the root; reject escapes."""
        sub = (path or "").strip().lstrip("/")
        target = os.path.realpath(os.path.join(self._root, sub)) if sub else self._root
        if target != self._root and not target.startswith(self._root.rstrip("/") + "/"):
            raise ValueError(f"path {path!r} escapes the storage-account root.")
        return target

    def _file(self, abs_path: str) -> TabularFile:
        st = os.stat(abs_path)
        is_dir = os.path.isdir(abs_path)
        return TabularFile(
            path=os.path.relpath(abs_path, self._root),
            name=os.path.basename(abs_path),
            size_bytes=None if is_dir else st.st_size,
            modified_at=iso_utc(st.st_mtime),
            is_dir=is_dir,
        )

    def stat(self, path: str) -> TabularFile:
        """Return the file / directory at ``path``."""
        target = self._abs(path)
        if not os.path.exists(target):
            raise FileNotFoundError(f"'{path}' does not exist.")
        return self._file(target)

    def children(self, path: str) -> list[TabularFile]:
        """Return the direct children of ``path``."""
        target = self._abs(path)
        out: list[TabularFile] = []
        with os.scandir(target) as scanner:
            for entry in scanner:
                try:
                    out.append(self._file(entry.path))
                except OSError:
                    continue
        return out

    def open(self, file: TabularFile) -> IO[bytes]:
        """Open ``file`` for binary reading."""
        return open(self._abs(file.path), "rb")


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

# Register at module bottom so importing this module transparently
# populates the driver registry. Idempotent when the entry is already
# ours; refuses to overwrite a different path (see
# :func:`ovid2.drivers.registry.register`). We ``unregister`` first
# because the D2 :func:`~ovid2.drivers.legacy_adapter.register_all_legacy_drivers`
# runs during registry-init and may have inserted the
# ``LegacyDriverAdapter_disc`` fallback if this module's import order
# put it after the legacy sweep — no-ops otherwise.
from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("disc")
_register("disc", "ovid2.drivers.disc.driver.DiscDriver")


__all__ = ["DiscDriver"]
