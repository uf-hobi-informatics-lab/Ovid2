"""CDM schema access (PCORnet CDM v7.0, OMOP CDM 5.3).

Two entry points:

- :func:`load_cdm_table_columns` — read the ordered column list for a
  declared CDM table (e.g. ``pcornet_7_0.CONDITION``). Used by
  :mod:`ovid2.formatters.engine` to validate the ``output_columns_from:``
  block of a spec.
- :func:`load_cdm_full` — read the full rows (name + type + description)
  for building a Snowflake-typed schema. Used by the eventual Snowflake
  address port (P5).

Snowflake DDL generation for the address writer:

- :func:`build_snowflake_types_ddl` — turn ``load_cdm_full`` output into
  the DDL fragment the Spark-Snowflake connector's
  ``createTableColumnTypes`` option expects
  (``"COL1 VARCHAR(64), COL2 DATE, COL3 NUMBER(38,0)"``). Widths use the
  harmonized-ID map first, then the declared ``RDBMS Text(N)`` size, then
  a generic default. No data scan happens here — pure metadata.

Data-source priority (phase C1):

1. **YAML** under ``resources/cdms/<cdm>/tables/*.yaml``, populated by
   :func:`ovid2.cdm.yaml_loader.load_cdm` at CDM import time.
2. **Machine-readable CSV** (``pcornet_cdm_v70.csv``) shipped under
   :data:`ovid2.config.settings.PCORNET_CDM_DIR`. Retained as a fallback
   for one release so pipelines that ship a CDM without a YAML directory
   keep working.

The reader is pure-Python (no Spark dependency) so it is safe to call
from contract tests and the installer.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ovid2.config.settings import HARMONIZED_FIELD_SIZES, PCORNET_CDM_CSV_PATH

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CdmField:
    """One CDM column."""

    table: str
    name: str
    rdbms_type: str


_CDM_CACHE: dict[str, list[CdmField]] = {}

#: Fallback ``VARCHAR`` width for ``RDBMS Text(x)`` columns that are not
#: in :data:`ovid2.config.settings.HARMONIZED_FIELD_SIZES`. Legacy Ovid
#: used 256 for these when the data scan was skipped.
_DEFAULT_VARCHAR_WIDTH: int = 256

_TEXT_WIDTH_RE = re.compile(r"RDBMS\s+Text\s*\(\s*(\d+)\s*\)", re.IGNORECASE)


def _cdm_source_path(cdm_ref: str) -> Path:
    """Return the CSV path for a CDM reference like ``pcornet_7_0.CONDITION``.

    Resolution consults :mod:`ovid2.cdm.registry` first (P10). Falls back
    to the bundled PCORnet CSV so this module keeps working when the
    registry has not yet been imported (contract tests, tooling).
    """
    cdm_name, _, _ = cdm_ref.partition(".")

    # Consult the CDM registry when it is available.
    try:
        from ovid2.cdm.registry import get_cdm  # local import: avoids import cycle

        handle = get_cdm(cdm_name)
    except (ImportError, ValueError):
        handle = None

    if handle is not None and handle.schema_source is not None:
        return handle.schema_source

    if cdm_name == "pcornet_7_0":
        return PCORNET_CDM_CSV_PATH
    raise ValueError(
        f"Unknown CDM reference '{cdm_ref}'. Registered CDMs must expose 'schema_source'."
    )


def _load_all(cdm_name: str) -> list[CdmField]:
    """Load and cache every row for ``cdm_name``.

    Priority (phase C1):

    1. In-process cache (populated by :mod:`ovid2.cdm.yaml_loader`).
    2. YAML directory under ``resources/cdms/<cdm_name>/tables/*.yaml``.
    3. Machine-readable CSV via :func:`_cdm_source_path`.
    """
    if cdm_name in _CDM_CACHE:
        return _CDM_CACHE[cdm_name]

    # Try YAML first — matches the phase-C1 layout.
    try:
        from ovid2.cdm.yaml_loader import (  # local: avoids cycle at module load
            CdmYamlError,
            cdm_yaml_dir,
            load_table_fields_from_yaml,
        )

        try:
            directory = cdm_yaml_dir(cdm_name)
        except CdmYamlError:
            directory = None
        if directory is not None:
            rows: list[CdmField] = []
            root_yaml = directory / "cdm.yaml"
            if root_yaml.is_file():
                import yaml

                with root_yaml.open(encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                tables = raw.get("tables") or []
                for table in tables:
                    rows.extend(load_table_fields_from_yaml(cdm_name, str(table)))
            if rows:
                _CDM_CACHE[cdm_name] = rows
                return _CDM_CACHE[cdm_name]
    except Exception as exc:
        logger.debug("YAML fallback for %s: %s", cdm_name, exc)

    # Fallback: the CSV that shipped with Ovid pre-C1.
    try:
        path = _cdm_source_path(f"{cdm_name}.PLACEHOLDER")
    except ValueError:
        _CDM_CACHE[cdm_name] = []
        return _CDM_CACHE[cdm_name]
    if not path.is_file():
        logger.warning("CDM CSV not found at %s; returning empty schema.", path)
        _CDM_CACHE[cdm_name] = []
        return _CDM_CACHE[cdm_name]

    rows = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            table = (row.get("TABLE_NAME") or "").strip()
            name = (row.get("FIELD_NAME") or "").strip()
            rdbms_type = (row.get("RDBMS_DATA_TYPE") or "").strip()
            if not table or not name:
                continue
            rows.append(CdmField(table=table.upper(), name=name.upper(), rdbms_type=rdbms_type))
    _CDM_CACHE[cdm_name] = rows
    return rows


def load_cdm_table_columns(cdm_ref: str) -> list[str]:
    """Return the ordered column list for a CDM table.

    Args:
        cdm_ref: ``<cdm_name>.<TABLE>``, e.g. ``pcornet_7_0.CONDITION``.

    Returns:
        Ordered list of uppercase column names, or ``[]`` when the CDM
        CSV is not available on disk (developer laptops without material/).
    """
    cdm_name, _, table_name = cdm_ref.partition(".")
    if not table_name:
        raise ValueError(f"CDM reference must be `<cdm>.<TABLE>`, got '{cdm_ref}'.")
    return [field.name for field in _load_all(cdm_name) if field.table == table_name.upper()]


def load_cdm_full(cdm_ref: str) -> list[CdmField]:
    """Return the full :class:`CdmField` rows for a CDM table."""
    cdm_name, _, table_name = cdm_ref.partition(".")
    if not table_name:
        raise ValueError(f"CDM reference must be `<cdm>.<TABLE>`, got '{cdm_ref}'.")
    return [f for f in _load_all(cdm_name) if f.table == table_name.upper()]


def clear_cache() -> None:
    """Drop the module-level CDM cache. Test-only."""
    _CDM_CACHE.clear()


# ---------------------------------------------------------------------------
# Snowflake DDL generation (harmonized widths — no data scan)
# ---------------------------------------------------------------------------


def _snowflake_type_for(field: CdmField) -> str:
    """Translate one ``CdmField`` into a Snowflake DDL type fragment.

    Widths for ``RDBMS Text(x)`` are taken from
    :data:`ovid2.config.settings.HARMONIZED_FIELD_SIZES` (matches legacy
    Ovid's harmonized ID sizes for ``PATID``, ``ENCOUNTERID`` etc.) with
    :data:`_DEFAULT_VARCHAR_WIDTH` as a fallback. ``RDBMS Text(N)``
    columns use the declared ``N``. ``RDBMS Date`` maps to ``DATE`` and
    ``RDBMS Number(...)`` maps to ``NUMBER(38,10)`` (Snowflake's
    high-precision default that comfortably fits the legacy ``DOUBLE``
    range without losing digits).

    Args:
        field: A single row from :func:`load_cdm_full`.

    Returns:
        A Snowflake type fragment like ``VARCHAR(64)`` or ``DATE``.
    """
    rdbms = (field.rdbms_type or "").strip()
    # Collapse internal whitespace so hand-authored variants like
    # ``RDBMS Text (x)`` (note the space before the paren, present on a
    # handful of PCORnet fields such as ``IMMUNIZATION.VX_BODY_SITE``)
    # are treated identically to the canonical ``RDBMS Text(x)``. Without
    # this the space-form fell through to the generic ``VARCHAR(256)``
    # branch below and lost harmonized-ID sizing.
    lower = re.sub(r"\s*\(\s*", "(", rdbms.lower())

    if lower.startswith("rdbms date"):
        return "DATE"

    if lower.startswith("rdbms number"):
        return "NUMBER(38,10)"

    match = _TEXT_WIDTH_RE.search(rdbms)
    if match:
        return f"VARCHAR({int(match.group(1))})"

    if "text(x)" in lower or lower == "rdbms text":
        width = HARMONIZED_FIELD_SIZES.get(field.name.upper(), _DEFAULT_VARCHAR_WIDTH)
        return f"VARCHAR({width})"

    # Unknown type — fall back to a wide VARCHAR so the write doesn't
    # silently drop data. Logged so operators notice.
    logger.warning(
        "Unrecognised CDM RDBMS_DATA_TYPE %r for %s.%s — defaulting to VARCHAR(%d).",
        rdbms,
        field.table,
        field.name,
        _DEFAULT_VARCHAR_WIDTH,
    )
    return f"VARCHAR({_DEFAULT_VARCHAR_WIDTH})"


def build_snowflake_types_ddl(cdm_ref: str) -> str:
    """Return the DDL fragment for the Spark-Snowflake ``createTableColumnTypes``.

    Args:
        cdm_ref: ``<cdm>.<TABLE>`` reference, e.g. ``pcornet_7_0.HASH_TOKEN``.

    Returns:
        A comma-separated DDL fragment like
        ``"PATID VARCHAR(64), ENCOUNTERID VARCHAR(128), ..."``.
        Empty string when the referenced table has no rows in the CSV
        (developer laptops without ``material/`` — the writer will fall
        back to Spark's default type inference and log a warning).
    """
    fields = load_cdm_full(cdm_ref)
    if not fields:
        return ""
    return ", ".join(f"{f.name} {_snowflake_type_for(f)}" for f in fields)


def build_snowflake_typed_columns(cdm_ref: str) -> list[tuple[str, str]]:
    """Return ordered ``(COLUMN, snowflake_type)`` pairs for a CDM table.

    The list-of-tuples counterpart of :func:`build_snowflake_types_ddl`,
    for callers (notably the Snowpark native write strategy) that need to
    build a ``CREATE OR REPLACE TABLE`` statement *and* per-column casts
    rather than a single Spark-connector ``createTableColumnTypes``
    fragment. Column order matches the CDM's declared field order.

    Args:
        cdm_ref: ``<cdm>.<TABLE>`` reference, e.g. ``omop_5_3.MEASUREMENT``.

    Returns:
        ``[(name, snowflake_type), …]`` — e.g.
        ``[("MEASUREMENT_ID", "NUMBER(38,10)"), ("MEASUREMENT_DATE", "DATE"), …]``.
        Empty list when the referenced table is unknown (developer
        laptops without ``material/``); callers should fall back to
        untyped behaviour and log a warning.
    """
    fields = load_cdm_full(cdm_ref)
    if not fields:
        return []
    return [(f.name, _snowflake_type_for(f)) for f in fields]


def build_snowflake_write_schema(cdm_ref: str) -> list[tuple[str, str, int]]:
    """Return per-column ``(name, kind, width)`` triples for the Spark writer.

    The Spark->Snowflake connector (``net.snowflake.spark.snowflake``) does
    **not** honour the generic ``createTableColumnTypes`` DataSource option
    -- it derives the ``CREATE TABLE`` DDL from the DataFrame's own schema.
    A ``StringType`` column therefore becomes ``VARCHAR(16777216)`` unless
    the field carries a ``metadata={"maxlength": N}`` hint (and ``DATE`` /
    ``NUMBER`` require the matching Spark type). This helper returns the
    information :meth:`ovid2.drivers.snowflake.spark.SnowflakeAddress.write`
    needs to rebuild the outgoing DataFrame with a correctly-typed schema,
    mirroring legacy Ovid's ``get_sf_schema`` + ``createDataFrame`` path.

    The schema module stays pyspark-free: it returns primitives and the
    (pyspark-importing) writer maps ``kind`` to the concrete Spark type.

    Args:
        cdm_ref: ``<cdm>.<TABLE>`` reference, e.g. ``pcornet_7_0.DEATH``.

    Returns:
        Ordered ``[(name, kind, width), ...]`` where ``kind`` is one of
        ``"varchar"``, ``"date"`` or ``"number"`` and ``width`` is the
        VARCHAR character width (``0`` for non-VARCHAR kinds). Column order
        matches the CDM's declared field order. Empty list when the table
        is unknown (developer laptops without ``material/``); the writer
        falls back to letting the connector infer types and logs a warning.
    """
    fields = load_cdm_full(cdm_ref)
    if not fields:
        return []
    result: list[tuple[str, str, int]] = []
    for field in fields:
        sf_type = _snowflake_type_for(field)
        upper = sf_type.upper()
        if upper.startswith("DATE"):
            result.append((field.name, "date", 0))
        elif upper.startswith(("NUMBER", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC", "INT")):
            result.append((field.name, "number", 0))
        else:
            match = re.search(r"\((\d+)\)", sf_type)
            width = int(match.group(1)) if match else _DEFAULT_VARCHAR_WIDTH
            result.append((field.name, "varchar", width))
    return result
