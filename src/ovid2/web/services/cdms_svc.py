"""CDM inspection service (read-only).

Surfaces the CDM registry (:mod:`ovid2.cdm.registry`) to the web tier so
operators can see which Common Data Models Ovid2 knows about, what
tables they carry, how each table is dedup-keyed, which columns are
identifier roles, and which bounded valuesets are registered.

The service is **read-only** by design. CDM authoring flows through the
YAML tree under ``src/ovid2/resources/cdms/`` and the
``ovid2 tools import-cdm`` regenerator — that is the C1 contract. This
module therefore imports :mod:`ovid2.cdm.registry` and
:mod:`ovid2.cdm.yaml_loader` and does nothing else that touches disk.

Web tier isolation: this module does not import pyspark, does not build
any Spark session, and does not read pipeline packages. It calls only
lightweight registry accessors that were populated at import time.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ovid2.web.models import (
    CdmDetail,
    CdmIdColumn,
    CdmSharedView,
    CdmSummary,
    CdmTableSummary,
    CdmValuesetSummary,
)

if TYPE_CHECKING:
    from ovid2.cdm.handle import CdmHandle

logger = logging.getLogger(__name__)

__all__ = [
    "get_cdm_detail",
    "list_cdms",
]


def _has_yaml(name: str) -> tuple[bool, str | None]:
    """Return ``(has_yaml, yaml_dir_str)`` for ``name``.

    Uses :func:`ovid2.cdm.yaml_loader.cdm_yaml_dir` — that helper raises
    :class:`~ovid2.cdm.yaml_loader.CdmYamlError` when the YAML tree is
    absent (pipeline-family CDMs still declared in Python). We treat
    that as a soft "no YAML" and return ``(False, None)``.
    """
    from ovid2.cdm.yaml_loader import CdmYamlError, cdm_yaml_dir

    try:
        directory = cdm_yaml_dir(name)
    except CdmYamlError:
        return False, None
    return True, str(directory)


def list_cdms() -> list[CdmSummary]:
    """Return every CDM registered in :mod:`ovid2.cdm.registry`.

    Sorted alphabetically by name. Mirrors the JSON shape returned by
    ``GET /api/v1/cdms`` so the two callers share a single Pydantic
    model.
    """
    # Local imports keep the module-level import graph light and let
    # the web app boot even when the CDM package has an unrelated
    # regression at import time (log-and-continue).
    from ovid2.cdm import registered_cdms
    from ovid2.cdm.registry import get_cdm

    out: list[CdmSummary] = []
    for name in registered_cdms():
        try:
            handle = get_cdm(name)
        except ValueError as exc:
            # Should not happen — registered_cdms enumerates the same
            # dict get_cdm reads — but degrade to a warning rather
            # than 500-ing the whole page.
            logger.warning("CDM '%s' missing from registry: %s", name, exc)
            continue
        has_yaml, _ = _has_yaml(name)
        out.append(
            CdmSummary(
                name=handle.name,
                version=handle.version,
                table_count=len(handle.composite_keys),
                has_yaml=has_yaml,
            )
        )
    return out


def _table_rows(handle: CdmHandle) -> list[CdmTableSummary]:
    """Flatten ``handle.composite_keys`` into API rows."""
    reference = set(handle.reference_tables)
    rows: list[CdmTableSummary] = []
    for table, key in handle.composite_keys.items():
        rows.append(
            CdmTableSummary(
                name=table,
                composite_key=list(key),
                is_reference=table in reference,
            )
        )
    return rows


def _id_column_rows(handle: CdmHandle) -> list[CdmIdColumn]:
    """Flatten ``handle.id_columns`` into API rows, sorted by role."""
    rows: list[CdmIdColumn] = []
    for role in sorted(handle.id_columns.keys()):
        entry = handle.id_columns[role]
        rows.append(
            CdmIdColumn(
                role=entry.role,
                column=entry.column,
                width=entry.width,
                reference_table=entry.reference_table,
            )
        )
    return rows


def _shared_view_rows(handle: CdmHandle) -> list[CdmSharedView]:
    """Flatten ``handle.shared_views`` into API rows."""
    return [
        CdmSharedView(
            view_name=view.view_name,
            source_path=str(view.source_path),
            delimiter=view.delimiter,
            header=view.header,
            required=view.required,
        )
        for view in handle.shared_views
    ]


def _valueset_rows(handle: CdmHandle) -> list[CdmValuesetSummary]:
    """Flatten ``handle.valuesets`` into API rows.

    Sorted by ``(table, column)`` for stable output; the caller does
    not need the actual code lists (they can hit the semantic layer for
    that).
    """
    rows: list[CdmValuesetSummary] = []
    for table in sorted(handle.valuesets.keys()):
        by_column = handle.valuesets[table]
        for column in sorted(by_column.keys()):
            rows.append(
                CdmValuesetSummary(
                    table=table,
                    column=column,
                    code_count=len(by_column[column]),
                )
            )
    return rows


def _flavour_of_null_list(handle: CdmHandle) -> list[str | None]:
    """Return ``flavour_of_null`` as a stable JSON-friendly list.

    ``None`` sorts last; string sentinels are alphabetically ordered.
    Keeping ``None`` explicit (not filtered out) is important —
    downstream renderers show it as ``(null)`` and operators need to
    see whether the CDM treats database-NULL as an empty-value
    sentinel.
    """
    strings = sorted(v for v in handle.flavour_of_null if v is not None)
    has_none = None in handle.flavour_of_null
    result: list[str | None] = list(strings)
    if has_none:
        result.append(None)
    return result


def get_cdm_detail(name: str) -> CdmDetail | None:
    """Return the full detail for ``name``, or ``None`` when unknown."""
    from ovid2.cdm.registry import get_cdm

    try:
        handle = get_cdm(name)
    except ValueError:
        return None

    has_yaml, yaml_dir = _has_yaml(name)
    return CdmDetail(
        name=handle.name,
        version=handle.version,
        has_yaml=has_yaml,
        yaml_dir=yaml_dir,
        tables=_table_rows(handle),
        reference_tables=sorted(handle.reference_tables),
        id_columns=_id_column_rows(handle),
        flavour_of_null=_flavour_of_null_list(handle),
        shared_views=_shared_view_rows(handle),
        valuesets=_valueset_rows(handle),
    )


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import json

    for row in list_cdms():
        print(json.dumps(row.model_dump(), sort_keys=True))  # noqa: T201 — __main__ smoke test
