"""Formatter spec service — read + edit YAML specs (W2 read, W4 write).

Enumerates every ``ovid2_pipelines/<pipeline>/formatters/*.yaml`` (and any
legacy Python ``*.py`` factory) on disk. Validates YAML specs against
the packaged JSON schema ``ovid2/resources/schemas/formatter_spec.v1.json``
without importing :mod:`ovid2.formatters` (forbidden from the web tier).

Python-dict specs are treated as opaque: we return the source text so
the UI can display it, but we do not attempt to execute the module and
the editor refuses to save over one.

W4 write path:

- Only ``.yaml`` / ``.yml`` files can be written or deleted.
- Every write parses the raw text with :func:`yaml.safe_load`, then
  validates the resulting document against the packaged JSON schema.
  The write is refused when either step fails.
- Mutations append a row to ``web_audit`` with pre/post content hashes.
"""

from __future__ import annotations

import hashlib
import importlib.resources as pkg_resources
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from ovid2.web import db as web_db
from ovid2.web.models import (
    FormatterSpecDetail,
    FormatterValidationEntry,
    FormatterValidationReport,
    FormatterWrite,
    PipelineFileInfo,
    WriteResult,
)
from ovid2.web.services import pipelines_meta_svc

logger = logging.getLogger(__name__)

_SCHEMA_CACHE: dict[str, Any] = {}


class FormatterError(ValueError):
    """Raised when a formatter YAML write is invalid or targets a bad path."""


def _load_schema() -> dict[str, Any]:
    """Return the packaged formatter-spec JSON schema."""
    if "schema" not in _SCHEMA_CACHE:
        text = pkg_resources.files("ovid2.resources.schemas").joinpath(
            "formatter_spec.v1.json"
        ).read_text(encoding="utf-8")
        _SCHEMA_CACHE["schema"] = json.loads(text)
    return _SCHEMA_CACHE["schema"]


def _formatters_dir(pipeline: str) -> Path | None:
    """Return ``<pipeline>/formatters`` if the directory exists."""
    pkg_path = pipelines_meta_svc._pipeline_path(pipeline)
    if pkg_path is None:
        return None
    candidate = pkg_path / "formatters"
    return candidate if candidate.is_dir() else None


def list_formatters(pipeline: str) -> list[PipelineFileInfo]:
    """Return the per-file listing of formatter specs for a pipeline."""
    directory = _formatters_dir(pipeline)
    if directory is None:
        return []
    return pipelines_meta_svc._list_spec_files(directory)


def get_formatter(pipeline: str, table: str) -> FormatterSpecDetail | None:
    """Return raw text + parsed contents for one formatter spec.

    Args:
        pipeline: Pipeline name.
        table: Table name (filename stem).

    Returns:
        A :class:`FormatterSpecDetail`, or ``None`` when neither
        ``<table>.yaml`` nor ``<table>.py`` exists.
    """
    directory = _formatters_dir(pipeline)
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
    adapter: str | None = None
    if source_kind == "yaml":
        try:
            parsed_any = yaml.safe_load(raw_text)
            if isinstance(parsed_any, dict):
                parsed = parsed_any
                if isinstance(parsed.get("adapter"), str):
                    adapter = parsed["adapter"]
        except yaml.YAMLError as exc:
            logger.warning("Failed to parse %s: %s", path, exc)

    return FormatterSpecDetail(
        pipeline=pipeline,
        table=table,
        path=str(path),
        source_kind=source_kind,
        adapter=adapter,
        raw_text=raw_text,
        parsed=parsed,
    )


