"""Microsoft SQL Server connection."""

from __future__ import annotations

from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult


class MssqlConnection(BaseConnection):
    """MSSQL server endpoint. Secret: password."""

    type_name = "mssql"

    def test(self) -> ConnectionTestResult:
        """Probe with a ``SELECT 1`` via ``pymssql`` or ``pyodbc``."""
        try:
            import pymssql  # type: ignore[import-not-found]
            return self._probe_pymssql(pymssql)
        except ImportError:
            pass
        try:
            import pyodbc  # type: ignore[import-not-found]
            return self._probe_pyodbc(pyodbc)
        except ImportError:
            return ConnectionTestResult(
                ok=False, code="skipped",
                message="Neither pymssql nor pyodbc is installed on this host.",
            )

    def _probe_pymssql(self, pymssql: Any) -> ConnectionTestResult:
        """Run ``SELECT 1`` through pymssql."""
        host = str(self.args.get("host") or "")
        port = int(self.args.get("port") or 1433)
        database = str(self.args.get("database") or "")
        user = str(self.args.get("user") or "")
        password = str(self.secrets.get("password") or "")
        if not (host and database and user):
            return ConnectionTestResult(
                ok=False, code="config",
                message="host, database, and user are all required.",
            )
        try:
            conn = pymssql.connect(
                server=host, port=port, user=user,
                password=password, database=database,
            )
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            return ConnectionTestResult(
                ok=False, code="unreachable",
                message=f"MSSQL connect/query failed: {exc}",
            )
        return ConnectionTestResult(
            ok=True, code="ok",
            message="MSSQL SELECT 1 succeeded (pymssql).",
        )

    def _probe_pyodbc(self, pyodbc: Any) -> ConnectionTestResult:
        """Run ``SELECT 1`` through pyodbc using the ODBC Driver 18 dsn string."""
        host = str(self.args.get("host") or "")
        port = int(self.args.get("port") or 1433)
        database = str(self.args.get("database") or "")
        user = str(self.args.get("user") or "")
        password = str(self.secrets.get("password") or "")
        encrypt = str(self.args.get("encrypt") or "yes")
        trust = str(self.args.get("trust_server_certificate") or "no")
        dsn = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={host},{port};DATABASE={database};UID={user};PWD={password};"
            f"Encrypt={encrypt};TrustServerCertificate={trust};"
        )
        try:
            conn = pyodbc.connect(dsn)
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.close()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            return ConnectionTestResult(
                ok=False, code="unreachable",
                message=f"MSSQL connect/query failed: {exc}",
            )
        return ConnectionTestResult(
            ok=True, code="ok",
            message="MSSQL SELECT 1 succeeded (pyodbc).",
        )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Merge server credentials with per-address database + schema."""
        merged: dict[str, Any] = {}
        for k in ("host", "port", "user", "encrypt", "trust_server_certificate"):
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
