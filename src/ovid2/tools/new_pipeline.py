"""``ovid2 tools new-pipeline`` — scaffold a new pipeline package.

Creating a pipeline of an ETL type copies the *template* of that type
(an existing pipeline package) under a user-given name, then lets the
operator personalise the dictionaries, formatters, and fixer chains.
This module implements the copy.

The scaffolder:

1. Resolves the template → its ``source_pipeline`` and target ``cdm``
   via :mod:`ovid2.tools.templates`.
2. Locates the source package on disk (through the
   ``ovid2_pipelines`` namespace ``__path__``).
3. Copies the whole tree to ``<pipelines_root>/<new_name>/``, refusing
   to overwrite an existing package.
4. Rewrites the copied ``pipeline_settings.py`` so the new package does
   not inherit the template's *identity* (DB name, HARVEST datamart
   id/name, DATAVANT key). The target ``cdm`` binding is preserved (or
   added) so ``resolve_cdm_for_pipeline`` returns the right handle.

Usage::

    ovid2 tools new-pipeline --template pcornet_etl --name myhospital
    ovid2 tools new-pipeline --source example_pcornet --name myhospital --cdm pcornet_7_0

``ovid2 tools new-pipeline`` is kept as a hidden, deprecated alias.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from ovid2.tools.templates import get_template, list_templates

logger = logging.getLogger(__name__)

__all__ = ["NewPipelineError", "main", "register", "scaffold_pipeline"]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_IDENTITY_PLACEHOLDERS = (
    "DEFAULT_PIPELINE_DB_NAME",
    "DATAMARTID",
    "DATAMART_NAME",
    "DATAVANT_KEY",
    "MERGE_DATABASE_NAME",
    "CDM_SOURCE_NAME",
    "CDM_SOURCE_ABBREVIATION",
)


class NewPipelineError(ValueError):
    """Raised when a scaffold request cannot be satisfied."""


def _pipelines_root() -> Path:
    """Return the first writable ``ovid2_pipelines`` namespace directory."""
    try:
        pkg = importlib.import_module("ovid2_pipelines")
    except ModuleNotFoundError as exc:  # pragma: no cover — defensive
        raise NewPipelineError(
            "The 'ovid2_pipelines' namespace package is not importable."
        ) from exc
    candidates = [Path(p) for p in getattr(pkg, "__path__", [])]
    for base in candidates:
        if base.is_dir():
            return base
    raise NewPipelineError("No on-disk 'ovid2_pipelines' directory found on sys.path.")


def _source_pipeline_path(source_pipeline: str) -> Path:
    """Return the on-disk directory for an existing pipeline package."""
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{source_pipeline}")
    except ModuleNotFoundError as exc:
        raise NewPipelineError(f"Source pipeline '{source_pipeline}' is not installed.") from exc
    for base in getattr(pkg, "__path__", []):
        candidate = Path(base)
        if candidate.is_dir():
            return candidate
    raise NewPipelineError(f"Source pipeline '{source_pipeline}' has no on-disk directory.")


def _network_prefix(text: str) -> str:
    """Return the datamart-id prefix for scaffolded pipelines.

    Uses ``$OVID2_NETWORK_ID`` when set, else the template's own
    ``NETWORKID`` constant, else ``NET``.
    """
    env = os.environ.get("OVID2_NETWORK_ID")
    if env:
        return env
    match = re.search(r"^NETWORKID\s*=\s*(['\"])(.*?)\1", text, re.MULTILINE)
    return match.group(2) if match and match.group(2) else "NET"


def _placeholder_value(constant: str, new_name: str, network_id: str = "NET") -> str:
    """Return the placeholder string for an identity ``constant``."""
    upper = new_name.upper().replace("-", "_").replace(".", "_")
    if constant in ("DEFAULT_PIPELINE_DB_NAME", "MERGE_DATABASE_NAME"):
        return f"CHAR_{upper}"
    if constant == "DATAMARTID":
        return f"{network_id}{upper}"
    if constant == "DATAMART_NAME":
        return new_name
    if constant == "DATAVANT_KEY":
        return new_name.lower()
    if constant == "CDM_SOURCE_NAME":
        return f"{new_name} PCORnet-to-OMOP conversion"
    if constant == "CDM_SOURCE_ABBREVIATION":
        return upper
    return new_name


def _rewrite_pipeline_settings(text: str, new_name: str, cdm: str) -> str:
    """Rewrite identity constants + the ``cdm`` binding in-place."""
    result = text
    network_id = _network_prefix(text)
    for constant in _IDENTITY_PLACEHOLDERS:
        placeholder = _placeholder_value(constant, new_name, network_id)
        pattern = re.compile(
            rf"(^{re.escape(constant)}\s*=\s*)(['\"])(.*?)(\2)",
            re.MULTILINE,
        )

        def _swap(m: re.Match[str], ph: str = placeholder) -> str:
            return f"{m.group(1)}{m.group(2)}{ph}{m.group(2)}"

        result = pattern.sub(_swap, result)
    cdm_pattern = re.compile(r"(^cdm\s*=\s*)(['\"])(.*?)(\2)", re.MULTILINE)
    if cdm_pattern.search(result):
        result = cdm_pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{cdm}{m.group(2)}", result)
    return result


def scaffold_pipeline(
    new_name: str,
    *,
    source_pipeline: str,
    cdm: str,
    pipelines_root: Path | None = None,
) -> Path:
    """Copy ``source_pipeline`` to a new package named ``new_name``.

    Args:
        new_name: Package name for the new pipeline (validated against
            :data:`_NAME_RE`).
        source_pipeline: Existing pipeline package to clone.
        cdm: Target CDM name recorded in the copied ``pipeline_settings``.
        pipelines_root: Override the destination root (used by tests).
            Defaults to the first writable ``ovid2_pipelines`` dir.

    Returns:
        The path to the newly created package directory.

    Raises:
        NewPipelineError: On an invalid name, missing source, or a
            destination that already exists.
    """
    if not new_name or not _NAME_RE.match(new_name):
        raise NewPipelineError(
            f"Invalid pipeline name {new_name!r}. Allowed: letters, "
            "digits, dot, underscore, hyphen."
        )
    if new_name == source_pipeline:
        raise NewPipelineError(
            f"New pipeline name must differ from the source '{source_pipeline}'."
        )
    src = _source_pipeline_path(source_pipeline)
    root = pipelines_root or _pipelines_root()
    dest = root / new_name
    if dest.exists():
        raise NewPipelineError(f"Pipeline package '{new_name}' already exists at {dest}.")

    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    settings_path = dest / "pipeline_settings.py"
    if settings_path.is_file():
        original = settings_path.read_text(encoding="utf-8")
        rewritten = _rewrite_pipeline_settings(original, new_name, cdm)
        settings_path.write_text(rewritten, encoding="utf-8")
        settings_path.chmod(0o644)

    logger.info(
        "Scaffolded pipeline '%s' from '%s' (cdm=%s) at %s",
        new_name,
        source_pipeline,
        cdm,
        dest,
    )
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_scaffold_args(parser: argparse.ArgumentParser, template_ids: str) -> None:
    """Attach the shared scaffold arguments to ``parser``."""
    parser.add_argument(
        "--name",
        required=True,
        help="Name for the new pipeline package.",
    )
    parser.add_argument(
        "--template",
        default=None,
        help=f"Pipeline template id (one of: {template_ids}).",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Explicit source pipeline to clone (used with --cdm; an alternative to --template).",
    )
    parser.add_argument(
        "--cdm",
        default=None,
        help="Target CDM name (required with --source; taken from the template otherwise).",
    )


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``new-pipeline`` plus the hidden ``new-partner`` alias.

    The alias must keep a distinct name — Python 3.11+ ``argparse`` raises
    ``conflicting subparser`` on duplicates.
    """
    template_ids = ", ".join(t.id for t in list_templates())
    parser = subparsers.add_parser(
        "new-pipeline",
        help="Scaffold a new pipeline package from a template.",
        description=(
            "Copy an existing pipeline package (or a curated pipeline "
            "template) under a new name so it can be personalised. "
            f"Known templates: {template_ids}."
        ),
    )
    _add_scaffold_args(parser, template_ids)
    parser.set_defaults(_entry=main, _tool="new-pipeline")

    # Deprecated alias: ``ovid2 tools new-partner`` for one release.
    # Omitting ``help=`` keeps it out of the tool listing.
    alias = subparsers.add_parser("new-partner")
    _add_scaffold_args(alias, template_ids)
    alias.set_defaults(_entry=main, _tool="new-partner")


def main(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools new-pipeline``."""
    template_id = getattr(args, "template", None)
    source = getattr(args, "source", None)
    cdm = getattr(args, "cdm", None)

    if template_id:
        template = get_template(template_id)
        if template is None:
            known = ", ".join(t.id for t in list_templates())
            print(
                f"error: unknown template '{template_id}'. Known: {known}.",
                file=sys.stderr,
            )
            return 2
        source = template.source_pipeline
        cdm = template.cdm
    elif source and cdm:
        pass
    else:
        print(
            "error: supply either --template <id> or both --source <pipeline> and --cdm <name>.",
            file=sys.stderr,
        )
        return 2

    try:
        dest = scaffold_pipeline(args.name, source_pipeline=source, cdm=cdm)
    except NewPipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Created pipeline '{args.name}' at {dest}")
    print(f"  cloned from : {source}")
    print(f"  target CDM  : {cdm}")
    print("Next: personalise dictionaries.py, formatters/, and fixer_configs.py.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        "This module is invoked via 'ovid2 tools new-pipeline'; it is not a standalone script."
    )
