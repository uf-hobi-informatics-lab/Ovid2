"""Install-wizard service (W5).

Drives a **step-based first-time setup** through the web UI:

1. ``bootstrap``  — run :func:`ovid2.installer.init.bootstrap` to create
   the XDG tree, drop sample address configs, and seed a
   ``secrets.d/snowflake.json`` template.
2. ``snowflake``  — the operator fills in real Snowflake credentials
   (writes through :mod:`ovid2.web.services.secrets_svc`, which enforces
   ``0o600``).
3. ``addresses``  — at least one address config must be present. The
   wizard re-uses :mod:`ovid2.web.services.addresses_svc`.
4. ``image``      — build the Ovid2 Docker image. Long-running: the
   invocation is executed by a background thread and its state is
   persisted in the ``web_image_builds`` table so the wizard can be
   reloaded without losing progress.
5. ``service``    — the systemd unit lives under ``/etc/`` and requires
   root. The wizard therefore *prints* the exact
   ``sudo ovid2 service install …`` command and re-runs
   :mod:`ovid2.installer.doctor` to detect completion.
6. ``pipeline``    — pick a pipeline package (namespace under
   ``ovid2_pipelines.*``). Read-only in W5 — the wizard verifies that
   the package resolves. Editing formatters / mappers is already
   available under ``/pipelines`` (W4).
7. ``done``       — congratulate the operator and hand off to the
   ``/submit`` page.

Every step is **idempotent**: re-running it on an already-completed
state is a no-op. State is *derived* from disk on every request rather
than persisted in its own table — the wizard is a view over the world,
not a state machine of its own.

Design notes:

- Docker builds are captured in ``web_image_builds`` (see
  :mod:`ovid2.web.db`). Only one build may be in-flight at a time; the
  service refuses concurrent submissions.
- The wizard emits ``install.*`` audit rows on every write path so the
  operator can trace what happened from the ``web_audit`` table.
- Contract tests already forbid the web tier from importing pipeline
  code — this module only depends on :mod:`ovid2.installer`,
  :mod:`ovid2.config`, and other web services.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ovid2.config import paths as _paths
from ovid2.config.settings import DOCKER_IMAGE
from ovid2.installer import doctor as _doctor
from ovid2.installer import init as _init
from ovid2.web import db as web_db
from ovid2.web.models import (
    ImageBuildDetail,
    ImageBuildListResponse,
    InstallServiceCommand,
    InstallStatus,
    InstallStepStatus,
    SecretWrite,
)
from ovid2.web.services import addresses_svc, pipelines_meta_svc, secrets_svc
from ovid2.web.settings import web_log_dir

logger = logging.getLogger(__name__)

__all__ = [
    "BUILD_FAILED",
    "BUILD_RUNNING",
    "BUILD_SUCCEEDED",
    "SNOWFLAKE_REQUIRED_KEYS",
    "SNOWFLAKE_SECRET_NAME",
    "STEP_KEYS",
    "InstallError",
    "compute_status",
    "get_image_build",
    "get_service_command",
    "image_build_log_path",
    "list_image_builds",
    "run_bootstrap",
    "save_snowflake_secret",
    "start_image_build",
    "verify_pipeline",
]

# ---------------------------------------------------------------------------
# Step keys — the wizard's linear ordering.
# ---------------------------------------------------------------------------

STEP_KEYS: tuple[str, ...] = (
    "bootstrap",
    "snowflake",
    "addresses",
    "image",
    "service",
    "pipeline",
    "done",
)

#: The bundled Snowflake template file names.
SNOWFLAKE_SECRET_NAME: str = "snowflake"  # noqa: S105 — file stem, not a password

#: Keys the Snowflake secret must contain to count as "filled in".
SNOWFLAKE_REQUIRED_KEYS: tuple[str, ...] = ("account", "user", "password")

#: The exact set of values considered placeholder / unfilled.
_PLACEHOLDER_VALUES: frozenset[str] = frozenset({"", "REPLACE_ME", "CHANGEME"})

BUILD_RUNNING: str = "running"
BUILD_SUCCEEDED: str = "succeeded"
BUILD_FAILED: str = "failed"

_VALID_BUILD_STATUSES: frozenset[str] = frozenset({BUILD_RUNNING, BUILD_SUCCEEDED, BUILD_FAILED})


class InstallError(ValueError):
    """Raised when an install-wizard action fails validation."""


# ---------------------------------------------------------------------------
# Schema — additive `web_image_builds` table lives in scheduler.db.
# ---------------------------------------------------------------------------

_IMAGE_BUILDS_DDL = """
CREATE TABLE IF NOT EXISTS web_image_builds (
    build_id       TEXT PRIMARY KEY,
    submitted_at   TEXT NOT NULL,
    submitted_by   TEXT NOT NULL DEFAULT 'web',
    tag            TEXT NOT NULL,
    context        TEXT NOT NULL,
    dockerfile     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'running',
    pid            INTEGER,
    started_at     TEXT,
    ended_at       TEXT,
    exit_code      INTEGER,
    error          TEXT
)
"""

_IMAGE_BUILDS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_web_image_builds_status "
    "ON web_image_builds(status, submitted_at DESC)",
)


def _ensure_image_builds_schema() -> None:
    """Create the ``web_image_builds`` table on demand.

    Layered on top of :func:`ovid2.web.db.ensure_schema` — we do not add
    the DDL to the base schema helper because ``install_svc`` is the
    only writer and the table is optional (an operator that never uses
    the wizard never sees it).
    """
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(_IMAGE_BUILDS_DDL)
        for stmt in _IMAGE_BUILDS_INDEXES:
            conn.execute(stmt)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def _new_build_id() -> str:
    """Return a fresh opaque build id."""
    return uuid.uuid4().hex


def _hash(payload: dict[str, Any]) -> str:
    """Return a stable content hash for audit rows."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _image_build_log_dir() -> Path:
    """Return the directory used to capture ``docker build`` output."""
    directory = web_log_dir() / "image_builds"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def image_build_log_path(build_id: str) -> Path:
    """Return the on-disk log path for one image build."""
    return _image_build_log_dir() / f"{build_id}.log"


