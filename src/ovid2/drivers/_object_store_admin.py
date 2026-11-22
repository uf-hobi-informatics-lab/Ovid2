"""Workflow-rollback storage admin for object stores (s3, azure_blob).

A folder-shaped object-store address keeps one object (or one
Spark-written ``<table>.csv/part-*`` prefix) per table directly under
its ``location.path`` prefix. Rollback works on whole tables:

- **snapshot** copies every object of a table to
  ``<prefix>/.ovid2_wf_backup/<key>/…`` (server-side copy),
- **restore** deletes the table's current objects and copies the backup
  back,
- **drop** deletes the table's objects,
- **drop_backup** deletes everything under the backup prefix.

Concrete drivers supply :meth:`_admin_store`, a tiny
:class:`AdminStore` over their SDK. The SDKs are imported lazily inside
those factories, so this module (and every driver module) stays
import-safe for the web tier.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ovid2.drivers._layout import WORKFLOW_BACKUP_DIR, safe_backup_key
from ovid2.drivers._table_listing import strip_table_extension

__all__ = ["AdminStore", "ObjectStoreAdminMixin"]


class AdminStore(ABC):
    """List / copy / delete verbs over one bucket or container."""

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """Return every object key starting with ``prefix``."""

    @abstractmethod
    def copy(self, src_key: str, dst_key: str) -> None:
        """Server-side copy ``src_key`` → ``dst_key`` (overwrites)."""

    @abstractmethod
    def delete(self, keys: list[str]) -> None:
        """Delete ``keys`` (missing keys are ignored)."""


def _join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


class ObjectStoreAdminMixin:
    """Implements the :class:`~ovid2.drivers.base.Driver` storage-admin methods."""

    location: dict[str, object]

    def _admin_store(self) -> AdminStore:  # pragma: no cover - abstract
        raise NotImplementedError

    @classmethod
    def supports_workflow_rollback(cls) -> bool:
        """Object-store prefixes can snapshot / restore / drop whole tables."""
        return True

    def _prefix(self) -> str:
        return str(self.location.get("path") or "").strip("/")

    def _backup_prefix(self, key: str) -> str:
        return _join(self._prefix(), WORKFLOW_BACKUP_DIR, safe_backup_key(key))

    @staticmethod
    def _table_keys(store: AdminStore, base: str, table: str) -> list[str]:
        """Return the keys directly under ``base`` that belong to ``table``."""
        want = strip_table_extension(table).lower()
        lead = (base + "/") if base else ""
        out: list[str] = []
        for key in store.list_keys(lead):
            rest = key[len(lead) :]
            head = rest.split("/", 1)[0]
            if not head or head.startswith("."):
                continue
            if strip_table_extension(head).lower() == want:
                out.append(key)
        return sorted(out)

    def snapshot_tables(self, tables: list[str], key: str) -> list[str]:
        """Copy every object of ``tables`` under the backup prefix."""
        store = self._admin_store()
        base = self._prefix()
        backup = self._backup_prefix(key)
        old = store.list_keys(backup + "/")
        if old:
            store.delete(old)
        copied: list[str] = []
        for table in tables:
            keys = self._table_keys(store, base, table)
            for k in keys:
                rel = k[len(base) + 1 :] if base else k
                store.copy(k, _join(backup, rel))
            if keys:
                copied.append(table)
        return copied

    def restore_tables(self, tables: list[str], key: str) -> list[str]:
        """Replace ``tables`` with their backup copies."""
        store = self._admin_store()
        base = self._prefix()
        backup = self._backup_prefix(key)
        restored: list[str] = []
        for table in tables:
            saved = self._table_keys(store, backup, table)
            if not saved:
                continue
            current = self._table_keys(store, base, table)
            if current:
                store.delete(current)
            for k in saved:
                store.copy(k, _join(base, k[len(backup) + 1 :]))
            restored.append(table)
        return restored

    def drop_tables(self, tables: list[str]) -> list[str]:
        """Delete every object of ``tables``."""
        store = self._admin_store()
        base = self._prefix()
        dropped: list[str] = []
        for table in tables:
            keys = self._table_keys(store, base, table)
            if keys:
                store.delete(keys)
                dropped.append(table)
        return dropped

    def drop_backup(self, key: str) -> None:
        """Delete everything under the backup prefix."""
        store = self._admin_store()
        keys = store.list_keys(self._backup_prefix(key) + "/")
        if keys:
            store.delete(keys)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module; import it.")
