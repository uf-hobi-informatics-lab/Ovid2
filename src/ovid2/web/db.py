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
from datetime import UTC, datetime
from pathlib import Path

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

# Per-item (table / check) timings of a run, folded from the run's event
# log by ``services.run_items_svc``. The event log lives on tmpfs, so this
# is what lets the run page show every finished item's saved duration — and
# anchor a running item's clock to its real start — the moment it opens.
_WEB_RUN_ITEMS_DDL = """
CREATE TABLE IF NOT EXISTS web_run_items (
    workspace_id  TEXT NOT NULL DEFAULT 'default',
    instance_id   TEXT NOT NULL,
    item_kind     TEXT NOT NULL,
    item          TEXT NOT NULL,
    display_name  TEXT,
    status        TEXT NOT NULL,
    started_at    TEXT,
    ended_at      TEXT,
    elapsed_ms    INTEGER,
    error         TEXT,
    seq           INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (workspace_id, instance_id, item_kind, item)
)
"""

_WEB_PIPELINE_RUNS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_web_pipeline_runs_pipeline "
    "ON web_pipeline_runs(pipeline, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_pipeline_runs_job ON web_pipeline_runs(job_id)",
]


# Workflows (adr-workflows.md): runtime state per workflow definition, one
# row per workflow run (with its frozen definition) and one row per step of
# that run. Workflow *definitions* live as JSON under
# ``$XDG_CONFIG_HOME/ovid2/workflows/``.
_WEB_WORKFLOWS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS web_workflows_state (
        name           TEXT PRIMARY KEY,
        enabled        INTEGER NOT NULL DEFAULT 1,
        last_fired_at  TEXT,
        next_fire_at   TEXT,
        last_run_id    TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_workflow_runs (
        run_id           TEXT PRIMARY KEY,
        workflow         TEXT NOT NULL,
        trigger          TEXT NOT NULL,
        batch_id         TEXT,
        root_input       TEXT,
        status           TEXT NOT NULL,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL,
        created_by       TEXT NOT NULL DEFAULT 'web',
        started_at       TEXT,
        ended_at         TEXT,
        error            TEXT,
        definition_json  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_workflow_runs_wf ON web_workflow_runs(workflow, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_workflow_runs_status ON web_workflow_runs(status)",
    """
    CREATE TABLE IF NOT EXISTS web_workflow_steps (
        run_id                    TEXT NOT NULL,
        step_id                   TEXT NOT NULL,
        pipeline                  TEXT NOT NULL,
        status                    TEXT NOT NULL,
        job_id                    TEXT,
        input_address             TEXT,
        output_address            TEXT,
        pre_existing_tables_json  TEXT,
        backup_address            TEXT,
        snapshot_job_id           TEXT,
        rollback_job_id           TEXT,
        rollback_action           TEXT,
        rollback_plan_json        TEXT,
        error                     TEXT,
        started_at                TEXT,
        ended_at                  TEXT,
        PRIMARY KEY (run_id, step_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_workflow_steps_job ON web_workflow_steps(job_id)",
)

#: Columns added to ``web_jobs`` after its first release. ``ALTER TABLE …
#: ADD COLUMN`` is idempotent here: a duplicate-column error is ignored.
_WEB_JOBS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("workflow_run_id", "TEXT"),
    ("workflow_step_id", "TEXT"),
    # Last-line JSON result of a job-only tool whose answer the web tier
    # needs (``workflow_probe``: numbers only, never row values).
    ("result_json", "TEXT"),
    # Operator pause (adr-atlas.md): 1 while the job should hold at its next
    # table boundary. The worker mirrors it into the run's pause flag file.
    ("pause_requested", "INTEGER NOT NULL DEFAULT 0"),
)

#: Columns added to ``web_workflow_steps`` for branching (adr-workflows.md):
#: the settled outcome, why a step was skipped / how a failure was handled,
#: the facts its outgoing conditions read, the probe job, and the decisions
#: on its outgoing links.
_WEB_WORKFLOW_STEPS_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("outcome", "TEXT"),
    ("reason", "TEXT"),
    ("facts_json", "TEXT"),
    ("probe_job_id", "TEXT"),
    ("decisions_json", "TEXT"),
)


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


# Per-user cosmetic preferences (Appearance panel: theme slug, quick
# colours, per-component overrides, saved named themes). One JSON blob per
# principal id; validated by ``services.prefs_svc`` before it is written.
_WEB_USER_PREFS_DDL = """
CREATE TABLE IF NOT EXISTS web_user_prefs (
    user_id         TEXT PRIMARY KEY,
    appearance_json TEXT NOT NULL,
    updated_at      TEXT NOT NULL
)
"""


# Report Studio (adr-report-studio.md). Report *sources* (aggregated run
# datasets) live as JSON files under ``$XDG_STATE_HOME/ovid2/reports``;
# these tables hold the catalogue rows, the saved layouts, their version
# history, per-user organisation (folders / favourites) and the share ACL.
_REPORT_STUDIO_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS web_report_sources (
        source_id     TEXT PRIMARY KEY,
        kind          TEXT NOT NULL,
        title         TEXT NOT NULL,
        instance_id   TEXT,
        pipeline      TEXT,
        batch_id      TEXT,
        cdm           TEXT,
        generated_at  TEXT,
        ingested_at   TEXT NOT NULL,
        ingested_by   TEXT NOT NULL,
        file_name     TEXT NOT NULL,
        summary_json  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_report_sources_kind ON web_report_sources(kind, generated_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_report_sources_instance ON web_report_sources(instance_id)",
    """
    CREATE TABLE IF NOT EXISTS web_reports (
        report_id     TEXT PRIMARY KEY,
        owner         TEXT NOT NULL,
        title         TEXT NOT NULL,
        description   TEXT NOT NULL DEFAULT '',
        folder        TEXT NOT NULL DEFAULT '',
        tags_json     TEXT NOT NULL DEFAULT '[]',
        source_id     TEXT NOT NULL,
        template_id   TEXT,
        layout_json   TEXT NOT NULL,
        version       INTEGER NOT NULL DEFAULT 1,
        status        TEXT NOT NULL DEFAULT 'saved',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        updated_by    TEXT NOT NULL,
        archived      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_reports_owner ON web_reports(owner, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS ix_web_reports_source ON web_reports(source_id)",
    """
    CREATE TABLE IF NOT EXISTS web_report_versions (
        report_id     TEXT NOT NULL,
        version       INTEGER NOT NULL,
        layout_json   TEXT NOT NULL,
        saved_at      TEXT NOT NULL,
        saved_by      TEXT NOT NULL,
        note          TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (report_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_report_shares (
        report_id       TEXT NOT NULL,
        principal_kind  TEXT NOT NULL,
        principal       TEXT NOT NULL,
        permission      TEXT NOT NULL,
        granted_by      TEXT NOT NULL,
        granted_at      TEXT NOT NULL,
        PRIMARY KEY (report_id, principal_kind, principal)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_report_shares_principal ON web_report_shares(principal_kind, principal)",
    """
    CREATE TABLE IF NOT EXISTS web_report_favorites (
        user_id     TEXT NOT NULL,
        report_id   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (user_id, report_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_report_templates (
        template_id   TEXT PRIMARY KEY,
        owner         TEXT NOT NULL,
        name          TEXT NOT NULL,
        description   TEXT NOT NULL DEFAULT '',
        source_kind   TEXT NOT NULL,
        layout_json   TEXT NOT NULL,
        shared        INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL
    )
    """,
    # Private notes: per-user annotations on a report, deliberately *outside*
    # the shared layout (and its version history) so collaborators, shared
    # viewers and the assistant never receive them.
    """
    CREATE TABLE IF NOT EXISTS web_report_notes (
        note_id         TEXT PRIMARY KEY,
        report_id       TEXT NOT NULL,
        owner           TEXT NOT NULL,
        section_id      TEXT NOT NULL,
        after_block_id  TEXT,
        block_json      TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_report_notes_owner ON web_report_notes(report_id, owner)",
)


# Agent Ovid chat sessions (adr-llm-dual-system.md, "Saved sessions").
# One row per conversation plus its ordered messages. Owned by the
# principal id (``anonymous`` while auth is off). Message text may contain
# PHI: it lives only here (mode-0600 DB), never in ``web_audit``.
_WEB_LLM_SESSIONS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS web_llm_sessions (
        session_id              TEXT PRIMARY KEY,
        user_id                 TEXT NOT NULL,
        title                   TEXT NOT NULL DEFAULT '',
        title_source            TEXT NOT NULL DEFAULT 'pending',
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL,
        last_prompt_tokens      INTEGER NOT NULL DEFAULT 0,
        total_prompt_tokens     INTEGER NOT NULL DEFAULT 0,
        total_completion_tokens INTEGER NOT NULL DEFAULT 0,
        connection              TEXT,
        model                   TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_llm_sessions_user ON web_llm_sessions(user_id, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS web_llm_messages (
        message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        meta_json   TEXT,
        created_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_llm_messages_session ON web_llm_messages(session_id, seq)",
)


# Ovid Atlas (adr-atlas.md): shared, workspace-wide saved views of the
# /navigator canvas (layer toggles, pinned / expanded nodes, positions) and
# the presence heartbeats behind "who is looking at / editing what" plus the
# soft edit locks. Presence rows are short-lived and pruned on every write.
_WEB_ATLAS_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS web_atlas_layouts (
        workspace_id  TEXT NOT NULL DEFAULT 'default',
        name          TEXT NOT NULL,
        layout_json   TEXT NOT NULL,
        created_by    TEXT NOT NULL,
        updated_by    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        PRIMARY KEY (workspace_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_atlas_presence (
        workspace_id  TEXT NOT NULL DEFAULT 'default',
        user_id       TEXT NOT NULL,
        client_id     TEXT NOT NULL,
        display_name  TEXT NOT NULL DEFAULT '',
        node          TEXT,
        mode          TEXT NOT NULL DEFAULT 'view',
        seen_at       TEXT NOT NULL,
        PRIMARY KEY (workspace_id, client_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_atlas_presence_seen ON web_atlas_presence(seen_at)",
)


# Workspaces (adr-workspaces.md). ``workspace_id`` *is* the immutable slug
# (used in paths and the ``X-Ovid2-Workspace`` header); ``display_name`` is
# editable. A membership's ``ws_role`` is ``member`` or ``admin`` — the
# latter may manage that workspace's members and nothing else; what a member
# may *do* is still their global role.
_WEB_WORKSPACES_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS web_workspaces (
        workspace_id   TEXT PRIMARY KEY,
        display_name   TEXT NOT NULL,
        description    TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT 'active',
        created_by     TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS web_workspace_members (
        workspace_id   TEXT NOT NULL,
        user_id        TEXT NOT NULL,
        ws_role        TEXT NOT NULL DEFAULT 'member',
        added_by       TEXT NOT NULL,
        added_at       TEXT NOT NULL,
        PRIMARY KEY (workspace_id, user_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_web_workspace_members_user ON web_workspace_members(user_id)",
)

#: Row tables that carry a ``workspace_id`` column (added idempotently; every
#: pre-workspace row lands in ``default``). Pinned by
#: ``tests/contract/test_workspace_scoped_queries.py``.
WORKSPACE_SCOPED_TABLES: tuple[str, ...] = (
    "web_jobs",
    "web_workflow_runs",
    "web_report_sources",
    "web_reports",
    "web_report_templates",
    "web_approvals",
    "web_audit",
    "web_llm_sessions",
    "web_run_items",
    "web_atlas_layouts",
    "web_atlas_presence",
)


def _add_column(conn: sqlite3.Connection, table: str, col: str, col_type: str) -> None:
    """``ALTER TABLE … ADD COLUMN`` unless the column already exists."""
    existing = {str(r[1]) for r in conn.execute("PRAGMA table_info(" + table + ")")}
    if col not in existing:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE " + table + " ADD COLUMN " + col + " " + col_type)


def _ensure_workspace_schema(conn: sqlite3.Connection) -> None:
    """Create the workspace tables, scope columns and the ``default`` workspace.

    Idempotent. The first run (upgrade) also enrols every *active* user in
    ``default`` so nobody loses access; later activations enrol through
    :func:`ovid2.web.services.workspaces_svc.enrol_default`.
    """
    for stmt in _WEB_WORKSPACES_DDL:
        conn.execute(stmt)
    for table in WORKSPACE_SCOPED_TABLES:
        _add_column(conn, table, "workspace_id", "TEXT NOT NULL DEFAULT 'default'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_" + table + "_workspace ON " + table + "(workspace_id)"
        )
    _add_column(conn, "web_sessions", "active_workspace_id", "TEXT")
    _add_column(conn, "web_users", "last_workspace_id", "TEXT")
    now = datetime.now(UTC).isoformat()
    created = conn.execute(
        "INSERT OR IGNORE INTO web_workspaces "
        "(workspace_id, display_name, description, status, created_by, created_at, updated_at) "
        "VALUES ('default', 'Default', '', 'active', 'migration', ?, ?)",
        (now, now),
    ).rowcount
    if created:
        conn.execute(
            "INSERT OR IGNORE INTO web_workspace_members "
            "(workspace_id, user_id, ws_role, added_by, added_at) "
            "SELECT 'default', user_id, 'member', 'migration', ? FROM web_users "
            "WHERE status = 'active'",
            (now,),
        )


#: Seconds a connection waits for another writer's lock before raising
#: ``database is locked``. The web server, the worker and every job's
#: ``ovid2`` process share this file; without a wait a single overlapping
#: write crashed the worker (and SIGTERMed every running job).
BUSY_TIMEOUT_S = 30.0


def _open(path: Path) -> sqlite3.Connection:
    """Open ``path`` in auto-commit mode with a busy wait and WAL."""
    # ``timeout`` sets SQLite's busy handler (= PRAGMA busy_timeout).
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_S)
    try:
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def ensure_schema() -> None:
    """Create web-tier tables and indexes if they do not already exist."""
    path = web_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(path)
    try:
        for stmt in _REPORT_STUDIO_DDL:
            conn.execute(stmt)
        conn.execute(_WEB_JOBS_DDL)
        for stmt in _WEB_JOBS_INDEXES:
            conn.execute(stmt)
        existing_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(web_jobs)")}
        for col, col_type in _WEB_JOBS_ADDED_COLUMNS:
            if col not in existing_cols:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute("ALTER TABLE web_jobs ADD COLUMN " + col + " " + col_type)
        for stmt in _WEB_WORKFLOWS_DDL:
            conn.execute(stmt)
        step_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(web_workflow_steps)")}
        for col, col_type in _WEB_WORKFLOW_STEPS_ADDED_COLUMNS:
            if col not in step_cols:
                with contextlib.suppress(sqlite3.OperationalError):
                    conn.execute(
                        "ALTER TABLE web_workflow_steps ADD COLUMN " + col + " " + col_type
                    )
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
        conn.execute(_WEB_RUN_ITEMS_DDL)
        # Start of the item's current *active* segment (multi-phase runs
        # pause a table between phases — see ``run_items_svc``).
        _add_column(conn, "web_run_items", "active_since", "TEXT")
        conn.execute(_WEB_USERS_DDL)
        for stmt in _WEB_USERS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_SESSIONS_DDL)
        for stmt in _WEB_SESSIONS_INDEXES:
            conn.execute(stmt)
        conn.execute(_WEB_USER_PREFS_DDL)
        for stmt in _WEB_LLM_SESSIONS_DDL:
            conn.execute(stmt)
        for stmt in _WEB_ATLAS_DDL:
            conn.execute(stmt)
        _ensure_workspace_schema(conn)
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
    conn = _open(web_db_path())
    conn.row_factory = sqlite3.Row
    try:
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

    The row is stamped with the active workspace
    (:func:`ovid2.workspaces.active_workspace`).
    """
    from ovid2.web.request_context import resolve_actor
    from ovid2.workspaces import active_workspace

    actor = resolve_actor(actor)
    with connect() as conn:
        conn.execute(
            "INSERT INTO web_audit "
            "(ts, actor, action, target, before_hash, after_hash, detail_json, workspace_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                actor,
                action,
                target,
                before_hash,
                after_hash,
                detail_json,
                active_workspace(),
            ),
        )


def state_key(name: str, workspace: str | None = None) -> str:
    """Return the key a per-name state row is stored under.

    ``web_pipelines_state`` / ``web_workflows_state`` / ``web_watch_cursors``
    are keyed by resource name. Two workspaces may both own a pipeline
    called ``nightly``, so a non-default workspace's rows are namespaced
    ``<slug>::<name>`` (resource names never contain ``::``); the
    ``default`` workspace keeps the bare historical key, so upgrading
    rewrites nothing.
    """
    from ovid2.workspaces import DEFAULT_WORKSPACE, active_workspace

    ws = workspace or active_workspace()
    return name if ws == DEFAULT_WORKSPACE else f"{ws}::{name}"
