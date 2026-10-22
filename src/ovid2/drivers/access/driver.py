"""Native ``AccessDriver`` — Microsoft Access (.accdb / .mdb) source.

Reads a Microsoft Access database file as a set of tables: each user
table inside the file is surfaced (via :meth:`AccessDriver.list_dir`)
and read (via :meth:`AccessDriver.read`) as a *separate* table. Backed
by the UCanAccess JDBC driver baked into the ETL image.

File layout follows the D3 driver recipe:

- :mod:`ovid2.drivers.access.metadata` — import-safe field specs +
  container-requirement derivation (no pyspark / JDBC).
- :mod:`ovid2.drivers.access.probe` — filesystem probes (no JVM).
- :mod:`ovid2.drivers.access.spark` — the *only* file allowed to
  import pyspark / touch the JDBC layer.

The Access source is read-only: :meth:`write` and :meth:`probe_write`
are intentionally left unimplemented (they raise
:class:`NotImplementedError`, which the harness treats as "capability
absent").
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ovid2.drivers.access.metadata import (
    ACCESS_FILE_SUFFIXES,
    container_requirements_from,
    normalise_access_args,
    parse_table_list,
)
from ovid2.drivers.access.metadata import (
    connection_fields as _connection_fields,
)
from ovid2.drivers.access.metadata import (
    location_fields as _location_fields,
)
from ovid2.drivers.access.probe import (
    probe_auth_from_host_path,
    probe_read_from_file,
)
from ovid2.drivers.base import (
    ConnectionFieldSpec,
    Driver,
    LocationField,
    ProbeResult,
    StorageEntry,
)
from ovid2.drivers.container import ContainerRequirement

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineSession


class AccessDriver(Driver):
    """Native driver for the ``access`` peripheral (Microsoft Access files).

    Constructor keeps the uniform ``(args, secrets, location)`` shape.
    ``secrets`` is always empty. :attr:`args` carries the connection
    body (``host_path`` / ``container_path`` / ``mount_mode``);
    :attr:`location` carries the per-address payload (``path`` to the
    ``.accdb`` file, optional ``tables`` / ``exclude_tables`` filter,
    ``lowercase_columns``).
    """

    type_name: str = "access"

    # ------------------------------------------------------------------
    # Metadata (import-safe)
    # ------------------------------------------------------------------

    @classmethod
    def connection_fields(cls) -> ConnectionFieldSpec:
        """Return the Access connection form field spec."""
        return _connection_fields()

    @classmethod
    def location_fields(cls) -> tuple[LocationField, ...]:
        """Return the Access location form field spec."""
        return _location_fields()

    def container_requirements_for(
        self, location: dict[str, Any] | None = None,
    ) -> tuple[ContainerRequirement, ...]:
        """Return the read-only bind-mount this Access address contributes.

        Delegates to
        :func:`ovid2.drivers.access.metadata.container_requirements_from`
        so the collector's legacy path (which builds a driver from raw
        ``src_args``) produces identical output.
        """
        return container_requirements_from(
            dict(self.args), dict(location or self.location or {}),
        )

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def _widened(self) -> dict[str, Any]:
        """Return the widened arg triple for probe display."""
        return normalise_access_args(self.args)

    def probe_auth(self) -> ProbeResult:
        """Verify the connection directory exists and is readable."""
        normalised = self._widened()
        return probe_auth_from_host_path(normalised["host_path"], normalised)

    def probe_read(self) -> ProbeResult:
        """Verify the resolved Access file (or the directory) is readable.

        Uses the host-side path so the probe can run in the web tier
        without a mounted container.
        """
        normalised = self._widened()
        return probe_read_from_file(
            self._host_file_path(), normalised,
        )

    # ------------------------------------------------------------------
    # Browse (control plane) — surfaces each table as a separate entry
    # ------------------------------------------------------------------

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List Access files (dirs) or the tables inside a file.

        Two levels, mirroring how Snowflake surfaces database → table:

        - Empty ``prefix`` (or a directory prefix) → list ``.accdb`` /
          ``.mdb`` files as ``dir`` entries plus any sub-directories.
        - A prefix that resolves to an Access file → list the user
          tables inside it as ``kind="table"`` entries.

        The browse root is the connection ``host_path``; ``..`` escapes
        are rejected so browsing cannot leak the wider filesystem.
        """
        normalised = self._widened()
        root = os.path.realpath(str(normalised["host_path"] or ""))
        if not root or not os.path.isdir(root):
            raise NotImplementedError(
                f"access host_path {root!r} is not a listable directory."
            )
        sub = (prefix or "").strip().lstrip("/")
        target = os.path.realpath(os.path.join(root, sub)) if sub else root
        root_prefix = root.rstrip("/") + "/"
        if target != root and not target.startswith(root_prefix):
            raise ValueError(
                f"prefix {prefix!r} escapes the storage-account root."
            )

        if os.path.isdir(target):
            return self._list_directory(root, target)
        if os.path.isfile(target) and target.lower().endswith(
            ACCESS_FILE_SUFFIXES
        ):
            return self._list_tables_in_file(root, target)
        raise ValueError(
            f"prefix {prefix!r} is neither a directory nor an Access file."
        )

    def _list_directory(self, root: str, target: str) -> list[StorageEntry]:
        """List sub-directories + Access files under ``target``."""
        entries: list[StorageEntry] = []
        with os.scandir(target) as scanner:
            for entry in scanner:
                if entry.name.startswith("."):
                    continue
                rel = os.path.relpath(
                    os.path.join(target, entry.name), root
                )
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    entries.append(
                        StorageEntry(name=entry.name, kind="dir", path=rel)
                    )
                elif entry.name.lower().endswith(ACCESS_FILE_SUFFIXES):
                    # An Access file is a *navigable* container of tables,
                    # so surface it as a ``dir`` the operator can descend.
                    entries.append(
                        StorageEntry(name=entry.name, kind="dir", path=rel)
                    )
        entries.sort(key=lambda e: e.name.lower())
        return entries

    def _list_tables_in_file(
        self, root: str, file_path: str,
    ) -> list[StorageEntry]:
        """List the user tables inside an Access file as ``table`` entries.

        Two enumeration paths, tried in order:

        1. **Spark-free** (:func:`ovid2.drivers.access.reader.list_tables_no_spark`)
           — opens the file over UCanAccess JDBC via jaydebeapi/jpype.
           This is the web-tier path (no Spark session required).
        2. **Spark** (:func:`ovid2.drivers.access.spark.list_tables`) —
           used when an active Spark session exists (container / ETL).

        When neither path is available (no jaydebeapi + no Spark
        session) a :class:`NotImplementedError` propagates so the
        caller stops at the file level rather than erroring hard.
        """
        rel_file = os.path.relpath(file_path, root)
        table_names = self._enumerate_tables(file_path)
        return [
            StorageEntry(
                name=table,
                kind="table",
                path=f"{rel_file}::{table}",
            )
            for table in table_names
        ]

    def _enumerate_tables(
        self, file_path: str, spark_file_path: str | None = None,
    ) -> list[str]:
        """Return the user tables in ``file_path``, Spark-free first.

        Prefers the Spark-free reader (works in the web tier, keyed on
        the *host* path). Falls back to the Spark/JDBC path when an
        active Spark session is present — keyed on ``spark_file_path``
        (the *container*-visible path) so the fallback works even when
        the host and container mounts differ. Re-raises
        :class:`NotImplementedError` when neither path can enumerate so
        the browser degrades to file-level.
        """
        from ovid2.drivers.access.reader import list_tables_no_spark

        try:
            return list_tables_no_spark(file_path)
        except NotImplementedError:
            # No jaydebeapi / no jars — try an active Spark session.
            from ovid2.drivers.access.spark import (
                get_active_session,
                list_tables,
            )

            session = get_active_session()
            return list_tables(session, spark_file_path or file_path)


    # ------------------------------------------------------------------
    # Table discovery (data plane helper)
    # ------------------------------------------------------------------

    def tables_to_read(self, session: EngineSession) -> list[str]:
        """Return the user tables this address should materialise.

        Enumerates every user table in the file, then applies the
        optional ``tables`` allow-list and ``exclude_tables`` deny-list
        from the location. When no ``tables`` filter is set, *all* user
        tables are returned — the default "one output per table"
        behaviour.
        """
        from ovid2.drivers.access.spark import list_tables

        all_tables = list_tables(session, self._container_file_path())  # type: ignore[arg-type]
        return self._apply_table_filters(all_tables, self.location)

    def _apply_table_filters(
        self, all_tables: list[str], location: dict[str, Any],
    ) -> list[str]:
        """Apply the ``tables`` allow-list / ``exclude_tables`` deny-list.

        Shared by :meth:`tables_to_read` (data plane, Spark) and
        :meth:`expand_all_tables` (control plane, Spark-free) so both
        honour the same per-address filters.
        """
        allow = {t.lower() for t in parse_table_list(location.get("tables"))}
        deny = {
            t.lower()
            for t in parse_table_list(location.get("exclude_tables"))
        }
        return [
            t
            for t in all_tables
            if (not allow or t.lower() in allow) and t.lower() not in deny
        ]

    @classmethod
    def supports_expand_all(cls) -> bool:
        """Access is a self-describing container, so ``all`` is expandable."""
        return True

    def expand_all_tables(
        self, location: dict[str, Any] | None = None,
    ) -> list[str] | None:
        """Expand an ``all`` copy/move request to the file's user tables.

        An Access ``.accdb`` / ``.mdb`` file is a self-describing
        container of tables, so ``copy`` / ``move`` can safely accept
        ``all`` for an Access source: it means "every user table in the
        file, after the location's ``exclude_tables`` deny-list". Uses
        the Spark-free enumerator so the web tier can validate the
        request before spinning up a cluster.

        Returns:
            The concrete list of table names, or ``None`` when
            enumeration is unavailable (no jaydebeapi + no jars) so the
            caller keeps rejecting ``all`` rather than silently copying
            nothing.
        """
        loc = dict(location or self.location or {})
        # The web tier (Spark-free reader) reads the *host* path; the
        # container Spark fallback reads the *container*-visible path.
        host_path = self._host_file_path_for(loc)
        container_path = self._container_file_path_for(loc)
        try:
            all_tables = self._enumerate_tables(
                host_path, spark_file_path=container_path,
            )
        except NotImplementedError:
            return None
        return self._apply_table_filters(all_tables, loc)

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
        """Read one Access table as a lazy DataFrame.

        Unlike disc (which keys on ``file_name``), Access keys on
        ``table_name`` — the ``.accdb`` file is one container and each
        table is a separate output. ``table_name`` takes precedence;
        ``file_name`` is accepted for interface uniformity and used as
        a fall-back table name when ``table_name`` is not supplied.
        """
        del pipeline, batch_id, delimiter  # location selects the file
        from ovid2.drivers.access.spark import read_table

        target_table = table_name or file_name
        if not target_table:
            raise ValueError(
                "AccessDriver.read requires a table_name (or file_name "
                "used as the table name)."
            )
        return read_table(
            session,  # type: ignore[arg-type]
            self._container_file_path(),
            target_table,
            bool(self.location.get("lowercase_columns") or False),
        )

    # ------------------------------------------------------------------
    # Legacy bridge
    # ------------------------------------------------------------------

    def as_legacy_src_args(self) -> tuple[str, dict[str, Any]]:
        """Return the ``(src_name, src_args)`` shape ``AccessAddress`` consumes."""
        merged: dict[str, Any] = {"path": self._container_file_path()}
        if "tables" in self.location:
            merged["tables"] = self.location["tables"]
        if "exclude_tables" in self.location:
            merged["exclude_tables"] = self.location["exclude_tables"]
        if "lowercase_columns" in self.location:
            merged["lowercase_columns"] = bool(
                self.location["lowercase_columns"]
            )
        return "access", merged

    # ------------------------------------------------------------------
    # Internal path resolution
    # ------------------------------------------------------------------

    def _host_file_path(self) -> str:
        """Return the host-side path to the Access file (for probes)."""
        return self._host_file_path_for(self.location)

    def _host_file_path_for(self, location: dict[str, Any]) -> str:
        """Return the host-side Access file path for a given ``location``."""
        normalised = self._widened()
        base = normalised["host_path"]
        sub = str(location.get("path") or "")
        return os.path.join(base, sub) if sub else base

    def _container_file_path(self) -> str:
        """Return the container-visible path to the Access file (for reads)."""
        return self._container_file_path_for(self.location)

    def _container_file_path_for(self, location: dict[str, Any]) -> str:
        """Return the container-visible Access file path for a ``location``."""
        normalised = self._widened()
        base = normalised["container_path"] or normalised["host_path"]
        sub = str(location.get("path") or "")
        return os.path.join(base, sub) if sub else base


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

# Register at module bottom so importing this module transparently
# populates the driver registry. ``unregister`` first in case the
# legacy sweep inserted a fallback adapter before this module imported.
from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("access")
_register("access", "ovid2.drivers.access.driver.AccessDriver")


__all__ = ["AccessDriver"]
