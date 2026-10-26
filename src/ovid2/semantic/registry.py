"""Semantic registry — one central, kind-keyed catalogue.

Any semantic handle Ovid2 knows about must call :func:`register` at
import time. Consumers read handles via :func:`get` (single lookup) or
:func:`registered` (list a kind).

The registry deliberately mirrors :mod:`ovid2.cdm.registry` so future
semantic kinds (metrics, catalog entries) reuse the same lifecycle,
CLI surface, and test scaffolding.

Column bindings live here too. A :class:`ColumnBinding` attaches a
vocabulary (or valueset) to a specific ``(cdm, table, column)`` tuple
without modifying :class:`ovid2.cdm.handle.CdmHandle`. Bindings are
registered separately so the CDM package stays free of vocabulary
imports (preserves P10 CDM/ETL independence).
"""

from __future__ import annotations

from typing import Final

from ovid2.semantic.handle import SemanticHandle, VocabularyHandle

# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------

#: kind -> name -> handle. Kind is the string discriminator on
#: :class:`~ovid2.semantic.handle.SemanticHandle` subclasses.
_REGISTRY: Final[dict[str, dict[str, SemanticHandle]]] = {}

#: (cdm_name, table, column) -> ColumnBinding.
_BINDINGS: Final[dict[tuple[str, str, str], ColumnBinding]] = {}


# ---------------------------------------------------------------------------
# Column bindings
# ---------------------------------------------------------------------------


from dataclasses import dataclass  # noqa: E402  — kept local to make imports obvious.


@dataclass(frozen=True)
class ColumnBinding:
    """Attach a vocabulary or valueset to a CDM column.

    Bindings enable downstream tooling (``translate_by_concept`` mapper
    op, ``semantic_join`` adapter, ``SemanticCoverageTransformer``) to
    discover *what a column means* without hard-coding the mapping.

    Attributes:
        cdm: CDM name the binding applies to (e.g. ``'pcornet_7_0'``).
        table: Table name in the CDM (e.g. ``'diagnosis'``).
        column: Column name (uppercase per Ovid2 convention).
        vocabulary: Registered :class:`VocabularyHandle` name.
        role: Optional semantic role such as ``'diagnosis_code'`` or
            ``'lab_result_loinc'``. Analogous to
            :class:`ovid2.cdm.handle.IdColumn.role`.
        valueset: Optional named valueset within the vocabulary — reserved
            for a future :class:`ValueSetBinding` sibling.
        required: When ``True``, strict formatter validation (opt-in) fails
            if the column is not produced through a semantic-aware path.
            Default ``False`` keeps existing specs valid.
    """

    cdm: str
    table: str
    column: str
    vocabulary: str
    role: str | None = None
    valueset: str | None = None
    required: bool = False


# ---------------------------------------------------------------------------
# Handle registration
# ---------------------------------------------------------------------------


def register(handle: SemanticHandle, *, overwrite: bool = False) -> None:
    """Register a semantic handle under ``(handle.kind, handle.name)``.

    Args:
        handle: The :class:`SemanticHandle` (or subclass) to register.
        overwrite: When ``True``, replaces any existing entry. Default
            ``False`` raises on collision.

    Raises:
        ValueError: If ``(kind, name)`` is already registered and
            ``overwrite`` is ``False``.
    """
    bucket = _REGISTRY.setdefault(handle.kind, {})
    if handle.name in bucket and not overwrite:
        raise ValueError(f"Semantic handle '{handle.kind}:{handle.name}' is already registered.")
    bucket[handle.name] = handle


def get(kind: str, name: str) -> SemanticHandle:
    """Return the handle for ``(kind, name)``.

    Raises:
        ValueError: If ``kind`` or ``name`` is not registered.
    """
    bucket = _REGISTRY.get(kind)
    if bucket is None or name not in bucket:
        known = sorted((_REGISTRY.get(kind) or {}).keys())
        raise ValueError(
            f"Unknown semantic handle '{kind}:{name}'. "
            f"Registered under kind '{kind}': {known or '(none)'}."
        )
    return bucket[name]


def get_vocabulary(name: str) -> VocabularyHandle:
    """Return the :class:`VocabularyHandle` for ``name``.

    Type-narrowing shortcut around :func:`get` for the phase-S1 kind.

    Raises:
        ValueError: If ``name`` is not registered as a vocabulary.
    """
    handle = get("vocabulary", name)
    if not isinstance(handle, VocabularyHandle):
        raise ValueError(
            f"Semantic handle 'vocabulary:{name}' is not a VocabularyHandle "
            f"(got {type(handle).__name__})."
        )
    return handle


def registered(kind: str) -> list[str]:
    """Return the sorted list of registered names for ``kind``.

    Unknown kinds return ``[]`` — the registry is open-ended by design.
    """
    return sorted((_REGISTRY.get(kind) or {}).keys())


def registered_kinds() -> list[str]:
    """Return the sorted list of kinds that have at least one registration."""
    return sorted(_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Binding registration
# ---------------------------------------------------------------------------


def bind_column(binding: ColumnBinding, *, overwrite: bool = False) -> None:
    """Register a :class:`ColumnBinding`.

    Args:
        binding: The binding to register.
        overwrite: When ``True``, replaces any existing binding for the
            same ``(cdm, table, column)``.

    Raises:
        ValueError: If a binding already exists and ``overwrite`` is
            ``False``, or if the referenced vocabulary is not registered.
    """
    # Referential integrity check — cheap and catches typos early.
    if binding.vocabulary not in (_REGISTRY.get("vocabulary") or {}):
        raise ValueError(
            f"ColumnBinding for {binding.cdm}.{binding.table}.{binding.column} "
            f"references unknown vocabulary '{binding.vocabulary}'. "
            f"Registered vocabularies: {registered('vocabulary') or '(none)'}."
        )
    key = (binding.cdm, binding.table, binding.column)
    if key in _BINDINGS and not overwrite:
        raise ValueError(
            f"ColumnBinding for {binding.cdm}.{binding.table}.{binding.column} "
            f"is already registered."
        )
    _BINDINGS[key] = binding


def binding_for(cdm: str, table: str, column: str) -> ColumnBinding | None:
    """Return the binding for ``(cdm, table, column)``, or ``None``."""
    return _BINDINGS.get((cdm, table, column))


def bindings_for(cdm: str, table: str | None = None) -> tuple[ColumnBinding, ...]:
    """Return every binding for ``cdm`` (optionally filtered to one ``table``)."""
    result = [b for (c, _t, _col), b in _BINDINGS.items() if c == cdm]
    if table is not None:
        result = [b for b in result if b.table == table]
    return tuple(sorted(result, key=lambda b: (b.table, b.column)))


def all_bindings() -> tuple[ColumnBinding, ...]:
    """Return every registered binding, sorted by ``(cdm, table, column)``."""
    return tuple(sorted(_BINDINGS.values(), key=lambda b: (b.cdm, b.table, b.column)))


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _clear() -> None:
    """Drop every registration and binding. Test-only."""
    _REGISTRY.clear()
    _BINDINGS.clear()
