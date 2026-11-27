"""Workspaces — the tenant boundary (tier-neutral, stdlib only).

A *workspace* is a named collaboration space. Its members see and share the
same saved pipelines, workflows, jobs, runs, reports, connections,
addresses, secrets, engines, LLM connections, transform library and
workspace pipeline packages. What a member may *do* there is still decided
by their global role (``system_admin`` / ``pipeline_operator`` / ``viewer``
/ ``guest``); membership only decides what they can *see*. ADR:
the adr-workspaces design note.

This module owns the two things every tier needs:

* **The active workspace.** :func:`active_workspace` answers "which
  workspace is this code running for?" — a request-scoped
  :class:`contextvars.ContextVar` set by the web middleware (or by the
  worker per job), else the ``OVID2_WORKSPACE`` environment variable (set
  on every executor subprocess / container), else :data:`DEFAULT_WORKSPACE`.
* **Where a workspace lives on disk.** The ``default`` workspace *is* the
  historical flat layout (``$XDG_CONFIG_HOME/ovid2/connections`` …), so an
  upgrade moves no files. Every other workspace is a self-contained tree::

      $XDG_CONFIG_HOME/ovid2/workspaces/<slug>/
          connections/ configs/ engines/ llms/ transforms/
          pipelines/ workflows/ secrets.d/ packages/ovid2_pipelines/

Global resources (``settings.json``, the ``auth_*`` secrets, shipped CDMs,
packs, health checks, built-in transforms and the shared
``pipelines/ovid2_pipelines`` tree) are not workspace-scoped.

Safe to import from ``ovid2.web`` / ``ovid2.mcp`` (stdlib only).
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import re
from collections.abc import Iterator
from pathlib import Path

from ovid2.config import paths as _paths

__all__ = [
    "DEFAULT_WORKSPACE",
    "NO_WORKSPACE",
    "WORKSPACES_SUBDIR",
    "WORKSPACE_ENV",
    "WORKSPACE_HEADER",
    "WorkspaceSlugError",
    "active_workspace",
    "config_root",
    "is_default",
    "is_global_secret",
    "kind_dir",
    "list_workspace_dirs",
    "materialise",
    "packages_dir",
    "reset_active",
    "search_dirs",
    "secret_dirs",
    "secrets_dir",
    "set_active",
    "shared_executor_secret_files",
    "shared_secret_dirs",
    "use_workspace",
    "validate_slug",
    "workspaces_root",
    "write_dir",
]

#: Slug of the workspace every existing resource and user is migrated into.
DEFAULT_WORKSPACE: str = "default"

#: Sentinel for "the caller belongs to no workspace". It is not a valid slug,
#: resolves to a directory that never exists and matches no DB row, so code
#: that forgets to check still fails *closed* (sees nothing).
NO_WORKSPACE: str = "__none__"

#: Environment variable carrying the active workspace to executor processes.
WORKSPACE_ENV: str = "OVID2_WORKSPACE"

#: HTTP header an API / MCP caller uses to pick a workspace per request.
WORKSPACE_HEADER: str = "X-Ovid2-Workspace"

#: Sub-directory of the config root holding the non-default workspaces.
WORKSPACES_SUBDIR: str = "workspaces"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")
_RESERVED: frozenset[str] = frozenset({"new", "switch", "none", "all", "api", "static"})

_ACTIVE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ovid2_active_workspace", default=None
)


class WorkspaceSlugError(ValueError):
    """Raised for a workspace slug that is malformed or reserved."""


def validate_slug(slug: str) -> str:
    """Return ``slug`` when it is a valid workspace slug, else raise.

    A slug is 2–40 characters of lowercase letters, digits, ``-`` and
    ``_``, starting with a letter or digit; it is used as a directory name
    so it can never contain a path separator.

    Raises:
        WorkspaceSlugError: When ``slug`` is malformed or reserved.
    """
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise WorkspaceSlugError(
            "Workspace slug must be 2-40 lowercase letters, digits, '-' or '_' "
            "and start with a letter or digit."
        )
    if slug in _RESERVED or slug == NO_WORKSPACE:
        raise WorkspaceSlugError(f"Workspace slug '{slug}' is reserved.")
    return slug


def _is_safe(slug: str) -> bool:
    return slug == DEFAULT_WORKSPACE or bool(_SLUG_RE.match(slug))


def active_workspace() -> str:
    """Return the slug of the workspace the current code runs for.

    Resolution: the request / job context var, then ``$OVID2_WORKSPACE``,
    then :data:`DEFAULT_WORKSPACE`. A malformed env value resolves to
    :data:`NO_WORKSPACE` (fail closed) rather than to ``default``.
    """
    value = _ACTIVE.get()
    if value:
        return value
    env = os.environ.get(WORKSPACE_ENV, "").strip()
    if not env:
        return DEFAULT_WORKSPACE
    return env if _is_safe(env) else NO_WORKSPACE


def set_active(slug: str) -> contextvars.Token[str | None]:
    """Set the active workspace for the current context; return a reset token."""
    return _ACTIVE.set(slug)


def reset_active(token: contextvars.Token[str | None]) -> None:
    """Undo a :func:`set_active` call."""
    _ACTIVE.reset(token)


@contextlib.contextmanager
def use_workspace(slug: str) -> Iterator[str]:
    """Run a block with ``slug`` as the active workspace (worker / tests)."""
    token = _ACTIVE.set(slug)
    try:
        yield slug
    finally:
        _ACTIVE.reset(token)


def is_default(slug: str | None = None) -> bool:
    """Return ``True`` when ``slug`` (default: the active one) is ``default``."""
    return (slug if slug is not None else active_workspace()) == DEFAULT_WORKSPACE


def workspaces_root() -> Path:
    """Return ``$XDG_CONFIG_HOME/ovid2/workspaces`` (non-default workspaces)."""
    return _paths.config_dir() / WORKSPACES_SUBDIR


def config_root(slug: str | None = None) -> Path:
    """Return the config root of workspace ``slug`` (default: the active one).

    ``default`` maps to the historical flat ``$XDG_CONFIG_HOME/ovid2``; any
    other slug to ``…/ovid2/workspaces/<slug>``. :data:`NO_WORKSPACE` maps
    to a path that is never created.
    """
    ws = slug if slug is not None else active_workspace()
    if ws == DEFAULT_WORKSPACE:
        return _paths.config_dir()
    if ws == NO_WORKSPACE or not _is_safe(ws):
        return workspaces_root() / ".none"
    return workspaces_root() / ws


def kind_dir(kind: str, slug: str | None = None) -> Path:
    """Return the directory holding one resource kind for a workspace.

    Args:
        kind: Sub-directory name (``connections``, ``configs``, ``engines``,
            ``llms``, ``transforms``, ``pipelines``, ``workflows``).
        slug: Workspace slug; the active workspace when ``None``.
    """
    return config_root(slug) / kind


def secrets_dir(slug: str | None = None) -> Path:
    """Return the ``secrets.d`` directory of a workspace."""
    return config_root(slug) / "secrets.d"


def packages_dir(slug: str | None = None) -> Path | None:
    """Return the directory that *contains* a workspace's ``ovid2_pipelines/``.

    ``None`` for the ``default`` workspace, whose packages live in the shared
    ``pipelines/ovid2_pipelines`` tree (see
    :mod:`ovid2.config.pipeline_packages`).
    """
    ws = slug if slug is not None else active_workspace()
    if ws == DEFAULT_WORKSPACE:
        return None
    return config_root(ws) / "packages"


def list_workspace_dirs() -> list[str]:
    """Return the slugs of every non-default workspace present on disk."""
    root = workspaces_root()
    if not root.is_dir():
        return []
    return sorted(
        entry.name for entry in root.iterdir() if entry.is_dir() and _SLUG_RE.match(entry.name)
    )


def materialise(slug: str, kinds: tuple[str, ...]) -> Path:
    """Create a non-default workspace's tree (dirs 0o755, ``secrets.d`` 0o700)."""
    root = config_root(slug)
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    for kind in kinds:
        (root / kind).mkdir(exist_ok=True, mode=0o755)
    private = secrets_dir(slug)
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    packages = packages_dir(slug)
    if packages is not None:
        (packages / "ovid2_pipelines").mkdir(parents=True, exist_ok=True, mode=0o755)
    return root


