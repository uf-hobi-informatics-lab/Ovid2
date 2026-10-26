"""Load a :class:`~ovid2.cdm.handle.CdmHandle` from YAML.

Ovid2 ships one YAML directory per CDM under ``src/ovid2/resources/cdms/``.
Layout (phase C1)::

    resources/cdms/<cdm_name>/
    ├── cdm.yaml                       # handle-level metadata
    ├── tables/<table>.yaml            # one per CDM table
    └── valuesets/<table>.<field>.yaml # one per VALUESET=YES field

This loader consumes that tree and produces a fully populated
:class:`CdmHandle`, including the ``valuesets`` fast-path mirror. The
per-table YAML rows are cached in :mod:`ovid2.schema.cdm` at load time
so the existing ``output_columns_from:`` validation and Snowflake DDL
builder keep working without touching the CSV.

The loader is deliberately pure-Python and does not import Spark, so
it is safe to call from contract tests, the web tier, the installer,
and any other host-side utility.

Path variables inside ``shared_views.<view>.source.path`` are resolved
against :mod:`ovid2.config.settings` at load time; ``${OMOP_CDM_DIR}``
is expanded to :data:`ovid2.config.settings.OMOP_CDM_DIR`.
"""

from __future__ import annotations

import logging
import os
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ovid2.cdm.handle import CdmHandle, IdColumn, SharedViewSpec

if TYPE_CHECKING:
    from ovid2.schema.cdm import CdmField

logger = logging.getLogger(__name__)

__all__ = [
    "CdmYamlError",
    "cdm_yaml_dir",
    "cdm_yaml_root",
    "iter_valueset_docs",
    "load_cdm",
    "load_table_fields_from_yaml",
]


class CdmYamlError(ValueError):
    """Raised when a CDM YAML tree cannot be loaded."""


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def cdm_yaml_root() -> Path:
    """Return the packaged root under ``src/ovid2/resources/cdms/``."""
    with resources.as_file(resources.files("ovid2.resources").joinpath("cdms")) as path:
        return Path(path)


