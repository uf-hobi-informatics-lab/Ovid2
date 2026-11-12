"""Load a pipeline's fixer chains from YAML or Python (web-safe).

A pipeline declares its fix chains either in
``pipelines/ovid2_pipelines/<pipeline>/fixer_configs.yaml`` (preferred;
editable in the web builder; schema ``fixer_configs.v1.json``)::

    v: 1
    fixer_chains:
      encounter:
        - {fix: patid_orphans, reference_table: demographic}
    multi_table_fixes: []

or in the legacy ``fixer_configs.py`` module (``FIXER_CHAINS`` /
``MULTI_TABLE_FIXES``). The YAML wins when both exist. The fix stage, the
web plan / builder and the contract tests all read through
:func:`load_fixer_configs` so they always agree.
"""

from __future__ import annotations

import importlib
import json
from importlib import resources
from pathlib import Path
from typing import Any

__all__ = [
    "FIXER_YAML",
    "FixerConfigsError",
    "dump_fixer_yaml",
    "fixer_yaml_path",
    "load_fixer_configs",
    "validate_fixer_document",
]

FIXER_YAML = "fixer_configs.yaml"

_SCHEMA: dict[str, Any] = {}


class FixerConfigsError(ValueError):
    """Raised when a fixer_configs.yaml fails validation."""


def _schema() -> dict[str, Any]:
    if not _SCHEMA:
        ref = resources.files("ovid2.resources.schemas") / "fixer_configs.v1.json"
        with ref.open("r", encoding="utf-8") as fh:
            _SCHEMA.update(json.load(fh))
    return _SCHEMA


def _package_dirs(pipeline: str) -> list[Path]:
    try:
        pkg = importlib.import_module(f"ovid2_pipelines.{pipeline}")
    except ModuleNotFoundError:
        return []
    return [Path(p) for p in getattr(pkg, "__path__", []) or []]


def fixer_yaml_path(pipeline: str) -> Path | None:
    """Return the pipeline's ``fixer_configs.yaml`` when it exists."""
    for base in _package_dirs(pipeline):
        candidate = base / FIXER_YAML
        if candidate.is_file():
            return candidate
    return None


def validate_fixer_document(raw: Any) -> list[str]:
    """Return schema + fixer-registry errors for a decoded YAML document.

    Checks the JSON schema, that every ``fix`` names a catalogued fixer,
    and that every argument key is canonical or one of that fixer's
    declared aliases.
    """
    from jsonschema import Draft202012Validator

    from ovid2.config.fixers_catalogue import CANONICAL_KEYS, FIXER_CATALOGUE

    if not isinstance(raw, dict):
        return ["<root>: fixer configs must be a mapping"]
    errors = [
        f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}"
        for e in sorted(
            Draft202012Validator(_schema()).iter_errors(raw), key=lambda e: list(e.path)
        )
    ]
    if errors:
        return errors

    def _check(where: str, entry: dict[str, Any]) -> None:
        name = str(entry.get("fix", ""))
        meta = FIXER_CATALOGUE.get(name)
        if meta is None:
            errors.append(f"{where}: unknown fix '{name}'")
            return
        allowed = set(CANONICAL_KEYS) | set(meta["aliases"])
        for key in entry:
            if key not in allowed:
                errors.append(f"{where}: unrecognised key '{key}' for fix '{name}'")

    for table, entries in (raw.get("fixer_chains") or {}).items():
        for i, entry in enumerate(entries):
            _check(f"fixer_chains/{table}/{i}", entry)
    for i, entry in enumerate(raw.get("multi_table_fixes") or []):
        _check(f"multi_table_fixes/{i}", entry)
    return errors


def load_fixer_configs(
    pipeline: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], str]:
    """Return ``(fixer_chains, multi_table_fixes, source)`` for ``pipeline``.

    ``source`` is ``'yaml'``, ``'python'`` or ``''`` (none declared).

    Raises:
        FixerConfigsError: When the YAML exists but is invalid.
    """
    path = fixer_yaml_path(pipeline)
    if path is not None:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        errors = validate_fixer_document(raw)
        if errors:
            raise FixerConfigsError(f"{path}: " + "; ".join(errors))
        chains = {str(t): [dict(e) for e in v] for t, v in (raw.get("fixer_chains") or {}).items()}
        multi = [dict(e) for e in (raw.get("multi_table_fixes") or [])]
        return chains, multi, "yaml"
    try:
        module = importlib.import_module(f"ovid2_pipelines.{pipeline}.fixer_configs")
    except ModuleNotFoundError:
        return {}, [], ""
    chains_raw = getattr(module, "FIXER_CHAINS", {}) or {}
    multi_raw = getattr(module, "MULTI_TABLE_FIXES", []) or []
    chains = {
        str(t): [dict(e) for e in v if isinstance(e, dict)]
        for t, v in chains_raw.items()
        if isinstance(v, (list, tuple))
    }
    multi = [dict(e) for e in multi_raw if isinstance(e, dict)]
    return chains, multi, "python"


def dump_fixer_yaml(
    chains: dict[str, list[dict[str, Any]]],
    multi: list[dict[str, Any]],
    *,
    description: str | None = None,
) -> str:
    """Render chains as a ``fixer_configs.yaml`` document (flow-style entries)."""
    import yaml

    lines = ["v: 1"]
    if description:
        lines.append(f"description: {json.dumps(description)}")
    lines.append("fixer_chains:" if chains else "fixer_chains: {}")
    for table, entries in chains.items():
        lines.append(f"  {table}:" if entries else f"  {table}: []")
        for entry in entries:
            flow = yaml.safe_dump(entry, default_flow_style=True, sort_keys=False, width=1000)
            lines.append(f"    - {flow.strip()}")
    lines.append("multi_table_fixes:" if multi else "multi_table_fixes: []")
    for entry in multi:
        flow = yaml.safe_dump(entry, default_flow_style=True, sort_keys=False, width=1000)
        lines.append(f"  - {flow.strip()}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.fixer_configs is a library module.")
