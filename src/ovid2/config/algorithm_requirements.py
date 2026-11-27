"""Web-safe mirror of each algorithm's *feature* requirements.

The plan-time capability check (:mod:`ovid2.algorithms.plan`) runs both in
the executor (``ovid2 etl``) and in the web tier (``jobs_svc.submit``), which
must not import algorithm code. This table lists, per algorithm kind and
name, the **feature** capabilities (``window`` / ``regex`` / …) the algorithm
declares in ``REQUIRES`` / ``requires=`` — function capabilities (``fn:*``)
are implemented by every engine and so are omitted here.

Pinned to the live declarations by
``tests/contract/test_algorithm_requires_declared.py`` (update both sides).
Parameter-dependent needs (``random_sample`` / ``stratified_sample`` with a
``seed`` → ``rand_seeded``) are in :data:`PARAM_FEATURES`.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ALGORITHM_FEATURES", "PARAM_FEATURES", "features_for"]

#: ``{kind: {name: frozenset(feature values)}}``; absent names need none.
ALGORITHM_FEATURES: dict[str, dict[str, frozenset[str]]] = {
    "formatter": {},
    "deduplicator": {},
    "mapper": {
        "regex_replace": frozenset({"regex"}),
        "translate_by_concept": frozenset({"broadcast_join"}),
    },
    "fixer": {
        "add_missing_hash_token_patids": frozenset({"broadcast_join"}),
        "copy_admit_date_or_delete": frozenset({"broadcast_join"}),
        "delete_records_after_death": frozenset({"broadcast_join"}),
        "delete_records_before_birth": frozenset({"broadcast_join"}),
        "encounterid_orphans": frozenset({"broadcast_join"}),
        "future_date_to_null": frozenset({"broadcast_join"}),
        "list_remover": frozenset({"broadcast_join"}),
        "loinc_mover": frozenset({"broadcast_join"}),
        "null_date_to_harvest_refresh": frozenset({"broadcast_join"}),
        "panel_loinc_remover": frozenset({"broadcast_join"}),
        "patid_orphans": frozenset({"broadcast_join"}),
        "patterns_remover": frozenset({"regex"}),
        "providerid_orphans": frozenset({"broadcast_join"}),
        "replication": frozenset({"broadcast_join"}),
    },
    "transform": {
        "add_row_number": frozenset({"window"}),
        "dense_rank": frozenset({"window"}),
        "head_by_group": frozenset({"window"}),
        "normalize_whitespace": frozenset({"regex"}),
        "null_if": frozenset({"regex"}),
        "rank": frozenset({"window"}),
        "regex_replace": frozenset({"regex"}),
        "strip_non_alpha": frozenset({"regex"}),
        "strip_non_numeric": frozenset({"regex"}),
    },
    "health_check": {
        "1.17": frozenset({"regex"}),
    },
}

#: ``{(kind, name): [(param, feature)]}`` — the feature is needed when the
#: step's ``param`` is set (truthy, or not ``None`` for ``seed``).
PARAM_FEATURES: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {
    ("transform", "random_sample"): (
        ("seed", "rand_seeded"),
        ("with_replacement", "sample_with_replacement"),
    ),
    ("transform", "stratified_sample"): (("seed", "rand_seeded"),),
}


def features_for(kind: str, name: str, params: dict[str, Any] | None = None) -> frozenset[str]:
    """Return the feature capabilities ``(kind, name)`` needs with ``params``."""
    out = set(ALGORITHM_FEATURES.get(kind, {}).get(name, frozenset()))
    for param, feature in PARAM_FEATURES.get((kind, name), ()):
        value = (params or {}).get(param)
        if (value is not None) if param == "seed" else bool(value):
            out.add(feature)
    return frozenset(out)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.config.algorithm_requirements is a library module.")
