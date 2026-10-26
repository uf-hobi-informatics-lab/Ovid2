"""PostgreSQL connection."""

from __future__ import annotations

import importlib
from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult


class PostgresConnection(BaseConnection):
    """PostgreSQL server endpoint. Secret: password."""

    type_name = "postgres"

    def test(self) -> ConnectionTestResult:
        """Probe with a ``SELECT 1`` via ``psycopg2`` or ``psycopg``."""
        connector: Any
        try:
            connector = importlib.import_module("psycopg2")
        except ImportError:
            try:
                connector = importlib.import_module("psycopg")
            except ImportError:
                return ConnectionTestResult(
                    ok=False,
                    code="skipped",
                    message="Neither psycopg2 nor psycopg is installed on this host.",
                )
        host = str(self.args.get("host") or "")
        port = int(self.args.get("port") or 5432)
        database = str(self.args.get("database") or "")
        user = str(self.args.get("user") or "")
        password = self.secrets.get("password")
        if not (host and database and user):
            return ConnectionTestResult(
                ok=False,
                code="config",
                message="host, database, and user are all required.",
            )
        kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "dbname": database,
            "user": user,
        }
        if password is not None:
            kwargs["password"] = password
        sslmode = self.args.get("sslmode")
        if sslmode:
            kwargs["sslmode"] = sslmode
        try:
            conn = connector.connect(**kwargs)
        except Exception as exc:
            return ConnectionTestResult(
                ok=False,
                code="unreachable",
                message=f"Postgres connect failed: {exc}",
            )
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
        except Exception as exc:
            conn.close()
            return ConnectionTestResult(
                ok=False,
                code="auth",
                message=f"Postgres query failed: {exc}",
            )
        conn.close()
        return ConnectionTestResult(
            ok=True,
            code="ok",
            message="Postgres SELECT 1 succeeded.",
        )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Merge server credentials with per-address database + schema."""
        merged: dict[str, Any] = {}
        for k in ("host", "port", "user", "sslmode"):
            v = self.args.get(k)
            if v is not None:
                merged[k] = v
        if self.secrets.get("password") is not None:
            merged["password"] = self.secrets["password"]
        database = location.get("database") or self.args.get("database")
        if database is not None:
            merged["database"] = database
        if location.get("schema") is not None:
            merged["schema"] = location["schema"]
        return merged
