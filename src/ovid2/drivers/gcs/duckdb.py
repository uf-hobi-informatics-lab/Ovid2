"""DuckDB data plane for the gcs driver (google-cloud-storage + local DuckDB).

Authenticates with the connection's ``service_account_json`` secret, or
Application Default Credentials when it is absent (same as the Snowpark
GCS strategy). ``$OVID2_GCS_ENDPOINT`` points the client at an emulator.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ovid2.core.engines.duckdb_stager import register_duckdb_strategy
from ovid2.drivers._object_store_duckdb import ObjectStoreClient, ObjectStoreStrategy


class GcsClient(ObjectStoreClient):
    """google-cloud-storage-backed :class:`ObjectStoreClient`."""

    def __init__(self, config: dict[str, Any]) -> None:
        from google.cloud import storage

        bucket = config.get("bucket")
        if not bucket:
            raise ValueError("gcs address has no bucket (location.bucket / bucket_default)")
        project = config.get("project_id")
        endpoint = os.environ.get("OVID2_GCS_ENDPOINT")
        raw = config.get("service_account_json")
        if endpoint:
            from google.auth.credentials import AnonymousCredentials

            client = storage.Client(
                project=project or "ovid2",
                credentials=AnonymousCredentials(),  # type: ignore[no-untyped-call,unused-ignore]
                client_options={"api_endpoint": endpoint},
            )
        elif raw:
            from google.oauth2 import service_account

            info = raw if isinstance(raw, dict) else json.loads(str(raw))
            client = storage.Client(
                project=project,
                credentials=service_account.Credentials.from_service_account_info(info),  # type: ignore[no-untyped-call,unused-ignore]
            )
        else:
            client = storage.Client(project=project)
        self._bucket = client.bucket(str(bucket))
        self._client = client

    def list_keys(self, prefix: str) -> list[str]:
        return [b.name for b in self._client.list_blobs(self._bucket, prefix=prefix or None)]

    def download(self, key: str, dest: str) -> None:
        self._bucket.blob(key).download_to_filename(dest)

    def upload(self, src: str, key: str) -> None:
        self._bucket.blob(key).upload_from_filename(src)

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            blob = self._bucket.blob(key)
            if blob.exists():
                blob.delete()


@register_duckdb_strategy
class DuckDBGcsStrategy(ObjectStoreStrategy):
    """gcs <-> DuckDB."""

    driver_type = "gcs"

    def client(self, config: dict[str, Any]) -> ObjectStoreClient:
        return GcsClient(config)
