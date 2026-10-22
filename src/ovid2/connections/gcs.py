"""Google Cloud Storage connection."""

from __future__ import annotations

import json
from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult


class GcsConnection(BaseConnection):
    """GCS endpoint. Secret: service-account JSON."""

    type_name = "gcs"

    def test(self) -> ConnectionTestResult:
        """Probe by listing buckets via ``google-cloud-storage``."""
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
            from google.oauth2 import service_account  # type: ignore[import-not-found]
        except ImportError:
            return ConnectionTestResult(
                ok=False, code="skipped",
                message="google-cloud-storage is not installed on this host.",
            )
        sa_json = self.secrets.get("service_account_json")
        if not sa_json:
            return ConnectionTestResult(
                ok=False, code="config",
                message="service_account_json secret is required.",
            )
        try:
            info = json.loads(sa_json) if isinstance(sa_json, str) else dict(sa_json)
            creds = service_account.Credentials.from_service_account_info(info)
            client = storage.Client(
                project=str(self.args.get("project_id") or ""),
                credentials=creds,
            )
            bucket = self.args.get("bucket_default")
            if bucket:
                client.get_bucket(bucket)
                return ConnectionTestResult(
                    ok=True, code="ok",
                    message=f"get_bucket({bucket}) succeeded.",
                )
            list(client.list_buckets(page_size=1))
            return ConnectionTestResult(
                ok=True, code="ok",
                message="list_buckets(page_size=1) succeeded.",
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionTestResult(
                ok=False, code="unreachable",
                message=f"GCS probe failed: {exc}",
            )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Merge connection credentials with per-address bucket + path."""
        merged: dict[str, Any] = {}
        if self.args.get("project_id") is not None:
            merged["project_id"] = self.args["project_id"]
        if self.secrets.get("service_account_json") is not None:
            merged["service_account_json"] = self.secrets["service_account_json"]
        bucket = location.get("bucket") or self.args.get("bucket_default")
        if bucket:
            merged["bucket"] = bucket
        if location.get("path") is not None:
            merged["prefix"] = str(location["path"])
        if location.get("format") is not None:
            merged["format"] = str(location["format"])
        if location.get("single_file") is not None:
            merged["single_file"] = bool(location["single_file"])
        return merged
