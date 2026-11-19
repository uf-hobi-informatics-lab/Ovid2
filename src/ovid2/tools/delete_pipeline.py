"""``ovid2 pipeline delete`` — remove a pipeline *package* (recoverably).

A pipeline package is the code folder ``ovid2_pipelines/<name>/``
(formatters, mappers, fixer chains, settings). It is distinct from a
pipeline *run config* (``$XDG_CONFIG_HOME/ovid2/pipelines/<name>.json``),
which only binds a package to storage and a trigger. Deleting a run config
leaves the package — and therefore the pipeline navigator entry — in place;
this module deletes the package itself.

The package is **moved** to ``$XDG_STATE_HOME/ovid2/trash/pipelines/
<name>-<timestamp>/`` rather than removed, so a mistaken delete is
recoverable with a plain ``mv``. Guards:

* the name must be a discovered ``ovid2_pipelines`` package and its
  resolved directory must sit directly inside a namespace directory
  (no traversal, no symlinked package);
* curated template sources (:data:`ovid2.tools.templates.TEMPLATES`)
  are refused — every "new pipeline" clone starts from them;
* run configs that still reference the package are refused unless the
  caller deletes them first (``referenced_by`` is supplied by the caller,
  so this module stays free of the web tier).

After the move, ``ovid2_pipelines.<name>`` and its sub-modules are purged
from ``sys.modules`` and :func:`importlib.invalidate_caches` is called so
the running process (e.g. the web server) never serves the deleted package.

Tier-neutral: imports neither ``ovid2.core`` nor ``ovid2.web``.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ovid2.config.paths import state_dir

logger = logging.getLogger(__name__)

__all__ = [
    "DeletePackageError",
    "DeletedPackage",
    "PackageInUseError",
    "delete_package",
    "protected_packages",
    "purge_package_modules",
    "trash_dir",
    "validate_deletable",
]

_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class DeletePackageError(ValueError):
    """Raised when a package cannot be deleted (unknown, protected, unsafe)."""


class PackageInUseError(DeletePackageError):
    """Raised when run configs still reference the package."""

    def __init__(self, name: str, configs: list[str]) -> None:
        self.name = name
        self.configs = list(configs)
        super().__init__(
            f"Pipeline package '{name}' is still used by run config(s): "
            f"{', '.join(self.configs)}. Delete those first (or pass --with-configs)."
        )


@dataclass(frozen=True)
class DeletedPackage:
    """Result of a package delete.

    Attributes:
        name: The package name.
        source: The directory the package lived in.
        trash: Where it was moved to (restore with ``mv trash source``).
    """

    name: str
    source: Path
    trash: Path


def trash_dir() -> Path:
    """Return ``$XDG_STATE_HOME/ovid2/trash/pipelines``."""
    return state_dir() / "trash" / "pipelines"


def protected_packages() -> frozenset[str]:
    """Return package names that may never be deleted (template sources)."""
    from ovid2.tools.templates import TEMPLATES

    return frozenset(t.source_pipeline for t in TEMPLATES.values())


def _namespace_dirs() -> list[Path]:
    """Return every on-disk directory contributing to ``ovid2_pipelines``."""
    try:
        pkg = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError:
        return []
    return [Path(p).resolve() for p in getattr(pkg, "__path__", []) if Path(p).is_dir()]


def _locate(name: str) -> Path:
    """Return the package directory for ``name`` after safety checks."""
    if not _NAME_RE.match(name or ""):
        raise DeletePackageError(f"Invalid pipeline package name '{name}'.")
    roots = _namespace_dirs()
    known = {m.name for m in pkgutil.iter_modules([str(r) for r in roots]) if m.ispkg}
    if name not in known:
        raise DeletePackageError(f"Pipeline package '{name}' not found.")
    for root in roots:
        candidate = root / name
        if candidate.is_symlink():
            raise DeletePackageError(
                f"Pipeline package '{name}' is a symlink ({candidate}); remove it by hand."
            )
        if candidate.is_dir() and candidate.resolve().parent == root:
            return candidate
    raise DeletePackageError(f"Pipeline package '{name}' has no on-disk directory.")


def validate_deletable(name: str) -> Path:
    """Return the package directory if ``name`` may be deleted.

    Raises:
        DeletePackageError: Unknown, protected (template source) or unsafe.
    """
    source = _locate(name)
    if name in protected_packages():
        raise DeletePackageError(
            f"Pipeline package '{name}' is the source of a pipeline template and cannot be deleted."
        )
    return source


def purge_package_modules(name: str) -> int:
    """Drop ``ovid2_pipelines.<name>`` (and sub-modules) from ``sys.modules``.

    Returns:
        The number of modules removed.
    """
    prefix = f"ovid2_pipelines.{name}"
    doomed = [m for m in sys.modules if m == prefix or m.startswith(prefix + ".")]
    for mod in doomed:
        sys.modules.pop(mod, None)
    importlib.invalidate_caches()
    return len(doomed)


def delete_package(name: str, *, referenced_by: list[str] | None = None) -> DeletedPackage:
    """Move the pipeline package ``name`` to the trash.

    Args:
        name: Package name (``ovid2_pipelines.<name>``).
        referenced_by: Names of run configs that still use the package.
            A non-empty list refuses the delete.

    Returns:
        A :class:`DeletedPackage` naming the source and trash directories.

    Raises:
        PackageInUseError: ``referenced_by`` is non-empty.
        DeletePackageError: Unknown, protected or unsafe package.
    """
    source = validate_deletable(name)
    if referenced_by:
        raise PackageInUseError(name, referenced_by)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    dest_root = trash_dir()
    dest_root.mkdir(parents=True, exist_ok=True, mode=0o755)
    dest = dest_root / f"{name}-{stamp}"
    shutil.move(str(source), str(dest))
    purge_package_modules(name)
    logger.info("Pipeline package %s moved to %s", name, dest)
    return DeletedPackage(name=name, source=source, trash=dest)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Use `ovid2 pipeline delete <name>`.")
