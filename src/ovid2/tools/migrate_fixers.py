"""``ovid2 tools migrate-fixers`` — convert ``fixer_configs.py`` to YAML.

Writes ``<pipeline pkg>/fixer_configs.yaml`` from the ``FIXER_CHAINS`` /
``MULTI_TABLE_FIXES`` of the pipeline's ``fixer_configs.py`` so the chains
become editable in the web pipeline builder. The YAML takes precedence at
run time; the ``.py`` is kept (renamed to ``fixer_configs.py.bak`` with
``--remove-python``) so the conversion is reversible.

Usage::

    ovid2 tools migrate-fixers --pipeline myhospital
    ovid2 tools migrate-fixers --pipeline myhospital --remove-python
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

from ovid2.config.fixer_configs import (
    FIXER_YAML,
    dump_fixer_yaml,
    fixer_yaml_path,
    load_fixer_configs,
    validate_fixer_document,
)

__all__ = ["MigrateFixersError", "main", "migrate_fixers", "register"]


class MigrateFixersError(ValueError):
    """Raised when a pipeline's fixers cannot be converted."""


def _package_dir(pipeline: str) -> Path:
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError as exc:
        raise MigrateFixersError(f"Pipeline '{pipeline}' is not installed.") from exc
    for base in getattr(pkg, "__path__", []) or []:
        if Path(base).is_dir():
            return Path(base)
    raise MigrateFixersError(f"Pipeline '{pipeline}' has no on-disk directory.")


def migrate_fixers(pipeline: str, *, remove_python: bool = False) -> Path:
    """Write ``fixer_configs.yaml`` for ``pipeline`` and return its path.

    Raises:
        MigrateFixersError: When a YAML already exists, the pipeline has no
            Python fixer configs, or the converted chains fail validation.
    """
    if fixer_yaml_path(pipeline) is not None:
        raise MigrateFixersError(f"Pipeline '{pipeline}' already has {FIXER_YAML}.")
    chains, multi, source = load_fixer_configs(pipeline)
    if source != "python":
        raise MigrateFixersError(f"Pipeline '{pipeline}' has no fixer_configs.py to convert.")
    text = dump_fixer_yaml(
        chains, multi, description=f"Fix chains of {pipeline} (converted from fixer_configs.py)."
    )
    import yaml

    errors = validate_fixer_document(yaml.safe_load(text))
    if errors:
        raise MigrateFixersError("converted chains do not validate: " + "; ".join(errors))
    dest = _package_dir(pipeline) / FIXER_YAML
    dest.write_text(text, encoding="utf-8")
    dest.chmod(0o644)
    if remove_python:
        py = dest.with_name("fixer_configs.py")
        if py.is_file():
            py.rename(py.with_name("fixer_configs.py.bak"))
    return dest


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register ``ovid2 tools migrate-fixers``."""
    parser = subparsers.add_parser(
        "migrate-fixers",
        help="Convert a pipeline's fixer_configs.py into editable fixer_configs.yaml.",
    )
    parser.add_argument("--pipeline", required=True, help="Pipeline package name.")
    parser.add_argument(
        "--remove-python",
        action="store_true",
        help="Rename fixer_configs.py to fixer_configs.py.bak after converting.",
    )
    parser.set_defaults(_entry=main, _tool="migrate-fixers")


def main(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools migrate-fixers``."""
    try:
        dest = migrate_fixers(args.pipeline, remove_python=bool(args.remove_python))
    except MigrateFixersError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {dest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Invoke via 'ovid2 tools migrate-fixers'.")
