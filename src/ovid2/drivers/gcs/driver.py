"""Native ``GcsDriver`` — Data Space browsing + table introspection.

Subclasses the legacy adapter for ``gcs`` (probe + field specs
unchanged) and adds folder-style browsing of a bucket (``list_blobs``
with a ``/`` delimiter) plus the shared Spark-free table introspection
(:mod:`ovid2.drivers._tabular`) over ranged blob downloads.
``google-cloud-storage`` is imported lazily; install with
``pip install 'ovid2[gcs]'``.
"""

from __future__ import annotations

import json
from typing import Any

from ovid2.drivers._tabular import ObjectStoreSource, TabularIntrospectionMixin, iso_utc
from ovid2.drivers.base import StorageEntry
from ovid2.drivers.legacy_adapter import build_legacy_driver_class

__all__ = ["GcsDriver"]


class GcsDriver(TabularIntrospectionMixin, build_legacy_driver_class("gcs")):  # type: ignore[misc]
    """``gcs`` driver with Data Space browsing + introspection."""

    def _client_bucket(self) -> tuple[Any, Any, str]:
        try:
            from google.cloud import storage
            from google.oauth2 import service_account
        except ImportError as exc:
            raise NotImplementedError(
                "google-cloud-storage is not installed (pip install 'ovid2[gcs]')."
            ) from exc
        bucket_name = str(self.location.get("bucket") or self.args.get("bucket_default") or "")
        if not bucket_name:
            raise NotImplementedError(
                "No bucket set on the storage account (location.bucket or bucket_default)."
            )
        sa = self.secrets.get("service_account_json")
        if not sa:
            raise ValueError("The GCS storage account has no service_account_json secret.")
        info = json.loads(sa) if isinstance(sa, str) else dict(sa)
        creds = service_account.Credentials.from_service_account_info(info)
        client = storage.Client(project=str(self.args.get("project_id") or ""), credentials=creds)
        return client, client.bucket(bucket_name), bucket_name

    def _listing(
        self, client: Any, bucket_name: str, prefix: str
    ) -> tuple[list[str], list[tuple[str, int, str | None]]]:
        iterator = client.list_blobs(bucket_name, prefix=prefix or None, delimiter="/")
        files: list[tuple[str, int, str | None]] = []
        for blob in iterator:
            name = str(blob.name)[len(prefix) :]
            if name and "/" not in name:
                files.append(
                    (name, int(blob.size or 0), iso_utc(blob.updated) if blob.updated else None)
                )
        dirs = sorted({str(p)[len(prefix) :].rstrip("/") for p in iterator.prefixes} - {""})
        return dirs, files

    def list_dir(self, prefix: str | None = None) -> list[StorageEntry]:
        """List prefixes (``dir``) and blobs (``file``) one level under ``prefix``."""
        client, _bucket, bucket_name = self._client_bucket()
        base = str(self.location.get("path") or "").strip("/")
        sub = (prefix or "").strip("/")
        effective = "/".join(p for p in (base, sub) if p)
        effective = effective + "/" if effective else ""
        dirs, files = self._listing(client, bucket_name, effective)
        rel = (sub + "/") if sub else ""
        entries = [StorageEntry(name=d, kind="dir", path=rel + d) for d in dirs]
        entries.extend(
            StorageEntry(name=n, kind="file", path=rel + n, size_bytes=s, modified_at=m)
            for n, s, m in sorted(files, key=lambda f: f[0].lower())
        )
        return entries

    def _tabular_source(self) -> ObjectStoreSource:
        client, bucket, bucket_name = self._client_bucket()

        def head(key: str) -> tuple[int, str | None] | None:
            blob = bucket.get_blob(key)
            if blob is None:
                return None
            return int(blob.size or 0), iso_utc(blob.updated) if blob.updated else None

        def fetch(key: str, start: int, end: int) -> bytes:
            if end <= start:
                return b""
            return bytes(bucket.blob(key).download_as_bytes(start=start, end=end - 1))

        return ObjectStoreSource(
            head=head,
            listing=lambda prefix: self._listing(client, bucket_name, prefix),
            fetch=fetch,
            base=str(self.location.get("path") or ""),
        )


GcsDriver.__name__ = "GcsDriver"
GcsDriver.__qualname__ = "GcsDriver"

from ovid2.drivers.registry import register as _register  # noqa: E402
from ovid2.drivers.registry import unregister as _unregister  # noqa: E402

_unregister("gcs")
_register("gcs", "ovid2.drivers.gcs.driver.GcsDriver")
