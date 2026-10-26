"""Mapper spec service — read + edit YAML specs (W2 read, W4 write).

Mirror of :mod:`ovid2.web.services.formatters_svc` for the (optional)
per-pipeline mapper YAMLs / ``SPEC``-dict Python modules. Currently
every pipeline falls back to the legacy class-based mapper — the ``mappers/``
directory is empty across all 22 pipeline packages — but the UI surfaces
the folder so future YAML specs are discoverable as soon as they land.

Writes are refused on ``.py`` (``SPEC``-dict) files; those must be
edited on disk per AGENTS.md. YAML writes are schema-validated before
they touch the filesystem.
"""

from __future__ import annotations

import hashlib
import importlib.resources as pkg_resources
import json
import logging
from pathlib import Path
from typing import Any, cast

import yaml

from ovid2.web import db as web_db
from ovid2.web.models import (
    MapperSpecDetail,
    MapperValidationEntry,
    MapperValidationReport,
    MapperWrite,
    PipelineFileInfo,
    WriteResult,
)
from ovid2.web.services import pipelines_meta_svc

logger = logging.getLogger(__name__)

_SCHEMA_CACHE: dict[str, Any] = {}


class MapperError(ValueError):
    """Raised when a mapper YAML write is invalid or targets a bad path."""


def _load_schema() -> dict[str, Any]:
    """Return the packaged mapper-spec JSON schema."""
    if "schema" not in _SCHEMA_CACHE:
        text = (
            pkg_resources.files("ovid2.resources.schemas")
            .joinpath("mapper_spec.v1.json")
            .read_text(encoding="utf-8")
        )
        _SCHEMA_CACHE["schema"] = json.loads(text)
    return cast(dict[str, Any], _SCHEMA_CACHE["schema"])


def _mappers_dir(pipeline: str) -> Path | None:
    """Return ``<pipeline>/mappers`` if the directory exists."""
    pkg_path = pipelines_meta_svc._pipeline_path(pipeline)
    if pkg_path is None:
        return None
    candidate = pkg_path / "mappers"
    return candidate if candidate.is_dir() else None


def list_mappers(pipeline: str) -> list[PipelineFileInfo]:
    """Return the per-file listing of mapper specs for a pipeline."""
    directory = _mappers_dir(pipeline)
    if directory is None:
        return []
    return pipelines_meta_svc._list_spec_files(directory)


def get_mapper(pipeline: str, table: str) -> MapperSpecDetail | None:
    """Return raw text + parsed contents for one mapper spec."""
    directory = _mappers_dir(pipeline)
    if directory is None:
        return None
    yaml_path = directory / f"{table}.yaml"
    yml_path = directory / f"{table}.yml"
    py_path = directory / f"{table}.py"

    if yaml_path.is_file():
        path = yaml_path
        source_kind = "yaml"
    elif yml_path.is_file():
        path = yml_path
        source_kind = "yaml"
    elif py_path.is_file():
        path = py_path
        source_kind = "python"
    else:
        return None

    raw_text = path.read_text(encoding="utf-8", errors="replace")
    parsed: dict[str, Any] | None = None
    if source_kind == "yaml":
        try:
            parsed_any = yaml.safe_load(raw_text)
            if isinstance(parsed_any, dict):
                parsed = parsed_any
        except yaml.YAMLError as exc:
            logger.warning("Failed to parse %s: %s", path, exc)

    return MapperSpecDetail(
        pipeline=pipeline,
        table=table,
        path=str(path),
        source_kind=source_kind,
        raw_text=raw_text,
        parsed=parsed,
    )


