"""YAML loader for the cross-CDM semantic tree (phase S3).

Reads every YAML file under

- ``src/ovid2/resources/semantic/vocabularies/*.yaml`` — one
  :class:`~ovid2.semantic.handle.VocabularyHandle` per file.
- ``src/ovid2/resources/semantic/bindings/*.yaml`` — one file per CDM,
  each carrying a list of :class:`~ovid2.semantic.registry.ColumnBinding`
  rows.

Called at import time from :mod:`ovid2.semantic.vocabularies.__init__`
so bundled vocabularies register themselves alongside the OMOP CONCEPT
handle and the PCORnet valuesets.

Design notes:

- **Idempotent**. Registering a handle that is already registered logs
  and continues (matches the behaviour of the PCORnet valueset loader
  in :mod:`ovid2.semantic.vocabularies.pcornet`).
- **Non-fatal**. A malformed YAML, a missing schema field, or a binding
  that references an unregistered vocabulary emits a warning and is
  skipped. This mirrors the CDM YAML loader's "partial tree is fine"
  policy and lets a laptop without the OMOP slice CSVs still boot.
- **Storage-agnostic sources.** Vocabularies may use any of the four
  legs on :class:`VocabularyHandle`: ``source_path``, ``shared_view``,
  ``address``, ``inline_items``. The ``shared_view`` leg accepts the
  ``{cdm, view}`` object form introduced by phase S3 (widened
  ``semantic_handle.v1.json``); the historical bare-string form is
  reserved for a future flat-name registry.
- **Path variables** — ``${OMOP_CDM_DIR}``, ``${PCORNET_CDM_DIR}``,
  ``${MATERIAL_DIR}`` are expanded via :mod:`ovid2.config.settings` on
  ``source_path`` values so YAML content stays deployment-agnostic.
- **No pipeline imports.** The loader is host-side only; it never
  imports Spark, snowpark, or any pipeline module.
"""

from __future__ import annotations

import logging
import os
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from ovid2.semantic.handle import VocabularyHandle
from ovid2.semantic.registry import (
    ColumnBinding,
    bind_column,
    register,
)

logger = logging.getLogger(__name__)

__all__ = [
    "build_binding",
    "build_vocabulary",
    "iter_binding_docs",
    "iter_vocabulary_docs",
    "register_all",
    "semantic_yaml_root",
]


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def semantic_yaml_root() -> Path:
    """Return the packaged root under ``src/ovid2/resources/semantic/``."""
    with resources.as_file(resources.files("ovid2.resources").joinpath("semantic")) as path:
        return Path(path)


def _vocabularies_dir() -> Path:
    """Return the packaged ``resources/semantic/vocabularies/`` directory."""
    return semantic_yaml_root() / "vocabularies"


def _bindings_dir() -> Path:
    """Return the packaged ``resources/semantic/bindings/`` directory."""
    return semantic_yaml_root() / "bindings"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file and require a dict at the top level."""
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level.")
    return raw


def _expand_path(raw: str) -> Path:
    """Expand ``${OMOP_CDM_DIR}`` / ``${PCORNET_CDM_DIR}`` / ``${MATERIAL_DIR}``.

    Also expands generic ``$VAR`` tokens against the process environment
    so operators can override slice locations without editing YAML.
    """
    # Local import — avoids a circular import at package load time (the
    # semantic layer is imported very early in ovid2 startup).
    from ovid2.config import settings as _settings

    mapping = {
        "OMOP_CDM_DIR": str(_settings.OMOP_CDM_DIR),
        "PCORNET_CDM_DIR": str(_settings.PCORNET_CDM_DIR),
        "MATERIAL_DIR": str(_settings.MATERIAL_DIR),
    }
    expanded = raw
    for key, value in mapping.items():
        expanded = expanded.replace(f"${{{key}}}", value)
    expanded = os.path.expandvars(expanded)
    return Path(expanded)


# ---------------------------------------------------------------------------
# Iteration
# ---------------------------------------------------------------------------


def iter_vocabulary_docs() -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(name_stem, payload)`` for every packaged vocabulary YAML.

    ``name_stem`` is the filename without the ``.yaml`` suffix — used
    only for logging. The real handle name comes from ``payload['name']``.
    """
    result: list[tuple[str, dict[str, Any]]] = []
    directory = _vocabularies_dir()
    if not directory.is_dir():
        return result
    for path in sorted(directory.iterdir()):
        if path.suffix != ".yaml":
            continue
        try:
            payload = _load_yaml(path)
        except (ValueError, yaml.YAMLError) as exc:
            logger.warning("Skipping vocabulary YAML %s: %s", path, exc)
            continue
        result.append((path.stem, payload))
    return result


