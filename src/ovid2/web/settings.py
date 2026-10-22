"""Web-tier settings.

All knobs are environment-overridable so packagers can supply values via
systemd ``Environment=`` directives or docker-compose ``environment:``
blocks without rebuilding the wheel.

The default bind is loopback only (``127.0.0.1:8090``). TLS and public
exposure are handled by a shared, box-level reverse proxy on the host
that owns ``443``, routes a per-app hostname (e.g. ``ovid2.<domain>``)
to this loopback port, and reads certs from its own certificate store (e.g. the OS certbot's
``/etc/letsencrypt/live``). This process never sees the cert/key
material and must NOT be bound to the host's LAN address or
``0.0.0.0`` — that would re-expose the unauthenticated,
plaintext panel and bypass the proxy. Set ``OVID2_WEB_BIND`` (host:port)
to override (e.g. an SSH-tunnel or dev workflow).
"""

from __future__ import annotations

import os
from pathlib import Path

from ovid2.config import paths as xdg_paths

# ---------------------------------------------------------------------------
# HTTP bind
# ---------------------------------------------------------------------------

#: Host:port to bind the FastAPI/uvicorn process to. Loopback only; the
#: shared host reverse proxy terminates TLS on 443 and forwards here.
WEB_BIND: str = os.environ.get("OVID2_WEB_BIND", "127.0.0.1:8090")

#: Host:port for the MCP HTTP transport (stdio remains the default).
#: Loopback only; fronted by the shared reverse proxy (distinct hostname).
MCP_BIND: str = os.environ.get("OVID2_MCP_BIND", "127.0.0.1:8091")

#: Base URL the MCP server uses to reach the FastAPI backend.
MCP_UPSTREAM: str = os.environ.get("OVID2_WEB_URL", f"http://{WEB_BIND}")

#: Base URL the assistant's executor child uses to call back into the API
#: for read-only tools. Empty → derived per request from the socket the
#: server is actually listening on (``request.scope["server"]``), which
#: bypasses the reverse proxy.
LLM_TOOLS_API_URL: str = os.environ.get("OVID2_LLM_TOOLS_API_URL", "").strip()

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def web_db_path() -> Path:
    """Return the SQLite file the web tier owns.

    Reuses the existing scheduler DB when it lives at
    ``$XDG_STATE_HOME/ovid2/scheduler.db`` (or the ``OVID2_SCHEDULER_DB``
    override) — the ``web_*`` tables are additive. Callers must invoke
    :func:`ovid2.web.db.ensure_schema` before touching the DB.
    """
    override = os.environ.get("OVID2_SCHEDULER_DB")
    if override:
        return Path(override)
    return xdg_paths.state_dir() / "scheduler.db"


def web_log_dir() -> Path:
    """Return the directory where the web tier stores per-job stderr logs."""
    override = os.environ.get("OVID2_WEB_LOG_DIR")
    if override:
        return Path(override)
    return xdg_paths.state_dir() / "web_logs"


def admin_storage_connection() -> str | None:
    """Return the configured admin storage-account (connection) name.

    The *admin storage account* backs Ovid2's own operational state —
    scheduler / job / audit / watch-cursor / run bookkeeping. It is
    modelled as a first-class Ovid2 storage account (a connection by
    name) and configured via ``settings.admin.storage_account``.

    Returns ``None`` when unset, which means "use the local
    ``scheduler.db`` at :func:`web_db_path`, exactly as today". No
    behaviour changes until an operator points this at a connection.

    The import of :mod:`ovid2.web.services.settings_svc` is deferred
    inside the function body to avoid an import cycle
    (``settings_svc`` → ``db`` → ``settings``).
    """
    from ovid2.web.services import settings_svc

    return settings_svc.get_settings().admin.storage_account


# ---------------------------------------------------------------------------
# UI polling
# ---------------------------------------------------------------------------

