"""Batch tracker for the scheduler.

Persists batch state across scheduler restarts. Two backends:

- :class:`SnowflakeBatchTracker` — production. Uses the ``OVID.SCHEDULER``
  schema, executed via the raw ``snowflake.connector`` cursor exposed by
  :class:`~ovid2.core.addresses.snowflake.SnowflakeAddress`.
- :class:`SqliteBatchTracker` — developer / non-Snowflake output. Uses a
  local ``$XDG_STATE_HOME/ovid2/scheduler.db``.

Both implement the :class:`BatchTracker` protocol so
:mod:`ovid2.scheduler.service` never needs to branch on the backend
class.

Batch lifecycle:

    Discovered on disk  ->  Registered (NEW)
    min_wait elapsed    ->  Dispatched (PROCESSING)
    Pipeline finishes   ->  PROCESSED (success) or ERROR (failure)

Re-running the scheduler against the same data directory is idempotent —
already-known batches are never re-processed.

.. note::

   ovid2: allow-sql-fstring — SQL strings in this module f-string only
   trusted class-level identifiers (``database``, ``schema``) plus
   whitelisted column names (``processing_start``, ``processing_end``).
   User-supplied values are always bound as parameters. The contract
   test :func:`tests.contract.test_style_and_paths.test_no_fstring_sql`
   honours this marker.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

STATUS_NEW = "NEW"
STATUS_PROCESSING = "PROCESSING"
STATUS_PROCESSED = "PROCESSED"
STATUS_ERROR = "ERROR"

_VALID_STATUSES: frozenset[str] = frozenset(
    {STATUS_NEW, STATUS_PROCESSING, STATUS_PROCESSED, STATUS_ERROR}
)
_VALID_TIMESTAMP_FIELDS: frozenset[str] = frozenset({"processing_start", "processing_end"})


def _validate_status(status: str) -> None:
    """Raise :class:`ValueError` when ``status`` is not a whitelisted constant."""
    if status not in _VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid values: {sorted(_VALID_STATUSES)}")


def _validate_timestamp_field(field: str) -> None:
    """Raise :class:`ValueError` when ``field`` is not a whitelisted column name."""
    if field not in _VALID_TIMESTAMP_FIELDS:
        raise ValueError(
            f"Invalid timestamp_field '{field}'. Valid values: {sorted(_VALID_TIMESTAMP_FIELDS)}"
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class BatchTracker(Protocol):
    """Anything that can persist scheduler batch state."""

    def initialize_tables(self) -> None: ...
    def get_batch_status(self, batch_id: str) -> str | None: ...
    def register_batch(self, batch_id: str, path: str) -> None: ...
    def update_status(
        self, batch_id: str, status: str, timestamp_field: str | None = None
    ) -> None: ...
    def get_ready_batches(self, min_wait_minutes: int = 10) -> list[dict[str, str]]: ...
    def reset_to_new(self, batch_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Snowflake backend
# ---------------------------------------------------------------------------


_CREATE_BATCH_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {database}.{schema}.data_batch (
    batch_id VARCHAR(200) PRIMARY KEY,
    path TEXT NOT NULL,
    received_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
    status VARCHAR(20) DEFAULT 'NEW',
    priority VARCHAR(10),
    processing_start TIMESTAMP,
    processing_end TIMESTAMP
)
"""