def cdm_yaml_dir(cdm_name: str) -> Path:
    """Return the packaged directory for ``cdm_name``.

    Raises:
        CdmYamlError: If ``cdm_name`` has no YAML directory.
    """
    directory = cdm_yaml_root() / cdm_name
    if not directory.is_dir():
        raise CdmYamlError(f"No CDM YAML directory for '{cdm_name}' at {directory}.")
    return directory


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file and require a dict at the top level."""
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise CdmYamlError(f"{path}: expected a YAML mapping at the top level.")
    return raw


def _expand_path(raw: str) -> Path:
    """Expand ``${OMOP_CDM_DIR}`` / ``${PCORNET_CDM_DIR}`` / ``$VAR`` tokens."""
    # Import locally to avoid a circular import at module load: this
    # module is imported from `ovid2.schema.cdm` which is in turn
    # imported from `ovid2.config.settings` in some workflows.
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
# Per-section parsers
# ---------------------------------------------------------------------------


def _parse_id_columns(raw: dict[str, Any]) -> dict[str, IdColumn]:
    """Parse the ``id_columns`` block of ``cdm.yaml``."""
    result: dict[str, IdColumn] = {}
    for role, payload in (raw or {}).items():
        if not isinstance(payload, dict):
            raise CdmYamlError(f"id_columns[{role!r}]: expected a mapping.")
        column = payload.get("column")
        if not column:
            raise CdmYamlError(f"id_columns[{role!r}]: missing required 'column'.")
        result[role] = IdColumn(
            role=role,
            column=str(column),
            width=int(payload.get("width", 64)),
            reference_table=payload.get("reference_table") or None,
        )
    return result


def _parse_shared_views(raw: list[Any] | None) -> tuple[SharedViewSpec, ...]:
    """Parse the ``shared_views`` block of ``cdm.yaml``.

    Two source shapes are accepted:

    * ``source.kind == 'file'`` (default) — requires ``source.path``.
      Read directly by Spark at registration time.
    * ``source.kind == 'address'`` — requires ``source.address`` (the
      filename stem of an address config). Resolved through
      :func:`ovid2.drivers.registry.driver_from_address` and materialised
      via the active engine's stager, so the vocabulary can live in a
      database reachable by both Spark and Snowpark.
    """
    result: list[SharedViewSpec] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            raise CdmYamlError(f"shared_views: expected a mapping, got {type(entry).__name__}.")
        view_name = entry.get("view_name")
        source = entry.get("source") or {}
        if not view_name or not isinstance(source, dict):
            raise CdmYamlError(
                f"shared_views[{view_name!r}]: 'view_name' and 'source' are required."
            )
        source_kind = source.get("kind", "file")
        if source_kind == "file":
            path_raw = source.get("path")
            if not path_raw:
                raise CdmYamlError(f"shared_views[{view_name!r}]: 'source.path' is required.")
            result.append(
                SharedViewSpec(
                    view_name=str(view_name),
                    source_path=_expand_path(str(path_raw)),
                    delimiter=str(source.get("delimiter", "\t")),
                    header=bool(source.get("header", True)),
                    required=bool(source.get("required", False)),
                    source_kind="file",
                )
            )
        elif source_kind == "address":
            address = source.get("address")
            if not address:
                raise CdmYamlError(
                    f"shared_views[{view_name!r}]: 'source.address' is required for kind='address'."
                )
            result.append(
                SharedViewSpec(
                    view_name=str(view_name),
                    source_path=None,
                    delimiter=str(source.get("delimiter", "\t")),
                    header=bool(source.get("header", True)),
                    required=bool(source.get("required", False)),
                    source_kind="address",
                    address=str(address),
                )
            )
        else:
            raise CdmYamlError(
                f"shared_views[{view_name!r}]: unknown source.kind "
                f"{source_kind!r} (expected 'file' or 'address')."
            )
    return tuple(result)


def _parse_flavour_of_null(raw: list[Any] | None) -> frozenset[str | None]:
    """Parse the ``flavour_of_null`` block of ``cdm.yaml``.

    Empty strings are preserved as ``""``; explicit ``null`` in YAML
    becomes Python ``None``. Both are meaningful sentinels.
    """
    if raw is None:
        return frozenset()
    values: set[str | None] = set()
    for item in raw:
        if item is None:
            values.add(None)
        else:
            values.add(str(item))
    return frozenset(values)


def _parse_composite_keys(
    tables: list[str],
    tables_dir: Path,
) -> tuple[dict[str, tuple[str, ...]], dict[str, dict[str, Any]]]:
    """Load per-table YAML files, returning composite keys + raw payloads.

    Insertion order into the returned dict follows ``tables`` — the
    canonical processing order.
    """
    composite_keys: dict[str, tuple[str, ...]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    for table in tables:
        path = tables_dir / f"{table}.yaml"
        if not path.is_file():
            logger.warning(
                "CDM table YAML missing at %s; skipping composite key for '%s'.",
                path,
                table,
            )
            continue
        payload = _load_yaml(path)
        payloads[table] = payload
        key = payload.get("composite_key") or []
        if not isinstance(key, list):
            raise CdmYamlError(f"{path}: 'composite_key' must be a list.")
        composite_keys[table] = tuple(str(k) for k in key)
    return composite_keys, payloads


def _parse_valuesets(cdm_name: str, cdm_dir: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """Walk ``valuesets/`` and return the ``{table: {column: (codes,)}}`` mirror.

    Missing directory returns an empty dict (a CDM may have no bounded
    valuesets). Filenames must match ``<table>.<field>.yaml`` — anything
    else is skipped with a warning.
    """
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    directory = cdm_dir / "valuesets"
    if not directory.is_dir():
        return result

    for path in sorted(directory.iterdir()):
        if path.suffix != ".yaml":
            continue
        stem = path.stem  # e.g. `demographic.sex`
        if "." not in stem:
            logger.warning("Skipping valueset file %s: expected <table>.<field>.yaml.", path)
            continue
        table, _, field_name = stem.partition(".")
        try:
            payload = _load_yaml(path)
        except (CdmYamlError, yaml.YAMLError) as exc:
            logger.warning("Skipping valueset %s: %s", path, exc)
            continue
        items = payload.get("items") or []
        if not isinstance(items, list):
            logger.warning("Skipping valueset %s: 'items' must be a list.", path)
            continue
        codes: list[str] = []
        for item in items:
            if isinstance(item, dict) and "code" in item:
                codes.append(str(item["code"]))
        if codes:
            result.setdefault(table, {})[field_name.upper()] = tuple(codes)
    return result


# ---------------------------------------------------------------------------
# Schema access — populates the ovid2.schema.cdm cache from YAML
# ---------------------------------------------------------------------------


def load_table_fields_from_yaml(cdm_name: str, table: str) -> list[CdmField]:
    """Return the ordered :class:`CdmField` rows for ``(cdm_name, table)``.

    Reads ``resources/cdms/<cdm>/tables/<table>.yaml`` fresh — callers
    should cache the result. Missing files return an empty list so
    developer laptops without every CDM YAML degrade gracefully.
    """
    from ovid2.schema.cdm import CdmField  # local: avoids cycle

    try:
        directory = cdm_yaml_dir(cdm_name)
    except CdmYamlError:
        return []
    path = directory / "tables" / f"{table.lower()}.yaml"
    if not path.is_file():
        return []
    payload = _load_yaml(path)
    fields_raw = payload.get("fields") or []
    rows: list[CdmField] = []
    for entry in fields_raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        rdbms_type = entry.get("rdbms_type") or ""
        if not name:
            continue
        rows.append(
            CdmField(
                table=table.upper(),
                name=str(name).upper(),
                rdbms_type=str(rdbms_type),
            )
        )
    return rows


def iter_valueset_docs(cdm_name: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Yield ``(table, field, payload)`` for every valueset YAML of a CDM.

    Used by :mod:`ovid2.semantic.vocabularies.pcornet` (phase C1 commit 2)
    to auto-register vocabularies at import time.
    """
    result: list[tuple[str, str, dict[str, Any]]] = []
    try:
        directory = cdm_yaml_dir(cdm_name)
    except CdmYamlError:
        return result
    valuesets_dir = directory / "valuesets"
    if not valuesets_dir.is_dir():
        return result
    for path in sorted(valuesets_dir.iterdir()):
        if path.suffix != ".yaml":
            continue
        stem = path.stem
        if "." not in stem:
            continue
        table, _, field_name = stem.partition(".")
        try:
            payload = _load_yaml(path)
        except (CdmYamlError, yaml.YAMLError):
            continue
        result.append((table, field_name, payload))
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_cdm(cdm_name: str) -> CdmHandle:
    """Return a fully populated :class:`CdmHandle` for ``cdm_name``.

    Reads:

    - ``cdm.yaml``                — handle-level metadata.
    - ``tables/<table>.yaml``     — composite keys.
    - ``valuesets/*.yaml``        — bounded valueset codes.

    Args:
        cdm_name: Snake-case CDM identifier, e.g. ``pcornet_7_0``.

    Returns:
        A frozen, immutable :class:`CdmHandle`.

    Raises:
        CdmYamlError: If the tree is missing or malformed.
    """
    directory = cdm_yaml_dir(cdm_name)
    root_path = directory / "cdm.yaml"
    if not root_path.is_file():
        raise CdmYamlError(f"Missing {root_path}.")
    root = _load_yaml(root_path)

    if root.get("name") != cdm_name:
        raise CdmYamlError(f"{root_path}: 'name' is {root.get('name')!r}, expected {cdm_name!r}.")

    version = str(root.get("version") or "")
    tables_raw = root.get("tables") or []
    if not isinstance(tables_raw, list) or not tables_raw:
        raise CdmYamlError(f"{root_path}: 'tables' must be a non-empty list.")
    tables = [str(t) for t in tables_raw]

    reference_tables = frozenset(str(t) for t in (root.get("reference_tables") or []))
    flavour_of_null = _parse_flavour_of_null(root.get("flavour_of_null"))
    id_columns = _parse_id_columns(root.get("id_columns") or {})
    shared_views = _parse_shared_views(root.get("shared_views"))

    composite_keys, _ = _parse_composite_keys(tables, directory / "tables")

    # Curated overrides: some deployments encode dedup semantics that
    # differ from the CDM's declared primary key (e.g. PCORnet 7.0's
    # surrogate DIAGNOSISID vs the (ENCOUNTERID, DX, DX_TYPE) dedup key
    # actually used by Ovid pipelines). ``composite_key_overrides:`` in
    # ``cdm.yaml`` wins over the value declared in the per-table YAML.
    overrides = root.get("composite_key_overrides") or {}
    if not isinstance(overrides, dict):
        raise CdmYamlError(f"{root_path}: 'composite_key_overrides' must be a mapping.")
    for table, keys in overrides.items():
        if not isinstance(keys, list) or not keys:
            raise CdmYamlError(
                f"{root_path}: composite_key_overrides[{table!r}] must be a non-empty list."
            )
        composite_keys[str(table)] = tuple(str(k) for k in keys)

    valuesets = _parse_valuesets(cdm_name, directory)

    # Populate the schema.cdm cache so `output_columns_from` and DDL
    # generation read from YAML instead of the CSV. Best-effort — a
    # missing table YAML is a warning, not a fatal error.
    _populate_schema_cache(cdm_name, tables)

    handle = CdmHandle(
        name=cdm_name,
        version=version,
        composite_keys=composite_keys,
        reference_tables=reference_tables,
        id_columns=id_columns,
        flavour_of_null=flavour_of_null,
        shared_views=shared_views,
        schema_source=None,  # YAML replaces the CSV path
        schema_loader=lambda name, table: load_table_fields_from_yaml(name, table),
        valuesets=valuesets,
    )
    return handle


def _populate_schema_cache(cdm_name: str, tables: list[str]) -> None:
    """Prime the module-level cache in :mod:`ovid2.schema.cdm` from YAML."""
    from ovid2.schema import cdm as _schema_cdm  # local: avoids cycle

    rows: list[CdmField] = []
    for table in tables:
        rows.extend(load_table_fields_from_yaml(cdm_name, table))
    if rows:
        _schema_cdm._CDM_CACHE[cdm_name] = rows