def iter_binding_docs() -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(cdm_name, payload)`` for every packaged binding YAML.

    ``cdm_name`` is taken from the YAML body (``payload['cdm']``); the
    filename is treated as advisory. A YAML whose ``cdm`` field is
    missing or doesn't match the filename stem is logged and skipped.
    """
    result: list[tuple[str, dict[str, Any]]] = []
    directory = _bindings_dir()
    if not directory.is_dir():
        return result
    for path in sorted(directory.iterdir()):
        if path.suffix != ".yaml":
            continue
        try:
            payload = _load_yaml(path)
        except (ValueError, yaml.YAMLError) as exc:
            logger.warning("Skipping binding YAML %s: %s", path, exc)
            continue
        cdm = payload.get("cdm")
        if not isinstance(cdm, str) or not cdm:
            logger.warning(
                "Skipping binding YAML %s: missing/invalid 'cdm' field.",
                path,
            )
            continue
        if cdm != path.stem:
            logger.warning(
                "Binding YAML %s: filename stem %r does not match 'cdm' %r; using the YAML value.",
                path,
                path.stem,
                cdm,
            )
        result.append((cdm, payload))
    return result


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def build_vocabulary(payload: dict[str, Any]) -> VocabularyHandle:
    """Convert one vocabulary YAML payload into a :class:`VocabularyHandle`.

    Args:
        payload: The parsed YAML mapping.

    Raises:
        ValueError: If required fields are missing, the source-leg
            invariants are violated, or a ``shared_view`` reference
            cannot be resolved.
    """
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("vocabulary YAML missing required 'name' field.")
    kind = payload.get("kind", "vocabulary")
    if kind != "vocabulary":
        raise ValueError(f"vocabulary YAML '{name}': kind must be 'vocabulary', got {kind!r}.")
    code_column = payload.get("code_column")
    if not isinstance(code_column, str) or not code_column:
        raise ValueError(f"vocabulary YAML '{name}' missing 'code_column'.")

    source_path = _resolve_source_path(payload)
    shared_view = _resolve_shared_view(name, payload)
    address = payload.get("address")
    address_table = payload.get("address_table")
    inline_items = _resolve_inline_items(payload)

    # Optional metadata.
    tags_raw = payload.get("tags") or ()
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()

    return VocabularyHandle(
        name=name,
        version=str(payload.get("version") or ""),
        description=str(payload.get("description") or ""),
        view_name=str(payload.get("view_name") or ""),
        code_column=code_column,
        description_column=(
            str(payload["description_column"]) if payload.get("description_column") else None
        ),
        domain_column=(str(payload["domain_column"]) if payload.get("domain_column") else None),
        source_path=source_path,
        shared_view=shared_view,
        address=str(address) if address else None,
        address_table=str(address_table) if address_table else None,
        inline_items=inline_items,
        delimiter=str(payload.get("delimiter", "\t")),
        header=bool(payload.get("header", True)),
        required=bool(payload.get("required", False)),
        tags=tags,
    )


def _resolve_source_path(payload: dict[str, Any]) -> Path | None:
    """Return the resolved ``source_path`` :class:`Path`, or ``None``."""
    raw = payload.get("source_path")
    if not raw:
        return None
    if not isinstance(raw, str):
        raise ValueError(
            f"vocabulary YAML '{payload.get('name')}': 'source_path' must be a string."
        )
    return _expand_path(raw)


def _resolve_shared_view(name: str, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    """Resolve the ``shared_view`` leg to a :class:`SharedViewSpec` or ``None``.

    Accepts two shapes:

    - **string** — bare view name. Reserved for a future flat-name
      registry; not currently supported and raises ``ValueError``.
    - **object** — ``{cdm: <name>, view: <view_name>}``. Looks up
      ``cdm`` in :func:`ovid2.cdm.registry.get_cdm` and finds a
      :class:`SharedViewSpec` matching ``view`` in its
      ``shared_views`` tuple.
    """
    raw = payload.get("shared_view")
    if raw is None:
        return None
    if isinstance(raw, str):
        raise ValueError(
            f"vocabulary '{name}': bare-string 'shared_view' is not yet "
            "supported. Use the {cdm, view} object form."
        )
    if not isinstance(raw, dict):
        raise ValueError(
            f"vocabulary '{name}': 'shared_view' must be a mapping "
            f"{{cdm, view}}, got {type(raw).__name__}."
        )
    cdm_name = raw.get("cdm")
    view_name = raw.get("view")
    if not isinstance(cdm_name, str) or not isinstance(view_name, str):
        raise ValueError(
            f"vocabulary '{name}': 'shared_view' requires 'cdm' and 'view' string fields."
        )

    from ovid2.cdm.registry import get_cdm

    try:
        handle = get_cdm(cdm_name)
    except ValueError as exc:
        raise ValueError(
            f"vocabulary '{name}': 'shared_view.cdm' refers to unknown CDM "
            f"'{cdm_name}'. Underlying error: {exc}"
        ) from exc
    for spec in handle.shared_views:
        if spec.view_name == view_name:
            return spec
    known = [s.view_name for s in handle.shared_views]
    raise ValueError(
        f"vocabulary '{name}': CDM '{cdm_name}' has no shared view "
        f"'{view_name}'. Known shared views: {known or '(none)'}."
    )


def _resolve_inline_items(
    payload: dict[str, Any],
) -> tuple[tuple[str, str | None], ...]:
    """Return the ``inline_items`` tuple, or an empty tuple when absent."""
    raw = payload.get("inline_items")
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"vocabulary '{payload.get('name')}': 'inline_items' must be a list.")
    items: list[tuple[str, str | None]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if code is None:
            continue
        label = entry.get("label")
        items.append((str(code), str(label) if label is not None else None))
    return tuple(items)


def build_binding(cdm: str, row: dict[str, Any]) -> ColumnBinding:
    """Convert one row from a bindings YAML into a :class:`ColumnBinding`.

    Raises:
        ValueError: If required fields are missing.
    """
    table = row.get("table")
    column = row.get("column")
    vocabulary = row.get("vocabulary")
    if not isinstance(table, str) or not table:
        raise ValueError(f"binding for CDM '{cdm}' missing 'table'.")
    if not isinstance(column, str) or not column:
        raise ValueError(f"binding for {cdm}.{table} missing 'column'.")
    if not isinstance(vocabulary, str) or not vocabulary:
        raise ValueError(f"binding for {cdm}.{table}.{column} missing 'vocabulary'.")
    return ColumnBinding(
        cdm=cdm,
        table=str(table),
        column=str(column).upper(),
        vocabulary=vocabulary,
        role=(str(row["role"]) if row.get("role") else None),
        valueset=(str(row["valueset"]) if row.get("valueset") else None),
        required=bool(row.get("required", False)),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def register_all() -> None:
    """Register every packaged vocabulary and binding.

    Two passes:

    1. **Vocabularies.** Malformed rows are warned + skipped. Handles
       that are already registered (test reload, double import) log
       at DEBUG and continue.
    2. **Bindings.** Rows whose vocabulary is not registered, or whose
       ``(cdm, table)`` is unknown, log at WARNING and are skipped —
       matches the CDM YAML loader's "partial tree is fine" policy.
    """
    vocab_count = 0
    for stem, payload in iter_vocabulary_docs():
        try:
            handle = build_vocabulary(payload)
        except (ValueError, KeyError) as exc:
            logger.warning("Skipping vocabulary %s: %s", stem, exc)
            continue
        try:
            register(handle)
        except ValueError:
            # Already registered — idempotent on reload / test scenarios.
            logger.debug("Vocabulary %s already registered; skipping.", handle.name)
            continue
        vocab_count += 1

    binding_count = 0
    for cdm, payload in iter_binding_docs():
        rows = payload.get("bindings") or []
        if not isinstance(rows, list):
            logger.warning(
                "Binding YAML for CDM %s: 'bindings' must be a list.",
                cdm,
            )
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                binding = build_binding(cdm, row)
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "Skipping binding row in %s: %s (row=%r)",
                    cdm,
                    exc,
                    row,
                )
                continue
            try:
                bind_column(binding)
            except ValueError as exc:
                # Unknown vocabulary or duplicate binding — non-fatal.
                logger.warning(
                    "Skipping binding %s.%s.%s: %s",
                    binding.cdm,
                    binding.table,
                    binding.column,
                    exc,
                )
                continue
            binding_count += 1

    logger.debug(
        "Semantic YAML loader registered %d vocabulary(ies), %d binding(s).",
        vocab_count,
        binding_count,
    )
