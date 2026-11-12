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
4. Rewrites every reference to the template package in the copy
   (``ovid2_pipelines.<template>`` imports and YAML ``class:`` paths,
   legacy ``partner_name`` bindings) so the clone is self-contained.
5. Rewrites the copied ``pipeline_settings.py`` so the new package does
   not inherit the template's *identity* — every constant in
   :data:`ovid2.tools.pipeline_identity.IDENTITY_FIELDS` the template
   declares (HARVEST network / datamart, databases, Datavant key, OMOP
   ``cdm_source``), following ``A = B`` aliases. Operator overrides
   (``identity=``) win over the name-derived defaults.

Usage::

    ovid2 tools new-pipeline --template pcornet_etl --name myhospital
    ovid2 tools new-pipeline --source example_pcornet --name myhospital --cdm pcornet_7_0

``ovid2 tools new-pipeline`` is kept as a hidden, deprecated alias.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from ovid2.tools.pipeline_identity import (
    default_identity,
    read_settings_constants,
    rewrite_settings,
)
from ovid2.tools.templates import get_template, list_templates

logger = logging.getLogger(__name__)

__all__ = ["NewPipelineError", "main", "register", "scaffold_pipeline", "template_references"]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_TEXT_SUFFIXES = (".py", ".yaml", ".yml", ".json", ".md", ".txt")


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


def _rewrite_pipeline_settings(
    text: str,
    new_name: str,
    cdm: str,
    overrides: dict[str, Any] | None = None,
    *,
    source_pipeline: str | None = None,
) -> str:
    """Re-personalise a cloned ``pipeline_settings.py``.

    Every identity constant the template declares (see
    :data:`ovid2.tools.pipeline_identity.IDENTITY_FIELDS`) is rewritten to
    its name-derived default or the supplied override, following
    ``A = B`` aliases to the defining literal; the ``cdm`` binding is
    set (appended when absent). A ``legacy_ovid`` clone additionally
    records ``LEGACY_VENDOR_PARTNER`` so the fix stage keeps finding the
    template's vendored fixer scripts.
    """
    constants = read_settings_constants(text)
    values = default_identity(new_name, constants, cdm=cdm, overrides=overrides)
    has_cdm_line = re.search(r"^cdm\s*=", text, re.MULTILINE) is not None
    if not has_cdm_line and "cdm" not in (overrides or {}):
        values.pop("cdm", None)
    result = rewrite_settings(text, values)
    if (
        source_pipeline
        and constants.get("ETL_PROFILE") == "legacy_ovid"
        and "LEGACY_VENDOR_PARTNER" not in constants
    ):
        if not result.endswith("\n"):
            result += "\n"
        result += (
            "# Cloned from a legacy_ovid port: the vendored fixer scripts stay\n"
            "# under pipelines/legacy_ovid_vendor/partners/<LEGACY_VENDOR_PARTNER>/.\n"
            f"LEGACY_VENDOR_PARTNER = {source_pipeline!r}\n"
        )
    return result


def _rewrite_package_references(dest: Path, source_pipeline: str, new_name: str) -> list[Path]:
    """Point the clone's code at itself instead of the template package.

    Rewrites, in every text file of the copied tree:

    * ``ovid2_pipelines.<source>.`` / ``ovid2_pipelines.<source>`` module
      references (imports, ``class:`` dotted paths in YAML);
    * ``from ovid2_pipelines import <source>`` imports;
    * the generated legacy-port ``partner_name = '<source>'`` binding.

    Returns:
        The files that changed.
    """
    module_re = re.compile(rf"\bovid2_pipelines\.{re.escape(source_pipeline)}\b")
    from_re = re.compile(rf"(\bfrom\s+ovid2_pipelines\s+import\s+){re.escape(source_pipeline)}\b")
    partner_re = re.compile(
        rf"^(\s*partner_name\s*=\s*)(['\"]){re.escape(source_pipeline)}\2", re.MULTILINE
    )
    changed: list[Path] = []
    for path in sorted(dest.rglob("*")):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new = module_re.sub(f"ovid2_pipelines.{new_name}", text)
        new = from_re.sub(rf"\g<1>{new_name}", new)
        new = partner_re.sub(rf"\g<1>\g<2>{new_name}\g<2>", new)
        if new != text:
            path.write_text(new, encoding="utf-8")
            path.chmod(0o644)
            changed.append(path)
    return changed


def template_references(dest: Path, source_pipeline: str) -> list[str]:
    """Return ``file:line`` locations in ``dest`` still naming ``source_pipeline``.

    Used by tests and by the scaffold report to prove a clone is
    self-contained.
    """
    module_re = re.compile(
        rf"\bovid2_pipelines\.{re.escape(source_pipeline)}\b"
        rf"|\bfrom\s+ovid2_pipelines\s+import\s+{re.escape(source_pipeline)}\b"
    )
    hits: list[str] = []
    for path in sorted(dest.rglob("*")):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines, 1):
            if module_re.search(line):
                hits.append(f"{path.relative_to(dest)}:{i}")
    return hits


def scaffold_pipeline(
    new_name: str,
    *,
    source_pipeline: str,
    cdm: str,
    pipelines_root: Path | None = None,
    identity: dict[str, Any] | None = None,
) -> Path:
    """Copy ``source_pipeline`` to a new package named ``new_name``.

    Args:
        new_name: Package name for the new pipeline (validated against
            :data:`_NAME_RE`).
        source_pipeline: Existing pipeline package to clone.
        cdm: Target CDM name recorded in the copied ``pipeline_settings``.
        pipelines_root: Override the destination root (used by tests).
            Defaults to the first writable ``ovid2_pipelines`` dir.
        identity: Operator-chosen identity values (``DATAMARTID``,
            ``DATAVANT_KEY``, …) overriding the name-derived defaults.

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

    if not new_name.isidentifier():
        raise NewPipelineError(
            f"Invalid pipeline name {new_name!r}: it must be a valid Python identifier "
            "(letters, digits, underscore; not starting with a digit) because the "
            "package is imported as ovid2_pipelines.<name>."
        )
    settings_src = src / "pipeline_settings.py"
    if settings_src.is_file():
        try:
            original = settings_src.read_text(encoding="utf-8")
            rewritten = _rewrite_pipeline_settings(
                original, new_name, cdm, identity, source_pipeline=source_pipeline
            )
        except SyntaxError as exc:
            raise NewPipelineError(
                f"Template '{source_pipeline}' pipeline_settings.py does not parse: {exc}"
            ) from None
    else:
        rewritten = None

    shutil.copytree(
        src,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    _rewrite_package_references(dest, source_pipeline, new_name)
    if rewritten is not None:
        settings_path = dest / "pipeline_settings.py"
        settings_path.write_text(rewritten, encoding="utf-8")
        settings_path.chmod(0o644)
    for path in dest.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    dest.chmod(0o755)

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
