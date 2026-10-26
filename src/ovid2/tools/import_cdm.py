"""Import a CDM spec from a machine-readable source into Ovid2 YAML.

Currently supports the PCORnet CDM v7.0 xlsx workbook shipped by
PCORnet's data-model project. The workbook has six sheets:

- ``INFO``        — provenance metadata (file date, cdm version).
- ``DOMAINS``     — one row per table, ordered by ``DOMAIN_ORDER``.
- ``RELATIONAL``  — primary / composite / foreign key declarations.
- ``CONSTRAINTS`` — per-field constraints (unique, not-null, …).
- ``FIELDS``      — the main schema: TABLE_NAME, FIELD_NAME, RDBMS_DATA_TYPE, …
- ``VALUESETS``   — bounded enums (SEX=A/F/M/…, DX_TYPE=09/10/…).

Usage::

    ovid2 tools import-cdm \\
        --source /path/to/…v7dot0_parseable.xlsx \\
        --cdm pcornet_7_0 \\
        --out-dir src/ovid2/resources/cdms/pcornet_7_0/

Produces:

- ``<out-dir>/cdm.yaml``                       (handle-level metadata)
- ``<out-dir>/tables/<table>.yaml``            (one per DOMAINS row)
- ``<out-dir>/valuesets/<table>.<field>.yaml`` (one per VALUESET=YES field)

The importer is **conservative**: existing curated fields at the top
of ``cdm.yaml`` (``id_columns``, ``reference_tables``, ``flavour_of_null``,
``shared_views``) are preserved when the file already exists; only the
mechanical sections (``tables``, ``source``, ``version``) are overwritten.
Per-table and per-valueset files are rewritten fully — those are pure
copies of the source spec.

The tool is dev-only: not exposed on the REST API or MCP because CDM
import is a rare, developer-time operation. It runs in dry-run mode
by default; pass ``--write`` to persist.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, SupportsInt, cast

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FieldRow:
    """One row of the FIELDS sheet."""

    table: str
    name: str
    order: int | None
    rdbms_type: str
    sas_type: str | None
    data_format: str | None
    replicated: bool
    unit_of_measure: str | None
    valueset: bool
    definition: str | None


@dataclass(frozen=True)
class _ValuesetRow:
    """One row of the VALUESETS sheet."""

    table: str
    field: str
    code: str
    label: str | None
    order: int | None


@dataclass(frozen=True)
class _RelationRow:
    """One row of the RELATIONAL sheet."""

    table: str
    relation: str
    details: str | None
    order: int | None


@dataclass(frozen=True)
class _ConstraintRow:
    """One row of the CONSTRAINTS sheet."""

    table: str
    field: str
    rule: str
    order: int | None


@dataclass(frozen=True)
class _DomainRow:
    """One row of the DOMAINS sheet."""

    table: str
    description: str | None
    order: int | None


@dataclass
class _ImportedSpec:
    """Everything the importer parses from an xlsx source."""

    cdm_name: str
    source_version: str = ""
    imported_from: str = ""
    domains: list[_DomainRow] = field(default_factory=list)
    fields: list[_FieldRow] = field(default_factory=list)
    valuesets: list[_ValuesetRow] = field(default_factory=list)
    relations: list[_RelationRow] = field(default_factory=list)
    constraints: list[_ConstraintRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# xlsx reader
# ---------------------------------------------------------------------------


def _cell_str(value: object) -> str | None:
    """Coerce a cell value to a stripped string or ``None`` when empty."""
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _cell_int(value: object) -> int | None:
    """Coerce a cell value to an ``int`` where possible."""
    if value is None or value == "":
        return None
    try:
        return int(cast("SupportsInt | str", value))
    except (TypeError, ValueError):
        return None


def _cell_bool_yn(value: object) -> bool:
    """Coerce a cell value that looks like ``YES``/``NO`` to a bool."""
    if value is None:
        return False
    return str(value).strip().upper() == "YES"


def _read_xlsx(source: Path, cdm_name: str) -> _ImportedSpec:
    """Parse an xlsx workbook into an :class:`_ImportedSpec`."""
    # Local import — openpyxl is only needed at import time and is a
    # relatively heavy dependency. Keeping it here means the module is
    # cheap to import for the top-level `ovid2 --help`.
    from openpyxl import load_workbook

    wb = load_workbook(source, data_only=True, read_only=True)
    try:
        spec = _ImportedSpec(cdm_name=cdm_name, imported_from=str(source))

        # ---- INFO ---------------------------------------------------
        if "INFO" in wb.sheetnames:
            ws = wb["INFO"]
            rows = list(ws.iter_rows(min_row=2, values_only=True))
            if rows:
                # Take the newest row's CDM_VERSION as the source version.
                for row in reversed(rows):
                    version = _cell_str(row[1] if len(row) > 1 else None)
                    if version:
                        spec.source_version = version
                        break

        # ---- DOMAINS ------------------------------------------------
        if "DOMAINS" in wb.sheetnames:
            ws = wb["DOMAINS"]
            headers = _header_map(ws)
            for row in ws.iter_rows(min_row=2, values_only=True):
                table = _cell_str(row[headers["TABLE_NAME"] - 1])
                if not table:
                    continue
                spec.domains.append(
                    _DomainRow(
                        table=table.lower(),
                        description=_cell_str(row[headers["DOMAIN_DESCRIPTION"] - 1]),
                        order=_cell_int(row[headers["DOMAIN_ORDER"] - 1]),
                    )
                )

        # ---- FIELDS -------------------------------------------------
        if "FIELDS" in wb.sheetnames:
            ws = wb["FIELDS"]
            headers = _header_map(ws)
            for row in ws.iter_rows(min_row=2, values_only=True):
                table = _cell_str(row[headers["TABLE_NAME"] - 1])
                name = _cell_str(row[headers["FIELD_NAME"] - 1])
                if not table or not name:
                    continue
                spec.fields.append(
                    _FieldRow(
                        table=table.lower(),
                        name=name.upper(),
                        order=_cell_int(row[headers["FIELD_ORDER"] - 1]),
                        rdbms_type=_cell_str(row[headers["RDBMS_DATA_TYPE"] - 1]) or "",
                        sas_type=_cell_str(row[headers["SAS_DATA_TYPE"] - 1]),
                        data_format=_cell_str(row[headers.get("DATA_FORMAT", 0) - 1])
                        if "DATA_FORMAT" in headers
                        else None,
                        replicated=_cell_bool_yn(
                            row[headers["REPLICATED_FIELD"] - 1]
                            if "REPLICATED_FIELD" in headers
                            else None
                        ),
                        unit_of_measure=_cell_str(
                            row[headers["UNIT_OF_MEASURE"] - 1]
                            if "UNIT_OF_MEASURE" in headers
                            else None
                        ),
                        valueset=_cell_bool_yn(
                            row[headers["VALUESET"] - 1] if "VALUESET" in headers else None
                        ),
                        definition=_cell_str(
                            row[headers["FIELD_DEFINITION"] - 1]
                            if "FIELD_DEFINITION" in headers
                            else None
                        ),
                    )
                )

        # ---- VALUESETS ---------------------------------------------
        if "VALUESETS" in wb.sheetnames:
            ws = wb["VALUESETS"]
            headers = _header_map(ws)
            for row in ws.iter_rows(min_row=2, values_only=True):
                table = _cell_str(row[headers["TABLE_NAME"] - 1])
                field_name = _cell_str(row[headers["FIELD_NAME"] - 1])
                code = _cell_str(row[headers["VALUESET_ITEM"] - 1])
                if not table or not field_name or code is None:
                    continue
                spec.valuesets.append(
                    _ValuesetRow(
                        table=table.lower(),
                        field=field_name.upper(),
                        code=code,
                        label=_cell_str(
                            row[headers["VALUESET_ITEM_DESCRIPTOR"] - 1]
                            if "VALUESET_ITEM_DESCRIPTOR" in headers
                            else None
                        ),
                        order=_cell_int(
                            row[headers["VALUESET_ITEM_ORDER"] - 1]
                            if "VALUESET_ITEM_ORDER" in headers
                            else None
                        ),
                    )
                )

        # ---- RELATIONAL --------------------------------------------
        if "RELATIONAL" in wb.sheetnames:
            ws = wb["RELATIONAL"]
            headers = _header_map(ws)
            for row in ws.iter_rows(min_row=2, values_only=True):
                table = _cell_str(row[headers["TABLE_NAME"] - 1])
                relation = _cell_str(row[headers["RELATION"] - 1])
                if not table or not relation:
                    continue
                spec.relations.append(
                    _RelationRow(
                        table=table.lower(),
                        relation=relation,
                        details=_cell_str(
                            row[headers["RELATIONAL_INTEGRITY_DETAILS"] - 1]
                            if "RELATIONAL_INTEGRITY_DETAILS" in headers
                            else None
                        ),
                        order=_cell_int(
                            row[headers["RELATION_ORDER"] - 1]
                            if "RELATION_ORDER" in headers
                            else None
                        ),
                    )
                )

        # ---- CONSTRAINTS -------------------------------------------
        if "CONSTRAINTS" in wb.sheetnames:
            ws = wb["CONSTRAINTS"]
            headers = _header_map(ws)
            for row in ws.iter_rows(min_row=2, values_only=True):
                table = _cell_str(row[headers["TABLE_NAME"] - 1])
                field_name = _cell_str(row[headers["FIELD_NAME"] - 1])
                rule = _cell_str(row[headers["CONSTRAINT"] - 1])
                if not table or not field_name or not rule:
                    continue
                spec.constraints.append(
                    _ConstraintRow(
                        table=table.lower(),
                        field=field_name.upper(),
                        rule=rule,
                        order=_cell_int(
                            row[headers["CONSTRAINT_ORDER"] - 1]
                            if "CONSTRAINT_ORDER" in headers
                            else None
                        ),
                    )
                )
    finally:
        wb.close()

    return spec


def _header_map(ws: Any) -> dict[str, int]:
    """Return a ``{header_name: 1-indexed_column}`` dict for a sheet."""
    return {
        str(cell.value).strip(): idx
        for idx, cell in enumerate(ws[1], start=1)
        if cell.value is not None
    }


def _supplement_from_csv(spec: _ImportedSpec, csv_path: Path) -> None:
    """Merge tables from a companion CSV that are missing from the xlsx.

    Only used to backfill PCORnet PRIVATE_ADDRESS_HISTORY and
    PRIVATE_ADDRESS_GEOCODE, which are absent from the 2025-01-23 xlsx
    but declared in ``pcornet_cdm_v70.csv``. Rows for tables already
    covered by the xlsx are ignored — the xlsx wins.
    """
    import csv as _csv

    existing_tables = {row.table for row in spec.fields}
    existing_domains = {d.table for d in spec.domains}
    with csv_path.open(encoding="utf-8", newline="") as fh:
        reader = _csv.DictReader(fh)
        for order_idx, row in enumerate(reader, start=1):
            table = (row.get("TABLE_NAME") or "").strip().lower()
            name = (row.get("FIELD_NAME") or "").strip().upper()
            if not table or not name or table == "table_name":
                continue
            if table in existing_tables:
                continue  # xlsx already covers this table
            spec.fields.append(
                _FieldRow(
                    table=table,
                    name=name,
                    order=order_idx,
                    rdbms_type=(row.get("RDBMS_DATA_TYPE") or "").strip(),
                    sas_type=(row.get("SAS_DATA_TYPE") or "").strip() or None,
                    data_format=None,
                    replicated=False,
                    unit_of_measure=None,
                    valueset=False,
                    definition=None,
                )
            )
            if table not in existing_domains:
                # Synthesise a minimal DOMAINS row so the table appears
                # in `cdm.yaml`'s ordered table list. Order is placed
                # after every xlsx-declared table so processing order
                # stays deterministic.
                spec.domains.append(
                    _DomainRow(
                        table=table,
                        description=f"Supplemented from {csv_path.name}.",
                        order=9000 + len(existing_domains),
                    )
                )
                existing_domains.add(table)


# ---------------------------------------------------------------------------
# Composite-key extraction
# ---------------------------------------------------------------------------


def _composite_key_for(spec: _ImportedSpec, table: str) -> tuple[str, ...]:
    """Return the ordered composite key for ``table``.

    PCORnet declares keys in the RELATIONAL sheet as either
    ``relation='PK'`` with a single field in ``details`` or
    ``relation='Composite Key'`` with ``details='A + B + C'``.
    """
    for row in spec.relations:
        if row.table != table:
            continue
        rel = row.relation.strip().lower()
        if rel not in ("pk", "primary key", "composite key"):
            continue
        details = (row.details or "").strip()
        if not details:
            continue
        parts = [p.strip().upper() for p in details.split("+") if p.strip()]
        if parts:
            return tuple(parts)
    # Fallback: single field named like the id column heuristics.
    return ()


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------


def _render_cdm_yaml(spec: _ImportedSpec, curated: dict[str, Any]) -> dict[str, Any]:
    """Render the root ``cdm.yaml`` payload, preserving curated keys.

    Curated keys (kept as-is when present):

    - ``version`` — hand-authored tag like ``"7.0"``.
    - ``source.origin`` — human-readable origin string.
    - ``reference_tables``, ``flavour_of_null``, ``id_columns``, ``shared_views``.
    - ``tables`` — the ordered list. Preserving it lets operators encode
      dependency order (``demographic`` before ``encounter``, …). Any
      newly-discovered tables from the source spec are appended at the
      end so nothing is silently dropped.

    Mechanical keys (always overwritten):

    - ``source.imported_from`` / ``source.imported_at``.
    - ``v`` (schema version).
    """
    discovered = sorted(
        {row.table for row in spec.domains},
        key=lambda t: _table_order(spec, t),
    )
    curated_tables = curated.get("tables")
    if isinstance(curated_tables, list) and curated_tables:
        # Preserve curated ordering; append any newly-discovered tables.
        tables_ordered = list(curated_tables)
        for t in discovered:
            if t not in tables_ordered:
                tables_ordered.append(t)
    else:
        tables_ordered = discovered

    curated_source = curated.get("source") or {}
    result: dict[str, Any] = {
        "v": 1,
        "name": spec.cdm_name,
        "version": curated.get("version") or spec.source_version,
        "source": {
            "origin": curated_source.get("origin")
            or f"CDM {spec.cdm_name} v{spec.source_version}".strip(),
            "imported_from": spec.imported_from,
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "tables": tables_ordered,
    }
    # Preserve remaining curated keys as-is.
    for key in ("reference_tables", "flavour_of_null", "id_columns", "shared_views"):
        if key in curated:
            result[key] = curated[key]
    return result


def _table_order(spec: _ImportedSpec, table: str) -> int:
    """Return the DOMAIN_ORDER for ``table``, or a large sentinel."""
    for row in spec.domains:
        if row.table == table and row.order is not None:
            return row.order
    return 9999


def _render_table_yaml(spec: _ImportedSpec, table: str) -> dict[str, Any]:
    """Render a single ``tables/<table>.yaml`` payload."""
    domain = next((d for d in spec.domains if d.table == table), None)
    fields_rows = sorted(
        [f for f in spec.fields if f.table == table],
        key=lambda f: (f.order if f.order is not None else 9999, f.name),
    )
    relations = sorted(
        [r for r in spec.relations if r.table == table],
        key=lambda r: (r.order if r.order is not None else 9999, r.relation),
    )
    constraints = sorted(
        [c for c in spec.constraints if c.table == table],
        key=lambda c: (c.order if c.order is not None else 9999, c.field, c.rule),
    )
    composite = _composite_key_for(spec, table)

    payload: dict[str, Any] = {
        "v": 1,
        "name": table,
    }
    if composite:
        payload["composite_key"] = list(composite)
    if domain is not None and domain.description:
        payload["domain_description"] = domain.description
    if domain is not None and domain.order is not None:
        payload["domain_order"] = domain.order
    if constraints:
        payload["constraints"] = [
            {"field": c.field, "rule": c.rule, **({"order": c.order} if c.order else {})}
            for c in constraints
        ]
    if relations:
        payload["relations"] = [
            {
                "relation": r.relation,
                **({"details": r.details} if r.details else {}),
                **({"order": r.order} if r.order else {}),
            }
            for r in relations
        ]

    fields_payload: list[dict[str, Any]] = []
    for f in fields_rows:
        item: dict[str, Any] = {
            "name": f.name,
            "rdbms_type": f.rdbms_type,
        }
        if f.order is not None:
            item["order"] = f.order
        if f.sas_type:
            item["sas_type"] = f.sas_type
        if f.data_format:
            item["data_format"] = f.data_format
        if f.replicated:
            item["replicated"] = True
        if f.unit_of_measure:
            item["unit_of_measure"] = f.unit_of_measure
        if f.valueset:
            item["valueset"] = f"{spec.cdm_name}.{table}.{f.name.lower()}"
        if f.definition:
            item["definition"] = f.definition
        fields_payload.append(item)
    payload["fields"] = fields_payload
    return payload


def _render_valueset_yaml(
    spec: _ImportedSpec, table: str, field_name: str
) -> dict[str, Any] | None:
    """Render one ``valuesets/<table>.<field>.yaml`` payload, or None."""
    rows = sorted(
        [v for v in spec.valuesets if v.table == table and v.field == field_name],
        key=lambda v: (v.order if v.order is not None else 9999, v.code),
    )
    if not rows:
        return None

    items: list[dict[str, Any]] = []
    for v in rows:
        item: dict[str, Any] = {"code": v.code}
        if v.label:
            item["label"] = v.label
        if v.order is not None:
            item["order"] = v.order
        items.append(item)

    return {
        "v": 1,
        "name": f"{spec.cdm_name}.{table}.{field_name.lower()}",
        "kind": "vocabulary",
        "version": spec.source_version,
        "description": f"{spec.cdm_name.upper()} {table.upper()}.{field_name} code set.",
        "code_column": "code",
        "description_column": "label",
        "items": items,
    }


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _dump_yaml(payload: dict[str, Any]) -> str:
    """Serialise ``payload`` to a stable, human-readable YAML string."""
    return yaml.safe_dump(
        payload,
        sort_keys=False,
        allow_unicode=True,
        width=100,
        default_flow_style=False,
    )


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically with mode 0o644."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    import os

    os.chmod(tmp, 0o644)
    tmp.replace(path)


def _load_curated_root(path: Path) -> dict[str, Any]:
    """Read an existing ``cdm.yaml`` and return only its curated keys."""
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover — defensive
        logger.warning("Existing %s is not valid YAML: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    curated_keys = {
        "version",
        "source",
        "tables",
        "reference_tables",
        "flavour_of_null",
        "id_columns",
        "shared_views",
        "composite_key_overrides",
    }
    return {k: v for k, v in raw.items() if k in curated_keys}


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``tools import-cdm`` subcommand."""
    parser = subparsers.add_parser(
        "import-cdm",
        help="Import a CDM spec (xlsx) into Ovid2 YAML.",
        description=__doc__,
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Path to the xlsx workbook (e.g. …v7dot0_parseable.xlsx).",
    )
    parser.add_argument(
        "--supplement-csv",
        default=None,
        help=(
            "Optional companion CSV (TABLE_NAME,FIELD_NAME,RDBMS_DATA_TYPE,"
            "SAS_DATA_TYPE) that fills in tables the xlsx omits. PCORnet 7.0 "
            "ships two tables (PRIVATE_ADDRESS_HISTORY, PRIVATE_ADDRESS_GEOCODE) "
            "only in the CSV — pass its path here to include them."
        ),
    )
    parser.add_argument(
        "--cdm",
        required=True,
        help="CDM name to write (snake_case, e.g. pcornet_7_0).",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Target directory (usually src/ovid2/resources/cdms/<cdm>/).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Actually persist the YAML (default: dry-run, prints a summary).",
    )
    parser.add_argument(
        "--vocab-destination",
        default="inline",
        help=(
            "Where to store valuesets. `inline` (default) writes them as "
            "`valuesets/<t>.<f>.yaml` with `inline_items:` blocks. Named "
            "address values are reserved for a follow-up phase — the tool "
            "prints the intended plan and exits with a warning without "
            "touching the destination address. Rationale: writing to a "
            "live DB from an offline CDM import needs a real target on the "
            "network and is deferred until the operator has a shared "
            "reference schema chosen (see the C1 ADR for the plan)."
        ),
    )
    parser.set_defaults(_entry=main)