def validate_pipeline(pipeline: str) -> MapperValidationReport:
    """Validate every YAML mapper spec for a pipeline against the JSON schema."""
    import jsonschema

    schema = _load_schema()
    directory = _mappers_dir(pipeline)
    entries: list[MapperValidationEntry] = []
    if directory is None:
        return MapperValidationReport(pipeline=pipeline, entries=[])

    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("_"):
            continue
        table = entry.stem
        if entry.suffix in {".yaml", ".yml"}:
            try:
                data = yaml.safe_load(entry.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                entries.append(
                    MapperValidationEntry(
                        table=table,
                        source_kind="yaml",
                        status="error",
                        message=f"YAML parse error: {exc}",
                    )
                )
                continue
            try:
                jsonschema.validate(data, schema)
            except jsonschema.ValidationError as exc:
                entries.append(
                    MapperValidationEntry(
                        table=table,
                        source_kind="yaml",
                        status="error",
                        message=exc.message,
                    )
                )
                continue
            entries.append(
                MapperValidationEntry(
                    table=table,
                    source_kind="yaml",
                    status="ok",
                    message="ok",
                )
            )
        elif entry.suffix == ".py":
            entries.append(
                MapperValidationEntry(
                    table=table,
                    source_kind="python",
                    status="skipped",
                    message="Python-dict spec — not validated against JSON schema.",
                )
            )
    return MapperValidationReport(pipeline=pipeline, entries=entries)


# ---------------------------------------------------------------------------
# Write path (W4)
# ---------------------------------------------------------------------------


def _hash_text(text: str) -> str:
    """Return a stable content hash used in ``web_audit`` rows."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_table_name(table: str) -> None:
    """Reject spec names that could escape the ``mappers/`` directory."""
    if not table:
        raise MapperError("Table name is required.")
    if not all(c.isalnum() or c in "-_." for c in table):
        raise MapperError(f"Invalid table name '{table}'. Use letters, digits, '-', '_', '.' only.")
    if table.startswith("."):
        raise MapperError(f"Table name '{table}' is reserved.")


def save_mapper(
    pipeline: str,
    table: str,
    payload: MapperWrite,
    *,
    actor: str = "web",
) -> WriteResult:
    """Create or update a mapper YAML spec (schema-validated).

    Refuses to write when ``<table>.py`` exists (that is a ``SPEC``-dict
    module — edit on disk per AGENTS.md).

    Raises:
        MapperError: On any validation failure.
    """
    import jsonschema

    _validate_table_name(table)
    directory = _mappers_dir(pipeline)
    if directory is None:
        # Auto-create the mappers/ directory when it does not exist yet —
        # every pipeline package is allowed to grow one.
        pkg_path = pipelines_meta_svc._pipeline_path(pipeline)
        if pkg_path is None:
            raise MapperError(f"Pipeline '{pipeline}' is not installed.")
        directory = pkg_path / "mappers"
        directory.mkdir(parents=True, exist_ok=True)

    py_path = directory / f"{table}.py"
    if py_path.is_file():
        raise MapperError(
            f"Refusing to overwrite SPEC-dict module {py_path}. Edit on disk per AGENTS.md."
        )

    try:
        parsed = yaml.safe_load(payload.raw_text)
    except yaml.YAMLError as exc:
        raise MapperError(f"YAML parse error: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MapperError("Mapper YAML must be a mapping at the top level.")

    schema = _load_schema()
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise MapperError(f"Schema validation failed: {exc.message}") from exc

    existing = directory / f"{table}.yaml"
    if not existing.is_file():
        alt = directory / f"{table}.yml"
        if alt.is_file():
            existing = alt
    target = existing if existing.is_file() else directory / f"{table}.yaml"

    action = "updated" if target.is_file() else "created"
    before_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    before_hash = _hash_text(before_text) if before_text else None

    text = payload.raw_text
    if not text.endswith("\n"):
        text += "\n"

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(target)

    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action=f"mapper.{action}",
        target=f"{pipeline}/{table}",
        before_hash=before_hash,
        after_hash=_hash_text(text),
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Mapper %s %s/%s at %s", action, pipeline, table, target)
    return WriteResult(name=f"{pipeline}/{table}", path=str(target), action=action)


def delete_mapper(
    pipeline: str,
    table: str,
    *,
    actor: str = "web",
) -> WriteResult:
    """Delete a mapper YAML spec (Python ``SPEC``-dict modules are never touched)."""
    _validate_table_name(table)
    directory = _mappers_dir(pipeline)
    if directory is None:
        raise MapperError(f"Pipeline '{pipeline}' has no mappers/ directory.")

    py_path = directory / f"{table}.py"
    if py_path.is_file():
        raise MapperError(f"Refusing to delete SPEC-dict module {py_path}. Edit on disk.")

    target: Path | None = None
    for suffix in (".yaml", ".yml"):
        candidate = directory / f"{table}{suffix}"
        if candidate.is_file():
            target = candidate
            break
    if target is None:
        raise MapperError(f"Mapper '{pipeline}/{table}' not found.")

    before_hash = _hash_text(target.read_text(encoding="utf-8"))
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="mapper.deleted",
        target=f"{pipeline}/{table}",
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Mapper deleted %s/%s at %s", pipeline, table, target)
    return WriteResult(name=f"{pipeline}/{table}", path=str(target), action="deleted")
