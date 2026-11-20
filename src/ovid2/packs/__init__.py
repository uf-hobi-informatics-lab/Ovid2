"""Domain packs — drop-in bundles that teach Ovid2 a new data domain.

A pack ships a target data model (CDM YAML trees), declarative
data-health checks and report-cycle packages, semantic vocabularies and
column bindings, Report Studio templates and remediation hints — and,
for installed (pip) packs only, check operators and fixers. PCORnet is
the built-in reference pack (``resources/packs/pcornet_7_0``).

This package is **tier-neutral**: it discovers packs and reads their
manifests and YAML without importing any pack code or any pipeline
module, so the web tier, the manual generator and the executor all see
the same catalogue (``tests/contract/test_packs_tier_neutral.py``).
Pack *code* is imported only by the executor registries
(:mod:`ovid2.transformations.quality.ops`,
:mod:`ovid2.transformations.fixers.common.registry`).

Public API:

- :func:`all_packs` / :func:`active_packs` / :func:`get_pack`;
- :func:`pack_dirs` — content directories of active packs;
- :func:`clear_cache` — re-run discovery.

ADR: the adr-domain-packs design note.
"""

from __future__ import annotations

from ovid2.packs.discovery import (
    ENTRY_POINT_GROUP,
    PackInfo,
    active_packs,
    all_packs,
    clear_cache,
    get_pack,
    pack_dirs,
)
from ovid2.packs.manifest import PackManifest, PackManifestError, load_manifest

__all__ = [
    "ENTRY_POINT_GROUP",
    "PackInfo",
    "PackManifest",
    "PackManifestError",
    "active_packs",
    "all_packs",
    "clear_cache",
    "get_pack",
    "load_manifest",
    "pack_dirs",
]
