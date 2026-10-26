"""Databricks SQL Warehouse connection."""

from __future__ import annotations

from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult


class DatabricksConnection(BaseConnection):
    """Databricks SQL Warehouse endpoint. Secret: personal access token."""

    type_name = "databricks"

    def test(self) -> ConnectionTestResult:
        """Probe with a ``SELECT 1`` via ``databricks-sql-connector``."""
        try:
            from databricks import sql  # type: ignore[import-not-found]
        except ImportError:
            return ConnectionTestResult(
                ok=False,
                code="skipped",
                message="databricks-sql-connector is not installed on this host.",
            )
        host = str(self.args.get("host") or "")
        http_path = str(self.args.get("http_path") or "")
        token = self.secrets.get("token")
        if not (host and http_path and token):
            return ConnectionTestResult(
                ok=False,
                code="config",
                message="host, http_path, and token are all required.",
            )
        try:
            conn = sql.connect(
                server_hostname=host.replace("https://", "").rstrip("/"),
                http_path=http_path,
                access_token=str(token),
            )
        except Exception as exc:
            return ConnectionTestResult(
                ok=False,
                code="unreachable",
                message=f"Databricks connect failed: {exc}",
            )
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchall()
            cursor.close()
        except Exception as exc:
            conn.close()
            return ConnectionTestResult(
                ok=False,
                code="auth",
                message=f"Databricks query failed: {exc}",
            )
        conn.close()
        return ConnectionTestResult(
            ok=True,
            code="ok",
            message="Databricks SELECT 1 succeeded.",
        )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Merge workspace credentials with per-address catalog + schema."""
        merged: dict[str, Any] = {}
        for k in ("host", "http_path"):
            v = self.args.get(k)
            if v is not None:
                merged[k] = v
        if self.secrets.get("token") is not None:
            merged["token"] = self.secrets["token"]
        catalog = location.get("catalog") or self.args.get("catalog")
        if catalog:
            merged["catalog"] = catalog
        if location.get("schema") is not None:
            merged["schema"] = location["schema"]
        return merged
