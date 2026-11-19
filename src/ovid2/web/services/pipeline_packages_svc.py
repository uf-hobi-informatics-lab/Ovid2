"""Pipeline *package* lifecycle service — delete.

Web-tier wrapper over :mod:`ovid2.tools.delete_pipeline`. A package
(``ovid2_pipelines/<name>/``) is what the pipeline navigator and the
``/pipeline-packages`` roster list; a run config (``pipelines_svc``) only
references one. Deleting a package is destructive, so the JSON route gates
it through ``approvals_svc`` (``pipeline_package.delete``); this module
does the work once the caller is authorised.

The package is moved to the XDG trash (recoverable), never ``rmtree``-d.
"""

from __future__ import annotations

import json
import logging

from ovid2.web import db as web_db
from ovid2.web.models import PipelinePackageDeleteResult
from ovid2.web.services import pipelines_svc

logger = logging.getLogger(__name__)

__all__ = ["PackageDeleteError", "PackageInUse", "configs_using", "delete_package"]


class PackageDeleteError(ValueError):
    """Raised when a package cannot be deleted (unknown / protected / unsafe)."""


class PackageInUse(PackageDeleteError):
    """Raised when run configs still reference the package."""

    def __init__(self, name: str, configs: list[str]) -> None:
        self.name = name
        self.configs = list(configs)
        super().__init__(
            f"Pipeline package '{name}' is still used by run config(s): "
            f"{', '.join(self.configs)}. Delete them first, or delete with their configs."
        )


def configs_using(name: str) -> list[str]:
    """Return the names of run configs whose ``pipeline`` is ``name``."""
    return sorted(c.name for c in pipelines_svc.list_pipelines() if c.pipeline == name)


def delete_package(
    name: str, *, with_configs: bool = False, actor: str = "web"
) -> PipelinePackageDeleteResult:
    """Move the package ``name`` to the trash, optionally with its run configs.

    Args:
        name: Package name.
        with_configs: Also delete every run config that references it.
        actor: Audit label.

    Raises:
        PackageInUse: Configs reference it and ``with_configs`` is false.
        PackageDeleteError: Unknown, protected or unsafe package, or a
            referencing config cannot be deleted.
    """
    from ovid2.tools import delete_pipeline as _tool

    configs = configs_using(name)
    if configs and not with_configs:
        raise PackageInUse(name, configs)
    # Validate the package before touching configs, so a refused delete
    # (protected / unknown) never leaves configs half-removed.
    try:
        _tool.validate_deletable(name)
    except _tool.DeletePackageError as exc:
        raise PackageDeleteError(str(exc)) from None

    deleted_configs: list[str] = []
    for cfg in configs:
        try:
            pipelines_svc.delete_pipeline(cfg, actor=actor)
        except pipelines_svc.PipelineError as exc:
            raise PackageDeleteError(
                f"Cannot delete run config '{cfg}' (not in the writable config directory): {exc}"
            ) from None
        deleted_configs.append(cfg)

    try:
        result = _tool.delete_package(name)
    except _tool.DeletePackageError as exc:
        raise PackageDeleteError(str(exc)) from None

    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="pipeline_package.deleted",
        target=name,
        detail_json=json.dumps(
            {"source": str(result.source), "trash": str(result.trash), "configs": deleted_configs}
        ),
    )
    logger.info("Pipeline package %s deleted (trash: %s)", name, result.trash)
    return PipelinePackageDeleteResult(
        name=name,
        path=str(result.source),
        trash=str(result.trash),
        deleted_configs=deleted_configs,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("This module is imported by the ovid2 web routes; it is not a script.")
