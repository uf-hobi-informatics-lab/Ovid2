"""Locate the ``ovid2_pipelines`` PEP 420 namespace for a source checkout.

Pipeline packages (``pipelines/ovid2_pipelines/<name>/``) are not part of
the ``ovid2`` wheel. Production installs ship them as separately
installed distributions; a developer / host install running from the
repo needs ``<repo>/pipelines`` on ``sys.path`` so that
``import ovid2_pipelines.<name>`` resolves.

Both the web app factory and the CLI entry point call
:func:`ensure_pipeline_packages_on_path` so ``ovid2 pipeline list``,
``ovid2 etl --local`` and ``/pipeline-packages`` all see the same set of
packages. Containers get the same tree via the ``/app/pipelines`` bind
mount plus ``PYTHONPATH`` (see ``core/docker.py``).

Stdlib-only and tier-neutral: safe to import from ``ovid2.web`` /
``ovid2.mcp``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "WORKSPACE_PACKAGES_ENV",
    "all_workspace_package_dirs",
    "ensure_all_workspace_packages_on_path",
    "ensure_pipeline_packages_on_path",
    "ensure_workspace_packages_on_path",
    "find_pipeline_packages_dir",
    "package_dirs",
    "package_visible",
    "package_workspace",
    "workspace_packages_dir",
]

_ENV_OVERRIDE = "OVID2_PIPELINE_PACKAGES_DIR"
_LEGACY_ENV_OVERRIDE = "OVID2_PIPELINES_DIR"
_NAMESPACE = "ovid2_pipelines"


def find_pipeline_packages_dir() -> Path | None:
    """Return the directory that *contains* ``ovid2_pipelines/``, if any.

    Resolution order:

    1. ``$OVID2_PIPELINE_PACKAGES_DIR`` (directory containing
       ``ovid2_pipelines/``).
    2. ``$OVID2_PIPELINES_DIR`` — honoured only when it actually contains
       an ``ovid2_pipelines/`` subdirectory. (The same variable also names
       the saved-pipeline-config JSON directory in
       ``web/services/pipelines_svc.py``; a JSON-only directory is ignored
       here.)
    3. Walk up from the installed :mod:`ovid2` source to the first parent
       holding ``pipelines/ovid2_pipelines/`` (editable / repo checkout).

    Returns:
        The directory to put on ``sys.path``, or ``None`` when no
        checkout-local pipelines tree is found (pure wheel install).
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        candidate = Path(override)
        return candidate if candidate.is_dir() else None
    legacy = os.environ.get(_LEGACY_ENV_OVERRIDE)
    if legacy and (Path(legacy) / _NAMESPACE).is_dir():
        return Path(legacy)
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pipelines"
        if (candidate / _NAMESPACE).is_dir():
            return candidate
    return None


#: Directory containing a workspace's own ``ovid2_pipelines/`` (set by the
#: web worker on a job of a non-default workspace; adr-workspaces.md).
WORKSPACE_PACKAGES_ENV = "OVID2_WORKSPACE_PACKAGES_DIR"


def workspace_packages_dir() -> Path | None:
    """Return the active workspace's package root (contains ``ovid2_pipelines/``).

    ``$OVID2_WORKSPACE_PACKAGES_DIR`` wins (executor processes); otherwise
    the active workspace's ``packages/`` directory. ``None`` for the
    ``default`` workspace or when the directory does not exist.
    """
    override = os.environ.get(WORKSPACE_PACKAGES_ENV)
    if override:
        candidate = Path(override)
        return candidate if (candidate / _NAMESPACE).is_dir() else None
    from ovid2.workspaces import packages_dir

    candidate_dir = packages_dir()
    if candidate_dir is None or not (candidate_dir / _NAMESPACE).is_dir():
        return None
    return candidate_dir


def ensure_workspace_packages_on_path(directory: Path | None = None) -> Path | None:
    """Put a workspace's package root on ``sys.path`` (idempotent).

    Workspace package names are unique across the deployment (enforced at
    scaffold time), so adding a root never shadows another workspace's
    package — it only makes this workspace's packages importable.
    """
    target = directory if directory is not None else workspace_packages_dir()
    if target is None:
        return None
    entry = str(target)
    if entry not in sys.path:
        sys.path.append(entry)
    return target


def all_workspace_package_dirs() -> list[Path]:
    """Return every workspace package root that exists on disk."""
    from ovid2.workspaces import list_workspace_dirs, packages_dir

    out: list[Path] = []
    for slug in list_workspace_dirs():
        candidate = packages_dir(slug)
        if candidate is not None and (candidate / _NAMESPACE).is_dir():
            out.append(candidate)
    return out


def ensure_all_workspace_packages_on_path() -> list[Path]:
    """Put *every* workspace's package root on ``sys.path`` (web tier start-up).

    The long-lived web process serves every workspace, so it needs each
    root importable; :func:`package_visible` then hides other workspaces'
    packages per request.
    """
    roots = all_workspace_package_dirs()
    for root in roots:
        ensure_workspace_packages_on_path(root)
    return roots


def _workspace_of_dir(directory: Path) -> str | None:
    """Return the workspace slug owning a package directory, or ``None`` (shared)."""
    from ovid2.workspaces import workspaces_root

    try:
        rel = directory.resolve().relative_to(workspaces_root().resolve())
    except (ValueError, OSError):
        return None
    return rel.parts[0] if rel.parts else None


def package_dirs(name: str) -> list[Path]:
    """Return every on-disk directory of package ``ovid2_pipelines.<name>``."""
    found: list[Path] = []
    for entry in sys.path:
        candidate = Path(entry or ".") / _NAMESPACE / name
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return found


def package_workspace(name: str) -> str | None:
    """Return the workspace owning package ``name`` (``None`` = shared / unknown)."""
    for directory in package_dirs(name):
        owner = _workspace_of_dir(directory)
        if owner is not None:
            return owner
    return None


def package_visible(name: str) -> bool:
    """Return ``True`` when package ``name`` is visible in the active workspace.

    Shared packages (the checkout / installed ``ovid2_pipelines`` tree) are
    visible everywhere; a workspace package only inside its own workspace.
    """
    from ovid2.workspaces import active_workspace

    owner = package_workspace(name)
    return owner is None or owner == active_workspace()


def ensure_pipeline_packages_on_path() -> Path | None:
    """Put the checkout-local pipelines directory on ``sys.path`` (idempotent).

    Also adds the active workspace's package root
    (:func:`ensure_workspace_packages_on_path`) when one is configured.

    Returns:
        The directory that is now on ``sys.path``, or ``None`` when none
        was found (the helper is then a no-op).
    """
    ensure_workspace_packages_on_path()
    directory = find_pipeline_packages_dir()
    if directory is None:
        return None
    entry = str(directory)
    if entry not in sys.path:
        # PEP 420 ``_NamespacePath`` recomputes from ``sys.path`` on
        # access, so an already-imported namespace picks this up too.
        sys.path.insert(0, entry)
    return directory
