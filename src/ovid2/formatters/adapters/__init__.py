"""Shared source adapters for the formatter spec engine.

Adapters know how to read a *shape* of source data and return a
normalized, still-lazy :class:`pyspark.sql.DataFrame` for the
``projections:`` block to consume.

Registered adapters:

- :class:`source_passthrough.SourcePassthroughAdapter` — CDM-agnostic
  source loader (formerly ``pcornet_passthrough``).
- :class:`semantic_join.SemanticJoinAdapter`         — vocabulary-driven
  join, framework-level. Superset of ``omop_concept_join`` (S2).
- :class:`omop_concept_join.OmopConceptJoinAdapter`  — S2 compatibility
  shim onto :class:`SemanticJoinAdapter`; kept so 184 legacy YAMLs load.
- :class:`union_of_specs.UnionOfSpecsAdapter`        — recursive union of sub-specs.
- :class:`custom.CustomAdapter`                      — escape hatch (BaseFormatter subclass).

The registry aliases ``pcornet_passthrough`` to the source-passthrough
adapter so pre-P10 specs keep loading. A deprecation warning is emitted
at spec-load time via :func:`ovid2.formatters.spec.load_spec`.

Adapters are **framework-level**. Adding a new adapter is a change to
Ovid2 itself, never a per-pipeline concern.
"""

from __future__ import annotations

from ovid2.formatters.adapters.base import AdapterContext, BaseAdapter
from ovid2.formatters.adapters.custom import CustomAdapter
from ovid2.formatters.adapters.omop_concept_join import OmopConceptJoinAdapter
from ovid2.formatters.adapters.pcornet_passthrough import PcornetPassthroughAdapter
from ovid2.formatters.adapters.semantic_join import SemanticJoinAdapter
from ovid2.formatters.adapters.source_passthrough import SourcePassthroughAdapter
from ovid2.formatters.adapters.union_of_specs import UnionOfSpecsAdapter

# Deprecated adapter names → current name. Kept out of the primary
# registry so that iteration over ADAPTERS returns only canonical names.
ADAPTER_ALIASES: dict[str, str] = {
    "pcornet_passthrough": "source_passthrough",
}

ADAPTERS: dict[str, type[BaseAdapter]] = {
    "source_passthrough": SourcePassthroughAdapter,
    "semantic_join": SemanticJoinAdapter,
    "omop_concept_join": OmopConceptJoinAdapter,
    "union_of_specs": UnionOfSpecsAdapter,
    "custom": CustomAdapter,
    # Deprecated alias — resolves to the same class.
    "pcornet_passthrough": PcornetPassthroughAdapter,
}

__all__ = [
    "ADAPTERS",
    "ADAPTER_ALIASES",
    "AdapterContext",
    "BaseAdapter",
    "CustomAdapter",
    "OmopConceptJoinAdapter",
    "PcornetPassthroughAdapter",
    "SemanticJoinAdapter",
    "SourcePassthroughAdapter",
    "UnionOfSpecsAdapter",
]