_CREATE_BATCH_FILE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {database}.{schema}.data_batch_file (
    id BIGINT AUTOINCREMENT PRIMARY KEY,
    batch_id VARCHAR(200) REFERENCES data_batch(batch_id) ON DELETE CASCADE,
    file_name VARCHAR(255) NOT NULL,
    file_size BIGINT,
    file_hash VARCHAR(64)
)
"""


class SnowflakeBatchTracker:
    """Track batch state in ``OVID.SCHEDULER`` (or an alternate schema).

    Uses bound parameters for every user-supplied value. Column names for
    timestamp updates are validated against a whitelist because they
    cannot be bound as parameters.

    Args:
        sf_address: :class:`~ovid2.core.addresses.snowflake.SnowflakeAddress`
            instance that exposes ``execute_ddl`` / ``execute_dml`` /
            ``execute_query`` helpers.
        database: Snowflake database name.
        schema: Snowflake schema name.
    """

    def __init__(
        self,
        sf_address: Any,
        database: str = "OVID",
        schema: str = "SCHEDULER",
    ) -> None:
        self.sf_address = sf_address
        self.database = database
        self.schema = schema

    # ----------------------------------------------------------------- init

    def initialize_tables(self) -> None:
        """Create the ``data_batch`` and ``data_batch_file`` tables."""
        self.sf_address.execute_ddl(f"CREATE SCHEMA IF NOT EXISTS {self.database}.{self.schema}")
        for ddl in (_CREATE_BATCH_TABLE_DDL, _CREATE_BATCH_FILE_TABLE_DDL):
            self.sf_address.execute_ddl(ddl.format(database=self.database, schema=self.schema))
        logger.info("Scheduler tables initialised in %s.%s", self.database, self.schema)

    # -------------------------------------------------------------- queries

    def get_batch_status(self, batch_id: str) -> str | None:
        """Return the current status of ``batch_id``, or ``None`` if unknown."""
        sql = f"SELECT status FROM {self.database}.{self.schema}.data_batch WHERE batch_id = %s"
        rows = self.sf_address.execute_query(sql, (batch_id,))
        if rows:
            return str(rows[0][0])
        return None

    def register_batch(self, batch_id: str, path: str) -> None:
        """Register a new batch with status ``NEW``."""
        sql = (
            f"INSERT INTO {self.database}.{self.schema}.data_batch "
            f"(batch_id, path, status) VALUES (%s, %s, %s)"
        )
        self.sf_address.execute_dml(sql, (batch_id, path, STATUS_NEW))
        logger.info("Registered batch '%s' with status NEW", batch_id)

    def update_status(
        self,
        batch_id: str,
        status: str,
        timestamp_field: str | None = None,
    ) -> None:
        """Update the status of a batch, optionally stamping a timestamp column."""
        _validate_status(status)
        if timestamp_field is not None:
            _validate_timestamp_field(timestamp_field)
            sql = (
                f"UPDATE {self.database}.{self.schema}.data_batch "
                f"SET status = %s, {timestamp_field} = CURRENT_TIMESTAMP() "
                f"WHERE batch_id = %s"
            )
        else:
            sql = (
                f"UPDATE {self.database}.{self.schema}.data_batch "
                f"SET status = %s WHERE batch_id = %s"
            )
        self.sf_address.execute_dml(sql, (status, batch_id))
        logger.info("Updated batch '%s' to status %s", batch_id, status)

    def get_ready_batches(self, min_wait_minutes: int = 10) -> list[dict[str, str]]:
        """Return NEW batches older than ``min_wait_minutes``, oldest first."""
        wait_minutes = int(min_wait_minutes)
        sql = (
            f"SELECT batch_id, path "
            f"FROM {self.database}.{self.schema}.data_batch "
            f"WHERE status = %s "
            f"AND TIMESTAMPDIFF(MINUTE, received_time, CURRENT_TIMESTAMP()) >= %s "
            f"ORDER BY received_time ASC"
        )
        rows = self.sf_address.execute_query(sql, (STATUS_NEW, wait_minutes))
        return [{"batch_id": str(r[0]), "path": str(r[1])} for r in rows]

    def reset_to_new(self, batch_id: str) -> None:
        """Rerun helper — flip status back to ``NEW`` and clear timestamps.

        Introduced in W3 so the web tier can rerun a failed batch by
        submitting an identical ``ovid2 etl`` invocation. The scheduler
        subsequently treats the row as a fresh drop.
        """
        sql = (
            f"UPDATE {self.database}.{self.schema}.data_batch "
            f"SET status = %s, processing_start = NULL, processing_end = NULL "
            f"WHERE batch_id = %s"
        )
        self.sf_address.execute_dml(sql, (STATUS_NEW, batch_id))
        logger.info("Reset batch '%s' to NEW (rerun)", batch_id)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


def _default_sqlite_path() -> Path:
    """Return the default SQLite file path (XDG state home)."""
    override = os.environ.get("OVID2_SCHEDULER_DB")
    if override:
        return Path(override)
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg_state) / "ovid2" / "scheduler.db"


class SqliteBatchTracker:
    """Batch tracker backed by a local SQLite database.

    Handy when the pipeline's output is not Snowflake (e.g. disc-only dev
    runs). One thread-safe :class:`sqlite3.Connection` per instance,
    guarded by a :class:`threading.Lock`.

    Args:
        path: SQLite file. Defaults to :func:`_default_sqlite_path`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_sqlite_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")

    # ----------------------------------------------------------------- init

    def initialize_tables(self) -> None:
        """Create the ``data_batch`` table if it does not exist."""
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS data_batch (
                    batch_id         TEXT PRIMARY KEY,
                    path             TEXT NOT NULL,
                    received_time    TEXT NOT NULL,
                    status           TEXT NOT NULL DEFAULT 'NEW',
                    priority         TEXT,
                    processing_start TEXT,
                    processing_end   TEXT
                )
                """
            )
        logger.info("Scheduler SQLite tables initialised at %s", self.path)

    # -------------------------------------------------------------- queries

    def get_batch_status(self, batch_id: str) -> str | None:
        """Return the current status of ``batch_id``, or ``None`` if unknown."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT status FROM data_batch WHERE batch_id = ?", (batch_id,)
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def register_batch(self, batch_id: str, path: str) -> None:
        """Register a new batch with status ``NEW``."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO data_batch "
                "(batch_id, path, received_time, status) VALUES (?, ?, ?, ?)",
                (batch_id, path, now_iso, STATUS_NEW),
            )
        logger.info("Registered batch '%s' with status NEW", batch_id)

    def update_status(
        self,
        batch_id: str,
        status: str,
        timestamp_field: str | None = None,
    ) -> None:
        """Update the status of a batch, optionally stamping a timestamp column."""
        _validate_status(status)
        if timestamp_field is not None:
            _validate_timestamp_field(timestamp_field)
            now_iso = datetime.now(timezone.utc).isoformat()
            sql = f"UPDATE data_batch SET status = ?, {timestamp_field} = ? WHERE batch_id = ?"
            params: tuple[Any, ...] = (status, now_iso, batch_id)
        else:
            sql = "UPDATE data_batch SET status = ? WHERE batch_id = ?"
            params = (status, batch_id)
        with self._lock:
            self._conn.execute(sql, params)
        logger.info("Updated batch '%s' to status %s", batch_id, status)

    def get_ready_batches(self, min_wait_minutes: int = 10) -> list[dict[str, str]]:
        """Return NEW batches older than ``min_wait_minutes``, oldest first."""
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=int(min_wait_minutes))).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "SELECT batch_id, path FROM data_batch "
                "WHERE status = ? AND received_time <= ? "
                "ORDER BY received_time ASC",
                (STATUS_NEW, cutoff),
            )
            rows = cursor.fetchall()
        return [{"batch_id": str(r[0]), "path": str(r[1])} for r in rows]

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    def reset_to_new(self, batch_id: str) -> None:
        """Rerun helper — flip status back to ``NEW`` and clear timestamps.

        Mirror of :meth:`SnowflakeBatchTracker.reset_to_new`. Introduced
        in W3 for the web-tier rerun path.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE data_batch SET status = ?, "
                "processing_start = NULL, processing_end = NULL "
                "WHERE batch_id = ?",
                (STATUS_NEW, batch_id),
            )
        logger.info("Reset batch '%s' to NEW (rerun)", batch_id)


__all__ = [
    "STATUS_ERROR",
    "STATUS_NEW",
    "STATUS_PROCESSED",
    "STATUS_PROCESSING",
    "BatchTracker",
    "SnowflakeBatchTracker",
    "SqliteBatchTracker",
]