#: Milliseconds between HTMX ``hx-trigger="every …"`` polls on the dashboard.
DASHBOARD_POLL_MS: int = int(os.environ.get("OVID2_WEB_DASHBOARD_POLL_MS", "3000"))

#: Milliseconds between HTMX polls on the run-detail event stream.
RUN_EVENTS_POLL_MS: int = int(os.environ.get("OVID2_WEB_RUN_EVENTS_POLL_MS", "2000"))

#: Maximum number of events returned per ``/api/v1/runs/{id}/events`` call.
RUN_EVENTS_PAGE_SIZE: int = int(os.environ.get("OVID2_WEB_RUN_EVENTS_PAGE_SIZE", "500"))

# ---------------------------------------------------------------------------
# Live metrics (SSE)
# ---------------------------------------------------------------------------

#: Seconds between SSE comment keepalives on the ``/metrics/stream``
#: endpoint. Prevents reverse-proxy idle timeouts (Nginx defaults to
#: 60 s) during long stretches with no metric emission.
LIVE_SSE_KEEPALIVE_S: int = int(os.environ.get("OVID2_WEB_LIVE_SSE_KEEPALIVE_S", "15"))

#: Maximum lifetime of a single SSE stream connection (seconds). Even
#: with keepalives we want an upper bound so a forgotten browser tab
#: doesn't hold a worker forever. Client can reconnect.
LIVE_SSE_MAX_SECONDS: int = int(
    os.environ.get("OVID2_WEB_LIVE_SSE_MAX_SECONDS", str(60 * 60))
)

#: Max reconnect attempts before the client falls back to polling
#: ``/metrics/latest``. Exposed to the frontend via a small `<meta>`
#: tag written by the run-detail template.
LIVE_SSE_MAX_RECONNECT: int = int(
    os.environ.get("OVID2_WEB_LIVE_SSE_MAX_RECONNECT", "5")
)

# ---------------------------------------------------------------------------
# Worker (W3+)
# ---------------------------------------------------------------------------

#: Maximum number of concurrent ``ovid2 etl`` subprocesses.
WEB_MAX_PARALLEL_JOBS: int = int(os.environ.get("OVID2_WEB_MAX_PARALLEL_JOBS", "1"))

#: Poll interval for the job worker (seconds).
WEB_WORKER_POLL_SECONDS: float = float(os.environ.get("OVID2_WEB_WORKER_POLL_SECONDS", "2.0"))

# ---------------------------------------------------------------------------
# Executor least-privilege (harness/API/executor trust model)
# ---------------------------------------------------------------------------

#: Environment variables the worker copies verbatim into a spawned
#: executor subprocess. Everything else in the worker's own environment
#: is dropped, so ambient credentials (e.g. ``AWS_SECRET_ACCESS_KEY``
#: exported into the worker's shell) never leak into a job. The executor
#: gets its credentials exclusively from the scoped ``secrets.d/`` mounts,
#: never from inherited env. See
#: the adr-harness-api-executor-trust-model design note.
#:
#: Names ending in ``*`` are treated as prefixes (e.g. ``OVID2_*`` keeps
#: every ``OVID2_`` variable). Operators can extend the list via
#: ``OVID2_WORKER_ENV_ALLOWLIST`` (comma-separated) without a rebuild.
_DEFAULT_WORKER_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_*",
    "TZ",
    "TMPDIR",
    "XDG_*",
    "OVID2_*",
    "JAVA_HOME",
    "SPARK_*",
    "PYTHONPATH",
    "PYTHONUNBUFFERED",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)


def _worker_env_allowlist() -> tuple[str, ...]:
    """Return the effective env allow-list (defaults + operator extras)."""
    extra = os.environ.get("OVID2_WORKER_ENV_ALLOWLIST", "")
    extras = tuple(tok.strip() for tok in extra.split(",") if tok.strip())
    return _DEFAULT_WORKER_ENV_ALLOWLIST + extras


#: The effective allow-list, resolved once at import time.
WORKER_ENV_ALLOWLIST: tuple[str, ...] = _worker_env_allowlist()