# ---------------------------------------------------------------------------
# Step 1 — bootstrap XDG tree
# ---------------------------------------------------------------------------


def run_bootstrap(*, actor: str = "web") -> list[str]:
    """Run :func:`ovid2.installer.init.bootstrap` (non-interactive).

    Args:
        actor: Free-form origin label recorded in ``web_audit``.

    Returns:
        Ordered log lines returned by the bootstrap helper.
    """
    log = _init.bootstrap(interactive=False)
    _ensure_image_builds_schema()
    web_db.audit(
        actor=actor,
        action="install.bootstrap",
        target="xdg",
        detail_json=json.dumps({"log": log}),
    )
    logger.info("Install-wizard bootstrap ran: %d actions", len(log))
    return log


# ---------------------------------------------------------------------------
# Step 2 — Snowflake secret
# ---------------------------------------------------------------------------


def _read_snowflake_values() -> dict[str, Any] | None:
    """Return the on-disk contents of ``secrets.d/snowflake.json``.

    Returns ``None`` when the file is missing. Values are never returned
    outside :mod:`ovid2.web.services.install_svc`; this helper exists
    only to detect placeholder templates and is not exposed on the API.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    candidates = (
        Path(xdg_config) / "ovid2" / "secrets.d" / f"{SNOWFLAKE_SECRET_NAME}.json",
        Path("/etc/ovid2/secrets.d") / f"{SNOWFLAKE_SECRET_NAME}.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                with candidate.open(encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, json.JSONDecodeError):
                return None
            if isinstance(data, dict):
                return data
    return None


def _is_snowflake_filled(values: dict[str, Any] | None) -> bool:
    """Return ``True`` when the Snowflake secret has real (non-placeholder) values."""
    if values is None:
        return False
    for key in SNOWFLAKE_REQUIRED_KEYS:
        value = values.get(key)
        if not isinstance(value, str) or value.strip() in _PLACEHOLDER_VALUES:
            return False
    return True


def save_snowflake_secret(payload: SecretWrite, *, actor: str = "web") -> dict[str, Any]:
    """Persist the Snowflake secret using the shared secret editor.

    Args:
        payload: The :class:`SecretWrite` posted by the client.
        actor: Free-form origin label.

    Returns:
        Summary dict with the resulting file path and hashed contents
        for the audit trail. Values are **never** included.
    """
    if not isinstance(payload.values, dict):
        raise InstallError("Snowflake secret payload must be a JSON object.")
    missing = [k for k in SNOWFLAKE_REQUIRED_KEYS if not payload.values.get(k)]
    if missing:
        raise InstallError(f"Snowflake secret is missing required keys: {sorted(missing)}.")
    try:
        result = secrets_svc.write_secret(SNOWFLAKE_SECRET_NAME, payload, actor=actor)
    except secrets_svc.SecretError as exc:
        raise InstallError(str(exc)) from exc
    web_db.audit(
        actor=actor,
        action="install.snowflake",
        target=SNOWFLAKE_SECRET_NAME,
        after_hash=_hash({"keys": sorted(payload.values.keys())}),
        detail_json=json.dumps({"path": result.path, "action": result.action}),
    )
    return {"path": result.path, "action": result.action}


# ---------------------------------------------------------------------------
# Step 4 — docker image build (background subprocess)
# ---------------------------------------------------------------------------


def _row_to_build_detail(row: dict[str, Any]) -> ImageBuildDetail:
    """Convert a ``web_image_builds`` row (dict) into an :class:`ImageBuildDetail`."""
    return ImageBuildDetail(
        build_id=str(row["build_id"]),
        submitted_at=str(row["submitted_at"]),
        submitted_by=str(row["submitted_by"]),
        tag=str(row["tag"]),
        context=str(row["context"]),
        dockerfile=str(row["dockerfile"]),
        status=str(row["status"]),
        pid=int(row["pid"]) if row.get("pid") is not None else None,
        started_at=str(row["started_at"]) if row.get("started_at") else None,
        ended_at=str(row["ended_at"]) if row.get("ended_at") else None,
        exit_code=(int(row["exit_code"]) if row.get("exit_code") is not None else None),
        error=str(row["error"]) if row.get("error") else None,
    )


def _find_repo_dockerfile() -> Path | None:
    """Best-effort lookup for a repo-local ``docker/Dockerfile``.

    Walks up from this module looking for a sibling ``docker/`` next
    to ``src/ovid2/``. Overridable via ``OVID2_DOCKER_CONTEXT``.
    """
    override = os.environ.get("OVID2_DOCKER_CONTEXT")
    if override:
        return Path(override) / "docker" / "Dockerfile"
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docker" / "Dockerfile"
        if candidate.is_file():
            return candidate
    return None


def _default_build_context() -> Path:
    """Return the default ``docker build`` context directory."""
    override = os.environ.get("OVID2_DOCKER_CONTEXT")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "docker" / "Dockerfile").is_file():
            return parent
    return Path.cwd()


def _insert_build_row(
    *,
    build_id: str,
    tag: str,
    context: Path,
    dockerfile: Path,
    actor: str,
) -> None:
    """Append a new ``web_image_builds`` row."""
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_image_builds "
            "(build_id, submitted_at, submitted_by, tag, context, dockerfile, "
            "status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                build_id,
                _now_iso(),
                actor,
                tag,
                str(context),
                str(dockerfile),
                BUILD_RUNNING,
                _now_iso(),
            ),
        )


def _update_build_row(
    build_id: str,
    *,
    status: str,
    exit_code: int | None = None,
    error: str | None = None,
    pid: int | None = None,
) -> None:
    """Update a build row with the given fields."""
    ended_at = _now_iso() if status != BUILD_RUNNING else None
    with web_db.connect() as conn:
        conn.execute(
            "UPDATE web_image_builds SET status = ?, pid = COALESCE(?, pid), "
            "ended_at = COALESCE(?, ended_at), exit_code = ?, error = ? "
            "WHERE build_id = ?",
            (status, pid, ended_at, exit_code, error, build_id),
        )


def _has_running_build() -> bool:
    """Return ``True`` when a build is currently in-flight."""
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM web_image_builds WHERE status = ? LIMIT 1",
            (BUILD_RUNNING,),
        ).fetchone()
    return row is not None


def _run_build_process(
    *,
    build_id: str,
    tag: str,
    context: Path,
    dockerfile: Path,
    docker_bin: str,
    log_path: Path,
) -> None:
    """Blocking subprocess wrapper — updates the DB row on completion.

    Intended to be launched on a :class:`threading.Thread`. The Docker
    CLI itself runs asynchronously in the daemon; we just wait on the
    client process. All stdout/stderr goes to ``log_path``.
    """
    cmd = [docker_bin, "build", "-t", tag, "-f", str(dockerfile), str(context)]
    logger.info("Image build %s starting: %s", build_id, " ".join(cmd))
    try:
        with log_path.open("wb") as log_fh:
            process = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            _update_build_row(build_id, status=BUILD_RUNNING, pid=process.pid)
            exit_code = process.wait()
    except (OSError, subprocess.SubprocessError) as exc:
        _update_build_row(
            build_id,
            status=BUILD_FAILED,
            exit_code=-1,
            error=f"docker invocation failed: {exc}",
        )
        return
    if exit_code == 0:
        _update_build_row(build_id, status=BUILD_SUCCEEDED, exit_code=0)
    else:
        _update_build_row(
            build_id,
            status=BUILD_FAILED,
            exit_code=exit_code,
            error=f"docker build exited with code {exit_code}",
        )


def start_image_build(
    *,
    tag: str | None = None,
    context: str | None = None,
    dockerfile: str | None = None,
    actor: str = "web",
) -> ImageBuildDetail:
    """Kick off a ``docker build`` in a background thread.

    Args:
        tag: Image tag (defaults to :data:`ovid2.config.settings.DOCKER_IMAGE`).
        context: Build-context directory (defaults to the repo-local
            directory containing ``docker/Dockerfile``).
        dockerfile: Path to a ``Dockerfile`` (defaults to
            ``<context>/docker/Dockerfile``).
        actor: Audit label recorded on submission.

    Returns:
        :class:`ImageBuildDetail` describing the freshly-inserted row.

    Raises:
        InstallError: When docker is missing, another build is already
            running, or the requested paths do not exist.
    """
    _ensure_image_builds_schema()
    if _has_running_build():
        raise InstallError(
            "An image build is already in progress. Wait for it to finish or cancel it."
        )
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        raise InstallError("`docker` is not on PATH. Install Docker before building the image.")

    resolved_tag = tag or DOCKER_IMAGE
    ctx = Path(context).resolve() if context else _default_build_context()
    if not ctx.is_dir():
        raise InstallError(f"Build context '{ctx}' does not exist or is not a directory.")
    if dockerfile:
        df = Path(dockerfile).resolve()
    else:
        detected = _find_repo_dockerfile()
        df = detected if detected is not None else ctx / "docker" / "Dockerfile"
    if not df.is_file():
        raise InstallError(f"Dockerfile '{df}' not found.")

    build_id = _new_build_id()
    _insert_build_row(
        build_id=build_id,
        tag=resolved_tag,
        context=ctx,
        dockerfile=df,
        actor=actor,
    )
    log_path = image_build_log_path(build_id)
    with contextlib.suppress(OSError):
        log_path.parent.mkdir(parents=True, exist_ok=True)

    thread = threading.Thread(
        target=_run_build_process,
        kwargs={
            "build_id": build_id,
            "tag": resolved_tag,
            "context": ctx,
            "dockerfile": df,
            "docker_bin": docker_bin,
            "log_path": log_path,
        },
        name=f"image-build-{build_id[:8]}",
        daemon=True,
    )
    thread.start()

    web_db.audit(
        actor=actor,
        action="install.image_build",
        target=resolved_tag,
        detail_json=json.dumps({"build_id": build_id, "context": str(ctx), "dockerfile": str(df)}),
    )
    logger.info("Image build %s launched (tag=%s)", build_id, resolved_tag)
    return get_image_build(build_id) or ImageBuildDetail(
        build_id=build_id,
        submitted_at=_now_iso(),
        submitted_by=actor,
        tag=resolved_tag,
        context=str(ctx),
        dockerfile=str(df),
        status=BUILD_RUNNING,
    )


def get_image_build(build_id: str) -> ImageBuildDetail | None:
    """Return one image-build row or ``None`` when unknown."""
    _ensure_image_builds_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_image_builds WHERE build_id = ?", (build_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_build_detail(dict(row))


def list_image_builds(*, limit: int = 20, offset: int = 0) -> ImageBuildListResponse:
    """Return a paginated slice of ``web_image_builds``."""
    _ensure_image_builds_schema()
    with web_db.connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) AS n FROM web_image_builds").fetchone()["n"])
        rows = conn.execute(
            "SELECT * FROM web_image_builds ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return ImageBuildListResponse(
        total=total,
        limit=limit,
        offset=offset,
        builds=[_row_to_build_detail(dict(r)) for r in rows],
    )


def latest_image_build() -> ImageBuildDetail | None:
    """Return the most recently submitted build, if any."""
    _ensure_image_builds_schema()
    with web_db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM web_image_builds ORDER BY submitted_at DESC LIMIT 1"
        ).fetchone()
    return _row_to_build_detail(dict(row)) if row is not None else None


# ---------------------------------------------------------------------------
# Step 5 — systemd service (sudo hand-off)
# ---------------------------------------------------------------------------


def get_service_command(
    *, user: str | None = None, ovid2_bin: str | None = None
) -> InstallServiceCommand:
    """Return the ``sudo ovid2 service install`` command to run.

    Args:
        user: Unix user the daemon should run as (defaults to
            ``$USER`` / current process owner).
        ovid2_bin: Path to the ``ovid2`` executable (defaults to the
            first ``ovid2`` on ``$PATH``, or ``sys.executable`` when the
            console script is not installed).
    """
    resolved_user = user or getpass.getuser()
    resolved_bin = ovid2_bin or shutil.which("ovid2") or sys.executable
    command = f"sudo ovid2 service install --user {resolved_user} --bin {resolved_bin}"
    return InstallServiceCommand(command=command, user=resolved_user, ovid2_bin=resolved_bin)


def _systemd_installed() -> bool:
    """Return True when the ``ovid2-scheduler.service`` unit file is present."""
    target = Path("/etc/systemd/system/ovid2-scheduler.service")
    return target.is_file()


# ---------------------------------------------------------------------------
# Step 6 — pipeline verification
# ---------------------------------------------------------------------------


def verify_pipeline(name: str) -> dict[str, Any]:
    """Return metadata for one pipeline package or raise.

    Args:
        name: Pipeline package name (matches
            :func:`ovid2.web.services.pipelines_meta_svc.discover_pipelines`).

    Returns:
        Small metadata dict — used by the wizard to display a
        confirmation card. Full detail is available under
        :func:`ovid2.web.services.pipelines_meta_svc.get_pipeline`.

    Raises:
        InstallError: When the package cannot be resolved.
    """
    if not name:
        raise InstallError("Pipeline name is required.")
    detail = pipelines_meta_svc.get_pipeline(name)
    if detail is None:
        raise InstallError(f"Pipeline '{name}' not found under ovid2_pipelines.")
    return {
        "name": detail.name,
        "path": detail.path,
        "formatters": len(detail.formatters),
        "mappers": len(detail.mappers),
    }


# ---------------------------------------------------------------------------
# Aggregate status
# ---------------------------------------------------------------------------


def _step_bootstrap() -> InstallStepStatus:
    """Check whether the XDG tree exists and holds sample configs."""
    config = _paths.config_dir()
    configs = _paths.configs_dir()
    secrets = _paths.secrets_dir()
    missing = [str(p) for p in (config, configs, secrets) if not p.is_dir()]
    if missing:
        return InstallStepStatus(
            key="bootstrap",
            status="pending",
            summary="XDG directories not yet created.",
            detail={"missing": missing},
        )
    return InstallStepStatus(
        key="bootstrap",
        status="done",
        summary="XDG tree ready.",
        detail={
            "config_dir": str(config),
            "configs_dir": str(configs),
            "secrets_dir": str(secrets),
        },
    )


def _step_snowflake() -> InstallStepStatus:
    """Check whether ``secrets.d/snowflake.json`` has real values."""
    values = _read_snowflake_values()
    if values is None:
        return InstallStepStatus(
            key="snowflake",
            status="pending",
            summary="secrets.d/snowflake.json missing — run bootstrap first.",
        )
    filled = _is_snowflake_filled(values)
    keys = sorted(values.keys())
    # Also verify the file has the correct permissions.
    xdg_config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(xdg_config) / "ovid2" / "secrets.d" / f"{SNOWFLAKE_SECRET_NAME}.json"
    mode_ok = True
    if path.is_file():
        mode_ok = stat.S_IMODE(path.stat().st_mode) == 0o600
    if not filled:
        return InstallStepStatus(
            key="snowflake",
            status="pending",
            summary="Snowflake credentials still contain placeholders.",
            detail={"keys": keys, "mode_0600": mode_ok},
        )
    if not mode_ok:
        return InstallStepStatus(
            key="snowflake",
            status="warning",
            summary="Snowflake secret is filled but file mode is not 0600.",
            detail={"keys": keys, "path": str(path)},
        )
    return InstallStepStatus(
        key="snowflake",
        status="done",
        summary="Snowflake credentials configured (mode 0600).",
        detail={"keys": keys},
    )


def _step_addresses() -> InstallStepStatus:
    """Verify at least one non-sample address config is present."""
    summaries = addresses_svc.list_addresses()
    real = [s for s in summaries if not s.name.endswith("_sample")]
    if not real:
        return InstallStepStatus(
            key="addresses",
            status="pending",
            summary="No address configs found beyond the bundled samples.",
            detail={"samples_present": len(summaries)},
        )
    return InstallStepStatus(
        key="addresses",
        status="done",
        summary=f"{len(real)} address config(s) configured.",
        detail={"addresses": [s.name for s in real]},
    )


def _step_image() -> InstallStepStatus:
    """Check whether the configured Docker image is available locally."""
    docker_bin = shutil.which("docker")
    latest = latest_image_build()
    if docker_bin is None:
        return InstallStepStatus(
            key="image",
            status="warning",
            summary="`docker` not on PATH — image cannot be built locally.",
            detail={"latest_build": latest.model_dump() if latest else None},
        )
    if latest and latest.status == BUILD_RUNNING:
        return InstallStepStatus(
            key="image",
            status="running",
            summary=f"docker build in progress ({latest.build_id[:8]}…).",
            detail={"latest_build": latest.model_dump()},
        )
    # Ask doctor whether the image is present.
    result = _doctor._check_docker_image(DOCKER_IMAGE)
    if result.severity == "ok":
        return InstallStepStatus(
            key="image",
            status="done",
            summary=result.message,
            detail={
                "image": DOCKER_IMAGE,
                "latest_build": latest.model_dump() if latest else None,
            },
        )
    return InstallStepStatus(
        key="image",
        status="pending",
        summary=result.message,
        detail={
            "image": DOCKER_IMAGE,
            "hint": result.hint,
            "latest_build": latest.model_dump() if latest else None,
        },
    )


def _step_service() -> InstallStepStatus:
    """Check whether the systemd unit is installed."""
    if _systemd_installed():
        return InstallStepStatus(
            key="service",
            status="done",
            summary="ovid2-scheduler.service is installed.",
        )
    command = get_service_command()
    if shutil.which("systemctl") is None:
        return InstallStepStatus(
            key="service",
            status="warning",
            summary="systemctl not present — no systemd service to install.",
            detail={"command": command.command},
        )
    return InstallStepStatus(
        key="service",
        status="pending",
        summary="Run the sudo command shown to install the systemd unit.",
        detail={"command": command.command, "user": command.user, "bin": command.ovid2_bin},
    )


def _step_pipeline() -> InstallStepStatus:
    """Verify at least one pipeline package resolves under ``ovid2_pipelines``."""
    pipelines = pipelines_meta_svc.discover_pipelines()
    if not pipelines:
        return InstallStepStatus(
            key="pipeline",
            status="pending",
            summary="No ovid2_pipelines.* packages on sys.path yet.",
        )
    return InstallStepStatus(
        key="pipeline",
        status="done",
        summary=f"{len(pipelines)} pipeline package(s) discovered.",
        detail={"pipelines": pipelines[:20]},
    )


def _step_done(steps: list[InstallStepStatus]) -> InstallStepStatus:
    """Overall completion — every prior step must be ``done`` or ``warning``."""
    blocking = [s for s in steps if s.status == "pending"]
    running = [s for s in steps if s.status == "running"]
    if running:
        return InstallStepStatus(
            key="done",
            status="running",
            summary=f"Waiting on: {', '.join(s.key for s in running)}.",
        )
    if blocking:
        return InstallStepStatus(
            key="done",
            status="pending",
            summary=f"Remaining steps: {', '.join(s.key for s in blocking)}.",
        )
    return InstallStepStatus(
        key="done",
        status="done",
        summary="Install checklist complete — head to /submit to run your first job.",
    )


def compute_status() -> InstallStatus:
    """Return the aggregate wizard status derived live from disk.

    The wizard is stateless: every call re-inspects the filesystem, the
    address configs, the secret file, and the docker environment. The
    only persistent state is the ``web_image_builds`` table (long-running
    subprocess bookkeeping) and the ``web_audit`` trail.
    """
    _ensure_image_builds_schema()
    steps: list[InstallStepStatus] = [
        _step_bootstrap(),
        _step_snowflake(),
        _step_addresses(),
        _step_image(),
        _step_service(),
        _step_pipeline(),
    ]
    done_step = _step_done(steps)
    # Determine the "current" step: the first that is not done/warning.
    current_key = "done"
    for step in steps:
        if step.status in ("pending", "running"):
            current_key = step.key
            break
    complete = all(s.status in ("done", "warning") for s in steps)
    return InstallStatus(
        current_step=current_key,
        complete=complete,
        steps=steps + [done_step],
    )
