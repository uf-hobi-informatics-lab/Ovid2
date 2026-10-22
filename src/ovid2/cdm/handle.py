"""CDM handle — the frozen contract every CDM in the registry must satisfy.

A :class:`CdmHandle` bundles everything the generic ETL engine needs to
know about a Common Data Model:

- **Structural metadata**: table list, composite keys per table, ordered
  dependency for processing.
- **Semantic metadata**: which columns are "identifier" roles (patient,
  encounter, provider, …) and their harmonised widths, which sentinel
  values act as flavour-of-null.
- **Runtime cues**: which finished tables should be cached after their
  per-table pipeline runs, which auxiliary tables must be loaded as
  Spark temp views once per session.
- **Schema access**: a callable that returns the ordered list of column
  names (and optionally full :class:`~ovid2.schema.cdm.CdmField` rows)
  for any table.

The engine reads a handle from :func:`ovid2.cdm.registry.get_cdm` — it
never imports a CDM module directly. That is the entire boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ovid2.schema.cdm import CdmField


@dataclass(frozen=True)
class IdColumn:
    """One identifier column in the CDM.

    Attributes:
        role: Semantic role, e.g. ``'patient'``, ``'encounter'``,
            ``'provider'``. The generic ``orphans`` fixer uses this
            to look up the column name from a pipeline's CDM handle.
        column: Actual column name in the CDM (e.g. ``'PATID'``,
            ``'PERSON_ID'``).
        width: VARCHAR width for the hashed representation stored on
            the warehouse. Fed into the Snowflake DDL generator.
        reference_table: Table where this ID is authoritative
            (``'demographic'`` for PATID in PCORnet). ``None`` when the
            ID has no single owning table.
    """

    role: str
    column: str
    width: int = 64
    reference_table: str | None = None


@dataclass(frozen=True)
class SharedViewSpec:
    """A dataset to register as an engine temp view once per session.

    A shared view is sourced either from a **file** (a CSV on the
    filesystem, read directly by Spark) or from a named **address**
    (resolved through :func:`ovid2.drivers.registry.driver_from_address`
    and materialised through the active engine's stager). The address
    form lets the OMOP CONCEPT / CONCEPT_RELATIONSHIP vocabularies live
    in a database (e.g. a Snowflake table) so a Snowpark run never has to
    round-trip a multi-GB CSV through the JVM-less session.

    Attributes:
        view_name: Temp-view name to expose to the engine's SQL.
        source_path: Path to the CSV when ``source_kind == 'file'``;
            ``None`` for the address form.
        delimiter: Column separator. Default TAB matches OMOP vocab
            CSVs and most Ovid material files. Ignored for addresses.
        header: Whether the file has a header row. Ignored for addresses.
        required: When ``True``, a missing source raises; when ``False``,
            it emits a warning and continues.
        source_kind: ``'file'`` (default) or ``'address'``.
        address: Address config name (filename stem) when
            ``source_kind == 'address'``; ``None`` for the file form.
    """

    view_name: str
    source_path: Path | None = None
    delimiter: str = "\t"
    header: bool = True
    required: bool = False
    source_kind: str = "file"
    address: str | None = None


@dataclass(frozen=True)
class CdmHandle:
    """Everything the ETL engine needs to know about a CDM.

    Instances are immutable and shared across a process. Build via
    :func:`ovid2.cdm.registry.register` and retrieve via
    :func:`ovid2.cdm.registry.get_cdm`.

    Attributes:
        name: Canonical CDM identifier (e.g. ``'pcornet_7_0'``,
            ``'omop_5_3'``, ``'ovid2_pipeline_veritas_v1'``).
        version: Human-readable version tag, informational only.
        composite_keys: Table → ordered list of key columns. Dict
            insertion order is the canonical processing order — earlier
            tables run first when ``--tables all`` is used.
        reference_tables: Tables to cache with ``persist()`` after
            their per-table pipeline completes so downstream fixers
            hit the cache instead of re-executing the plan.
        id_columns: Role → :class:`IdColumn`. Enables the generic
            ``orphans`` fixer and Snowflake width lookups.
        flavour_of_null: Sentinel values that CDM consumers treat as
            "null". PCORnet uses ``{'NI', 'OT', 'UN', '', None}``.
        shared_views: Views to register once per Spark session (e.g.
            OMOP CONCEPT + CONCEPT_RELATIONSHIP).
        schema_source: Optional path to a machine-readable schema CSV.
            When set, :meth:`load_table_columns` and
            :meth:`load_table_fields` read from it.
        schema_loader: Optional callable ``(cdm_name, table) -> list[CdmField]``
            used when ``schema_source`` alone is not sufficient (e.g.
            OMOP where columns live in a different CSV format).
        valuesets: Fast-path mirror of the bounded valuesets registered
            through the semantic layer. ``{table: {column: (code, ...)}}``.
            Populated by :func:`ovid2.cdm.yaml_loader.load_cdm` at import
            time so fixers can do O(1) membership tests without going
            through :mod:`ovid2.semantic.registry`. The semantic registry
            remains the single source of truth for cross-CDM tooling;
            this dict is a read-only convenience.
    """

    name: str
    version: str = ""
    composite_keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    reference_tables: frozenset[str] = field(default_factory=frozenset)
    id_columns: dict[str, IdColumn] = field(default_factory=dict)
    flavour_of_null: frozenset[str | None] = field(default_factory=frozenset)
    shared_views: tuple[SharedViewSpec, ...] = ()
    schema_source: Path | None = None
    schema_loader: Callable[[str, str], list["CdmField"]] | None = None
    valuesets: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Lookup helpers                                                     #
    # ------------------------------------------------------------------ #

    def dedup_key_for(self, table: str) -> tuple[str, ...]:
        """Return the composite key for ``table``.

        Raises:
            ValueError: If ``table`` is not registered in this CDM.
        """
        if table not in self.composite_keys:
            raise ValueError(f"CDM '{self.name}' has no composite key for table '{table}'.")
        return self.composite_keys[table]

    def id_column_for(self, role: str) -> IdColumn:
        """Return the :class:`IdColumn` for ``role`` (e.g. ``'patient'``)."""
        if role not in self.id_columns:
            raise ValueError(f"CDM '{self.name}' has no id column for role '{role}'.")
        return self.id_columns[role]

    def valueset_for(self, table: str, column: str) -> tuple[str, ...] | None:
        """Return the bounded valueset for ``(table, column)``, or ``None``.

        Fast-path membership check for fixers and mappers that need to
        validate a value against a CDM-declared enum (SEX, DX_TYPE, …)
        without going through :mod:`ovid2.semantic.registry`.
        """
        return self.valuesets.get(table, {}).get(column)

    def tables(self) -> list[str]:
        """Return the ordered list of tables in this CDM."""
        return list(self.composite_keys.keys())

    def load_table_columns(self, table: str) -> list[str]:
        """Return the ordered column list for ``table``.

        Delegates to :func:`ovid2.schema.cdm.load_cdm_table_columns`
        when a ``schema_source`` is set. Returns ``[]`` when the CDM
        has no bundled schema (e.g. pipeline-family CDMs).
        """
        if self.schema_loader is not None:
            fields = self.schema_loader(self.name, table)
            return [f.name for f in fields]
        if self.schema_source is None:
            return []
        from ovid2.schema.cdm import load_cdm_table_columns  # local import: avoids cycle

        return load_cdm_table_columns(f"{self.name}.{table}")

    def load_table_fields(self, table: str) -> list["CdmField"]:
        """Return the full :class:`~ovid2.schema.cdm.CdmField` rows for ``table``."""
        if self.schema_loader is not None:
            return self.schema_loader(self.name, table)
        if self.schema_source is None:
            return []
        from ovid2.schema.cdm import load_cdm_full  # local import: avoids cycle

        return load_cdm_full(f"{self.name}.{table}")
