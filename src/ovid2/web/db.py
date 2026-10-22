"""Web-tier persistence.

Adds two tables to the SQLite file used by the scheduler
(``$XDG_STATE_HOME/ovid2/scheduler.db``):

- ``web_jobs`` — job queue driven by ``ovid2 web-worker`` (populated in W3).
- ``web_audit`` — append-only audit log of every mutation.

The additions are strictly additive; the ``data_batch`` table owned by
:mod:`ovid2.scheduler.batch_tracker` is left untouched. The web tier
never writes to ``data_batch`` directly — that remains the scheduler's
responsibility.

Concurrency:

- The scheduler uses one connection per instance guarded by a lock.
- The web app opens short-lived connections per request. WAL journal
  mode makes readers and writers coexist without blocking.

Callers should:

.. code-block:: python

   from ovid2.web import db
   with db.connect() as conn:
       rows = conn.execute("SELECT ...").fetchall()
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from ovid2.web.settings import web_db_path

logger = logging.getLogger(__name__)

_WEB_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS web_jobs (
    job_id                TEXT PRIMARY KEY,
    submitted_at          TEXT NOT NULL,
    submitted_by          TEXT NOT NULL DEFAULT 'web',
    parent_job_id         TEXT,
    pipeline               TEXT NOT NULL,
    batch_id              TEXT NOT NULL,
    input_address         TEXT NOT NULL,
    output_address        TEXT NOT NULL,
    tables_json           TEXT NOT NULL,
    transformations_json  TEXT NOT NULL,
    extra_args_json       TEXT,
    status                TEXT NOT NULL DEFAULT 'queued',
    cancel_requested      INTEGER NOT NULL DEFAULT 0,
    instance_id           TEXT,
    pid                   INTEGER,
    started_at            TEXT,
    ended_at              TEXT,
    error                 TEXT
)
"""

_WEB_JOBS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_jobs_status ON web_jobs(status, submitted_at)",
    "CREATE INDEX IF NOT EXISTS ix_web_jobs_instance ON web_jobs(instance_id)",
    "CREATE INDEX IF NOT EXISTS ix_web_jobs_pipeline_batch ON web_jobs(pipeline, batch_id)",
]

_WEB_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS web_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    target        TEXT,
    before_hash   TEXT,
    after_hash    TEXT,
    detail_json   TEXT
)
"""

_WEB_AUDIT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_audit_ts ON web_audit(ts DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_audit_action ON web_audit(action, ts DESC)",
]

# Approval gate for destructive operations (harness / API / executor
# trust model, Phase 3). A destructive capability request enters
# ``pending`` and is refused execution until a human approves it. The
# agent never self-approves — approval is a separate, human-driven POST.
_WEB_APPROVALS_DDL = """
CREATE TABLE IF NOT EXISTS web_approvals (
    approval_id   TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    action        TEXT NOT NULL,
    target        TEXT,
    detail_json   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    decided_at    TEXT,
    decided_by    TEXT,
    reason        TEXT
)
"""

_WEB_APPROVALS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_approvals_status ON web_approvals(status, created_at DESC)",
]


_WEB_WATCH_CURSORS_DDL = """
CREATE TABLE IF NOT EXISTS web_watch_cursors (
    pipeline         TEXT NOT NULL,
    input_address    TEXT NOT NULL,
    last_seen_batch  TEXT,
    last_checked_at  TEXT,
    PRIMARY KEY (pipeline, input_address)
)
"""

_WEB_WATCH_CURSORS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_watch_cursors_pipeline ON web_watch_cursors(pipeline)",
]

_WEB_PIPELINE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS web_pipeline_runs (
    run_id      TEXT PRIMARY KEY,
    pipeline    TEXT NOT NULL,
    job_id      TEXT,
    trigger     TEXT,
    batch_id    TEXT,
    created_at  TEXT NOT NULL
)
"""

_WEB_PIPELINE_RUNS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_pipeline_runs_pipeline "
    "ON web_pipeline_runs(pipeline, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_pipeline_runs_job ON web_pipeline_runs(job_id)",
]


_WEB_USERS_DDL = """
CREATE TABLE IF NOT EXISTS web_users (
    user_id         TEXT PRIMARY KEY,
    issuer          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    email           TEXT,
    name            TEXT,
    role            TEXT NOT NULL DEFAULT 'pending',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    last_login_at   TEXT,
    UNIQUE(issuer, subject)
)
"""

_WEB_USERS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_users_status ON web_users(status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_web_users_email ON web_users(lower(email))",
]


_WEB_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS web_sessions (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    absolute_deadline TEXT NOT NULL,
    idp_sid         TEXT,
    revoked         INTEGER NOT NULL DEFAULT 0
)
"""

_WEB_SESSIONS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_sessions_user_id ON web_sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_web_sessions_expires_at ON web_sessions(expires_at)",
]


def ensure_schema() -> None:
    """Create web-tier tables and indexes if they do not already exist."""
    path = web_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_WEB_JOBS_DDL)
        for stmt in _WEB_JOBS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_AUDIT_DDL)
        for stmt in _WEB_AUDIT_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_APPROVALS_DDL)
        for stmt in _WEB_APPROVALS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_WATCH_CURSORS_DDL)
        for stmt in _WEB_WATCH_CURSORS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_PIPELINE_RUNS_DDL)
        for stmt in _WEB_PIPELINE_RUNS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_USERS_DDL)
        for stmt in _WEB_USERS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_SESSIONS_DDL)
        for stmt in _WEB_SESSIONS_INDEXES:
            conn.execute(stmt)
    finally:
        conn.close()
    logger.info("Ovid2 web-tier schema ensured at %s", path)


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Yield a short-lived read/write connection to the web-tier DB.

    Uses row-factory :class:`sqlite3.Row` so callers can index by column
    name. Journal mode is WAL and ``isolation_level=None`` (auto-commit),
    matching the scheduler's usage.
    """
    conn = sqlite3.connect(web_db_path(), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
    finally:
        conn.close()


def audit(
    *,
    actor: str,
    action: str,
    target: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    detail_json: str | None = None,
) -> None:
    """Append one row to the ``web_audit`` table.

    Args:
        actor: Identifier of who caused the change (``web``, ``mcp:<client>``,
            ``cli``, …).
        action: Dotted action name (``formatter.write``, ``run.submit``, …).
        target: Optional resource identifier the action mutated.
        before_hash: Optional pre-mutation content hash.
        after_hash: Optional post-mutation content hash.
        detail_json: Optional serialized JSON payload with extra context.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO web_audit "
            "(ts, actor, action, target, before_hash, after_hash, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                actor,
                action,
                target,
                before_hash,
                after_hash,
                detail_json,
            ),
        )
