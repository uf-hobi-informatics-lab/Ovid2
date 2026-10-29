"""Databricks Spark compute engine.

Uses Databricks Connect (``pyspark.sql.SparkSession.builder.remote(...)``)
to point the pipeline at a remote Databricks cluster. The returned
session is API-compatible with a real :class:`~pyspark.sql.SparkSession`
so no downstream pipeline code needs to change.

**Configuration** (``$XDG_CONFIG_HOME/ovid2/engines/<name>.json``):

* ``args.workspace_url`` — e.g. ``https://<workspace>.cloud.databricks.com``
* ``args.cluster_id``   — Databricks cluster id (optional; some
  workspaces use SQL warehouses instead).
* ``args.serverless``   — ``"true"`` to route through serverless
  compute.
* ``secrets.token``     — Databricks personal access token
  (``$XDG_CONFIG_HOME/ovid2/secrets.d/engine_<name>.json``, mode
  ``0o600``).

**Family** — ``spark``. All Spark-family fixers work; the pipeline
never needs to gate on capabilities.
"""

from __future__ import annotations

import contextlib
from datetime import UTC
from typing import TYPE_CHECKING, Any

from ovid2.core.engines.base import BaseEngine, EngineTestResult
from ovid2.core.engines.protocols import EngineFamily, EngineFunctions, EngineSession
from ovid2.core.engines.pyspark import _SparkFunctions, read_status_tracker

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

    from ovid2.core.engines.metrics import EngineMetricsSample


class DatabricksSparkEngine(BaseEngine):
    """Databricks-Connect-backed engine.

    Instantiates ``SparkSession.builder.remote(...)`` with a
    Databricks-issued connection string. The resulting session speaks
    the same DataFrame API as a local ``SparkSession``, so we reuse the
    :class:`~ovid2.core.engines.pyspark._SparkFunctions` adapter and
    every downstream fixer / adapter continues to work.
    """

    type_name = "databricks_spark"
    family = EngineFamily.SPARK
    session_is_spark = True

    def __init__(
        self,
        args: dict[str, Any] | None = None,
        secrets: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(args=args, secrets=secrets)
        self._session: SparkSession | None = None
        self._functions: EngineFunctions | None = None

    def _connection_string(self) -> str:
        """Build the ``sc://…`` Databricks-Connect connection string."""
        workspace_url = str(self.args.get("workspace_url") or "").rstrip("/")
        if not workspace_url:
            raise ValueError("databricks_spark: 'workspace_url' is required")
        token = self.secrets.get("token")
        if not token:
            raise ValueError("databricks_spark: secret 'token' is required")
        # Strip protocol
        host = workspace_url.split("://", 1)[-1]
        parts = [f"sc://{host}:443/", f";token={token}"]
        cluster_id = self.args.get("cluster_id")
        if cluster_id:
            parts.append(f";x-databricks-cluster-id={cluster_id}")
        return "".join(parts)

    def get_session(self) -> EngineSession:
        """Return the Databricks-Connect Spark session."""
        if self._session is None:
            from pyspark.sql import SparkSession

            conn = self._connection_string()
            self._session = SparkSession.builder.remote(conn).getOrCreate()
            # Databricks Connect does not always expose sparkContext.
            with contextlib.suppress(Exception):
                self._session.sparkContext.setLogLevel("OFF")
        return self._session  # type: ignore[return-value]

    def stop_session(self) -> None:
        """Stop the remote Spark session if one has been created."""
        if self._session is not None:
            with contextlib.suppress(Exception):
                self._session.stop()
            self._session = None
            self._functions = None

    def get_functions(self) -> EngineFunctions:
        """Return the cached :class:`EngineFunctions` adapter."""
        if self._functions is None:
            self._functions = _SparkFunctions()
        return self._functions

    def native(self) -> SparkSession:
        """Return the underlying Databricks-Connect ``SparkSession``."""
        return self.get_session()  # type: ignore[return-value]

    def test(self) -> EngineTestResult:
        """Build a session and run ``SELECT 1``."""
        try:
            from pyspark.sql import SparkSession  # noqa: F401
        except ImportError as exc:
            return EngineTestResult(
                ok=False,
                code="unreachable",
                message=f"pyspark not importable: {exc}",
            )
        try:
            session = self.get_session()
            row = session.sql("SELECT 1 AS ok").collect()  # noqa: OVID2001
            if row and row[0][0] == 1:
                return EngineTestResult(
                    ok=True,
                    code="ok",
                    message="Databricks-Connect session established",
                    detail={"workspace_url": str(self.args.get("workspace_url") or "")},
                )
            return EngineTestResult(
                ok=False,
                code="error",
                message="SELECT 1 returned unexpected result",
                detail={"row": str(row)},
            )
        except ValueError as exc:
            return EngineTestResult(ok=False, code="auth", message=str(exc))
        except Exception as exc:
            return EngineTestResult(
                ok=False,
                code="unreachable",
                message=f"Databricks probe failed: {exc}",
                detail={"exception_type": type(exc).__name__},
            )

    def get_metrics(self) -> EngineMetricsSample:
        """Return a live snapshot, tolerating Databricks-Connect quirks.

        Databricks-Connect does not always expose ``sparkContext``
        (thin-client mode, serverless SQL warehouses). When it's
        missing we return a bare "engine alive" sample so the pulse
        UI still ticks, but no per-task numbers.
        """
        import time
        from datetime import datetime

        from ovid2.core.engines.metrics import EngineMetricsSample

        now_ms = int(time.monotonic() * 1000)
        wall = datetime.now(UTC).isoformat()

        if self._session is None:
            return EngineMetricsSample(t_ms=now_ms, wall_iso=wall, engine_family=self.family.value)

        # Try the same tracker path as PySparkEngine — Databricks Connect
        # sometimes exposes a compatible ``sparkContext``. When it
        # doesn't, this raises AttributeError inside the try/except
        # below and we return a bare sample.
        try:
            snap = read_status_tracker(self._session.sparkContext)
            return EngineMetricsSample(
                t_ms=now_ms,
                wall_iso=wall,
                engine_family=self.family.value,
                executors_active=snap.executors_active,
                executors_total=snap.executors_total,
                tasks_active=snap.tasks_active,
                tasks_completed_total=snap.tasks_completed,
                tasks_failed_total=snap.tasks_failed,
                stages_total=len(snap.active_stage_ids),
                current_stage=snap.current_stage,
            )
        except Exception:
            return EngineMetricsSample(t_ms=now_ms, wall_iso=wall, engine_family=self.family.value)


__all__ = ["DatabricksSparkEngine"]