def validate_pipeline(pipeline: str) -> FormatterValidationReport:
    """Validate every YAML formatter spec for a pipeline against the JSON schema.

    Python-dict specs are reported as ``skipped`` (the schema does not
    describe Python source). YAML specs are validated with
    :mod:`jsonschema`.

    Args:
        pipeline: Pipeline name.

    Returns:
        A :class:`FormatterValidationReport` with one entry per file.
    """
    import jsonschema  # imported lazily so callers without web extras still work

    schema = _load_schema()
    directory = _formatters_dir(pipeline)
    entries: list[FormatterValidationEntry] = []
    if directory is None:
        return FormatterValidationReport(pipeline=pipeline, entries=[])

    for entry in sorted(directory.iterdir()):
        if entry.name.startswith("_"):
            continue
        table = entry.stem
        if entry.suffix in {".yaml", ".yml"}:
            try:
                data = yaml.safe_load(entry.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                entries.append(
                    FormatterValidationEntry(
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
                    FormatterValidationEntry(
                        table=table,
                        source_kind="yaml",
                        status="error",
                        message=exc.message,
                    )
                )
                continue
            adapter = data.get("adapter") if isinstance(data, dict) else None
            entries.append(
                FormatterValidationEntry(
                    table=table,
                    source_kind="yaml",
                    status="ok",
                    message=f"adapter={adapter}" if adapter else "ok",
                )
            )
        elif entry.suffix == ".py":
            entries.append(
                FormatterValidationEntry(
                    table=table,
                    source_kind="python",
                    status="skipped",
                    message="Python-dict spec — not validated against JSON schema.",
                )
            )
    return FormatterValidationReport(pipeline=pipeline, entries=entries)


# ---------------------------------------------------------------------------
# Write path (W4)
# ---------------------------------------------------------------------------


def _hash_text(text: str) -> str:
    """Return a stable content hash used in ``web_audit`` rows."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_table_name(table: str) -> None:
    """Reject spec names that could escape the ``formatters/`` directory."""
    if not table:
        raise FormatterError("Table name is required.")
    if not all(c.isalnum() or c in "-_." for c in table):
        raise FormatterError(
            f"Invalid table name '{table}'. Use letters, digits, '-', '_', '.' only."
        )
    if table.startswith("."):
        raise FormatterError(f"Table name '{table}' is reserved.")


def save_formatter(
    pipeline: str,
    table: str,
    payload: FormatterWrite,
    *,
    actor: str = "web",
) -> WriteResult:
    """Create or update a formatter YAML spec.

    The raw text is parsed with :func:`yaml.safe_load` and validated
    against the packaged JSON schema *before* touching disk. When the
    file already exists with a ``.py`` (Python-dict) source, the write
    is refused — those specs must be edited on disk per AGENTS.md.

    Args:
        pipeline: Pipeline name.
        table: Table stem.
        payload: Parsed :class:`FormatterWrite` from the client.
        actor: Audit label.

    Returns:
        :class:`WriteResult` describing the change.

    Raises:
        FormatterError: On any validation or filesystem error.
    """
    import jsonschema

    _validate_table_name(table)
    directory = _formatters_dir(pipeline)
    if directory is None:
        raise FormatterError(f"Pipeline '{pipeline}' has no formatters/ directory.")

    py_path = directory / f"{table}.py"
    if py_path.is_file():
        raise FormatterError(
            f"Refusing to overwrite Python-dict spec {py_path}. "
            "Edit on disk per AGENTS.md."
        )

    try:
        parsed = yaml.safe_load(payload.raw_text)
    except yaml.YAMLError as exc:
        raise FormatterError(f"YAML parse error: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FormatterError("Formatter YAML must be a mapping at the top level.")

    schema = _load_schema()
    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        raise FormatterError(f"Schema validation failed: {exc.message}") from exc

    # Prefer the existing extension; default to `.yaml` for new files.
    existing = directory / f"{table}.yaml"
    if not existing.is_file():
        alt = directory / f"{table}.yml"
        if alt.is_file():
            existing = alt
    target = existing if existing.is_file() else directory / f"{table}.yaml"

    action = "updated" if target.is_file() else "created"
    before_text = target.read_text(encoding="utf-8") if target.is_file() else ""
    before_hash = _hash_text(before_text) if before_text else None

    # Normalise line endings and append a trailing newline for git hygiene.
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
        action=f"formatter.{action}",
        target=f"{pipeline}/{table}",
        before_hash=before_hash,
        after_hash=_hash_text(text),
        detail_json=json.dumps(
            {"path": str(target), "adapter": parsed.get("adapter")}
        ),
    )
    logger.info("Formatter %s %s/%s at %s", action, pipeline, table, target)
    return WriteResult(name=f"{pipeline}/{table}", path=str(target), action=action)


def delete_formatter(
    pipeline: str,
    table: str,
    *,
    actor: str = "web",
) -> WriteResult:
    """Delete a formatter YAML spec.

    Python-dict specs are never deleted by the web tier. Missing files
    surface as :class:`FormatterError`.
    """
    _validate_table_name(table)
    directory = _formatters_dir(pipeline)
    if directory is None:
        raise FormatterError(f"Pipeline '{pipeline}' has no formatters/ directory.")

    py_path = directory / f"{table}.py"
    if py_path.is_file():
        raise FormatterError(
            f"Refusing to delete Python-dict spec {py_path}. Edit on disk."
        )

    target: Path | None = None
    for suffix in (".yaml", ".yml"):
        candidate = directory / f"{table}{suffix}"
        if candidate.is_file():
            target = candidate
            break
    if target is None:
        raise FormatterError(f"Formatter '{pipeline}/{table}' not found.")

    before_hash = _hash_text(target.read_text(encoding="utf-8"))
    target.unlink()
    web_db.ensure_schema()
    web_db.audit(
        actor=actor,
        action="formatter.deleted",
        target=f"{pipeline}/{table}",
        before_hash=before_hash,
        detail_json=json.dumps({"path": str(target)}),
    )
    logger.info("Formatter deleted %s/%s at %s", pipeline, table, target)
    return WriteResult(name=f"{pipeline}/{table}", path=str(target), action="deleted")