def main(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools import-cdm``."""
    source = Path(args.source)
    if not source.is_file():
        print(f"[ovid2 tools import-cdm] source not found: {source}", file=sys.stderr)
        return 2
    out_dir = Path(args.out_dir)

    logger.info("Reading %s …", source)
    spec = _read_xlsx(source, cdm_name=args.cdm)
    if args.supplement_csv:
        csv_path = Path(args.supplement_csv)
        if not csv_path.is_file():
            print(
                f"[ovid2 tools import-cdm] supplement CSV not found: {csv_path}",
                file=sys.stderr,
            )
            return 2
        _supplement_from_csv(spec, csv_path)
    logger.info(
        "Parsed %d tables, %d fields, %d valueset rows, %d relations, %d constraints.",
        len({d.table for d in spec.domains}),
        len(spec.fields),
        len(spec.valuesets),
        len(spec.relations),
        len(spec.constraints),
    )

    curated = _load_curated_root(out_dir / "cdm.yaml")
    cdm_yaml = _render_cdm_yaml(spec, curated)

    table_payloads: dict[str, dict[str, Any]] = {}
    valueset_payloads: dict[str, dict[str, Any]] = {}
    for table in cdm_yaml["tables"]:
        table_payloads[table] = _render_table_yaml(spec, table)
        for f in spec.fields:
            if f.table == table and f.valueset:
                vs = _render_valueset_yaml(spec, table, f.name)
                if vs is not None:
                    valueset_payloads[f"{table}.{f.name.lower()}"] = vs

    print(f"[ovid2 tools import-cdm] cdm={args.cdm} source={source}")
    print(f"  tables:    {len(table_payloads)}")
    print(f"  valuesets: {len(valueset_payloads)}")
    print(f"  target:    {out_dir}")

    vocab_destination = str(getattr(args, "vocab_destination", "inline") or "inline")
    if vocab_destination != "inline":
        print(
            f"  vocab dest: {vocab_destination!r} (deferred — "
            "address-backed valueset materialisation is scheduled for a "
            "follow-up C1 sub-phase; falling back to inline this run.)",
            file=sys.stderr,
        )
        vocab_destination = "inline"

    if not args.write:
        print("\nDry-run — pass --write to persist. Sample of cdm.yaml:")
        print(_dump_yaml(cdm_yaml))
        return 0

    _write_atomic(out_dir / "cdm.yaml", _dump_yaml(cdm_yaml))
    print(f"[write] {out_dir / 'cdm.yaml'}")

    tables_dir = out_dir / "tables"
    for table, payload in table_payloads.items():
        _write_atomic(tables_dir / f"{table}.yaml", _dump_yaml(payload))
    print(f"[write] {tables_dir}/*.yaml  ({len(table_payloads)} file(s))")

    valuesets_dir = out_dir / "valuesets"
    for stem, payload in valueset_payloads.items():
        _write_atomic(valuesets_dir / f"{stem}.yaml", _dump_yaml(payload))
    print(f"[write] {valuesets_dir}/*.yaml  ({len(valueset_payloads)} file(s))")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="import-cdm")
    subs = parser.add_subparsers(dest="_command", metavar="COMMAND", required=True)
    register(subs)
    ns = parser.parse_args()
    sys.exit(main(ns))
