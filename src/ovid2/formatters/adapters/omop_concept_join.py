"""``omop_concept_join`` adapter — S2 compatibility shim.

Historical adapter for OMOP-shaped source files that need CONCEPT
translation. As of phase S2, this class is a thin subclass of
:class:`~ovid2.formatters.adapters.semantic_join.SemanticJoinAdapter`;
the previous OMOP-specific logic is now a special case of the generic
vocabulary-join machinery.

Existing YAML specs continue to work unchanged: joining against the
``concept`` temp view still resolves through
:meth:`SemanticJoinAdapter._prepare_right_side`'s fallback path, and
``domain_filter`` on the ``concept`` view continues to apply against
``domain_id``.

Codifies the same hazards documented in
the legacy Ovid OMOP-formatter notes:

1. **Case-insensitive headers** — every source is lowercased at read
   time.
2. **Ambiguous ``concept_id``** — every join specifies a ``prefix:`` and
   the adapter aliases every column of the right side by that prefix.
3. **Domain filter** — pre-filters the joined view before the join.
4. **``*_sup_*`` glob leakage** — handled via each source's ``exclude:``.
"""

from __future__ import annotations

from ovid2.formatters.adapters.semantic_join import SemanticJoinAdapter


class OmopConceptJoinAdapter(SemanticJoinAdapter):
    """Adapter for OMOP source data that needs CONCEPT translation.

    Historically a distinct implementation; as of S2 it is a marker
    subclass of :class:`SemanticJoinAdapter`. The registry entry for
    ``omop_concept_join`` is preserved so the 184 existing pipeline
    YAMLs continue to load without change.
    """
