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

__all__ = ["ensure_pipeline_packages_on_path", "find_pipeline_packages_dir"]

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


def ensure_pipeline_packages_on_path() -> Path | None:
    """Put the checkout-local pipelines directory on ``sys.path`` (idempotent).

    Returns:
        The directory that is now on ``sys.path``, or ``None`` when none
        was found (the helper is then a no-op).
    """
    directory = find_pipeline_packages_dir()
    if directory is None:
        return None
    entry = str(directory)
    if entry not in sys.path:
        # PEP 420 ``_NamespacePath`` recomputes from ``sys.path`` on
        # access, so an already-imported namespace picks this up too.
        sys.path.insert(0, entry)
    return directory
