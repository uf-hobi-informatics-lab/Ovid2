"""Semantic layer — vocabularies, bindings, and (later) metrics/catalog.

Import this package to force every bundled semantic handle to register
itself with :mod:`ovid2.semantic.registry`. Callers should use only the
public API:

- :func:`register` / :func:`get` / :func:`registered` — kind-agnostic
  registry operations.
- :func:`get_vocabulary` — type-narrowing shortcut for the phase-S1 kind.
- :func:`bind_column` / :func:`binding_for` / :func:`bindings_for` —
  column-level bindings that attach a vocabulary to a specific
  ``(cdm, table, column)`` tuple.

The registry is kind-keyed so future semantic kinds (metrics, catalog
entries) reuse the same lifecycle, CLI surface, and test scaffolding.

Pipeline code (``core/``, ``transformations/``, ``formatters/``,
``mappers/``) MUST NOT import individual vocabulary modules — only the
registry API. This mirrors the CDM/ETL independence rule (P10) and is
enforced by ``tests/contract/test_semantic_isolation.py``.

See the adr-semantic-layer design note for the full
design and rollout plan.
"""

from __future__ import annotations

# Import each bundled vocabulary module for its side effect (self-registration).
# The `vocabularies` package's `__init__` calls the YAML loader, which
# reads `resources/semantic/vocabularies/*.yaml` (phase S3 — bundled
# cross-CDM vocabularies including OMOP CONCEPT) and
# `resources/semantic/bindings/*.yaml` (S3 — column bindings).
from ovid2.semantic import vocabularies as _vocabularies  # noqa: F401
from ovid2.semantic.handle import SemanticHandle, VocabularyHandle
from ovid2.semantic.registry import (
    ColumnBinding,
    all_bindings,
    bind_column,
    binding_for,
    bindings_for,
    get,
    get_vocabulary,
    register,
    registered,
    registered_kinds,
)

# PCORnet CDM valuesets (SEX, DX_TYPE, …) — registered as inline
# vocabularies from `resources/cdms/pcornet_7_0/valuesets/*.yaml` at
# import time. Introduced by phase C1.
from ovid2.semantic.vocabularies import pcornet as _pcornet  # noqa: F401

__all__ = [
    "ColumnBinding",
    "SemanticHandle",
    "VocabularyHandle",
    "all_bindings",
    "bind_column",
    "binding_for",
    "bindings_for",
    "get",
    "get_vocabulary",
    "register",
    "registered",
    "registered_kinds",
]