# ---------------------------------------------------------------------------
# Resolver helpers (one implementation for every config kind)
# ---------------------------------------------------------------------------


def search_dirs(
    kind: str, override_env: str | None = None, *, include_cwd: bool = True
) -> list[Path]:
    """Return the ordered directories a resolver searches for ``kind`` files.

    * ``default`` workspace — the historical order: ``$<override_env>``,
      ``$XDG_CONFIG_HOME/ovid2/<kind>``, ``./<kind>`` (when
      ``include_cwd``), ``/etc/ovid2/<kind>``.
    * any other workspace — its own ``…/workspaces/<slug>/<kind>`` then the
      site-wide, read-only ``/etc/ovid2/<kind>``. The default tree and the
      current directory are **not** searched, so another workspace's names
      never resolve.
    """
    ws = active_workspace()
    system = _paths.system_config_dir() / kind
    if ws != DEFAULT_WORKSPACE:
        return [kind_dir(kind, ws), system]
    found: list[Path] = []
    override = os.environ.get(override_env) if override_env else None
    if override:
        found.append(Path(override))
    found.append(_paths.config_dir() / kind)
    if include_cwd:
        found.append(Path.cwd() / kind)
    found.append(system)
    return found


def write_dir(kind: str, override_env: str | None = None) -> Path:
    """Return (without creating) the directory new ``kind`` files are written to."""
    ws = active_workspace()
    if ws != DEFAULT_WORKSPACE:
        return kind_dir(kind, ws)
    override = os.environ.get(override_env) if override_env else None
    return Path(override) if override else _paths.config_dir() / kind


#: ``secrets.d`` stems / file names that are *site-wide*, never per-workspace.
#: ``auth_*`` (sign-in, session key, break-glass, service tokens) are read by
#: the web tier only and are never granted to an executor.
GLOBAL_SECRET_PREFIXES: tuple[str, ...] = ("auth_",)


def shared_executor_secret_files() -> frozenset[str]:
    """Secret *file names* every workspace's executor may be granted (the XOR seed)."""
    return frozenset({os.environ.get("OVID2_SEED_FILE_NAME", "xor_seed.txt")})


def is_global_secret(file_name: str) -> bool:
    """Return ``True`` for a site-wide secret (``auth_*``) — web tier only."""
    return file_name.startswith(GLOBAL_SECRET_PREFIXES)


def secret_dirs(slug: str | None = None) -> list[Path]:
    """Return the ``secrets.d`` directories owned by a workspace, in lookup order."""
    ws = slug if slug is not None else active_workspace()
    if ws == DEFAULT_WORKSPACE:
        return [_paths.secrets_dir(), _paths.system_config_dir() / "secrets.d"]
    return [secrets_dir(ws)]


def shared_secret_dirs() -> list[Path]:
    """Return the site-wide ``secrets.d`` directories (global / shared files)."""
    return [_paths.secrets_dir(), _paths.system_config_dir() / "secrets.d"]


if __name__ == "__main__":
    raise SystemExit("ovid2.workspaces is a library module; not runnable directly.")
