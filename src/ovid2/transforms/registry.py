"""Registry of every built-in transform.

The engine looks up transforms by name via :data:`TRANSFORMS`.
Third-party extensions call :func:`register` at import time.

The registry is the single source of truth for the transform
vocabulary. The web-safe catalogue
(:mod:`ovid2.config.transforms_catalogue`) mirrors the *metadata* of
every entry here so the web/MCP tiers can enumerate transforms without
importing this (pipeline-tier) module; a contract test
(``tests/contract/test_transforms_catalogue_synchronised.py``) keeps
the two in sync.
"""

from __future__ import annotations

from ovid2.transforms.base import BaseTransform
from ovid2.transforms.ops import (
    AddRowNumberTransform,
    CastTransform,
    ClampTransform,
    CoalesceColumnsTransform,
    DedupTransform,
    DefaultValueTransform,
    DenseRankTransform,
    DistinctTransform,
    DropColumnsTransform,
    ExplodeTransform,
    FilterExprTransform,
    HeadByGroupTransform,
    LimitTransform,
    NormalizeWhitespaceTransform,
    NullIfTransform,
    PadTransform,
    PivotTransform,
    RandomSampleTransform,
    RankTransform,
    ReduceTransform,
    RegexReplaceTransform,
    RenameColumnsTransform,
    SelectColumnsTransform,
    StandardizeNullTransform,
    StratifiedSampleTransform,
    StripNonAlphaTransform,
    StripNonNumericTransform,
    ToLowerTransform,
    ToTitleTransform,
    ToUpperTransform,
    TrimTransform,
    UnpivotTransform,
)

TRANSFORMS: dict[str, type[BaseTransform]] = {
    # formatting
    "to_upper": ToUpperTransform,
    "to_lower": ToLowerTransform,
    "to_title": ToTitleTransform,
    "trim": TrimTransform,
    "normalize_whitespace": NormalizeWhitespaceTransform,
    "regex_replace": RegexReplaceTransform,
    "pad": PadTransform,
    "rename_columns": RenameColumnsTransform,
    "cast": CastTransform,
    "coalesce_columns": CoalesceColumnsTransform,
    "default_value": DefaultValueTransform,
    # correction
    "null_if": NullIfTransform,
    "clamp": ClampTransform,
    "standardize_null": StandardizeNullTransform,
    "strip_non_numeric": StripNonNumericTransform,
    "strip_non_alpha": StripNonAlphaTransform,
    "dedup": DedupTransform,
    "distinct": DistinctTransform,
    "filter_expr": FilterExprTransform,
    # reshape
    "select_columns": SelectColumnsTransform,
    "drop_columns": DropColumnsTransform,
    "reduce": ReduceTransform,
    "explode": ExplodeTransform,
    "unpivot": UnpivotTransform,
    "pivot": PivotTransform,
    "add_row_number": AddRowNumberTransform,
    "rank": RankTransform,
    "dense_rank": DenseRankTransform,
    # sampling
    "random_sample": RandomSampleTransform,
    "limit": LimitTransform,
    "stratified_sample": StratifiedSampleTransform,
    "head_by_group": HeadByGroupTransform,
}


def register(name: str, transform_class: type[BaseTransform], *, overwrite: bool = False) -> None:
    """Register a new transform.

    Args:
        name: Transform identifier used in spec ``op:`` fields.
        transform_class: The :class:`BaseTransform` subclass.
        overwrite: When ``True``, replaces an existing entry.

    Raises:
        ValueError: If ``name`` already registered and ``overwrite=False``.
    """
    if name in TRANSFORMS and not overwrite:
        raise ValueError(f"Transform '{name}' is already registered.")
    TRANSFORMS[name] = transform_class


def transform_names() -> list[str]:
    """Return the sorted list of registered transform names."""
    return sorted(TRANSFORMS)


__all__ = ["TRANSFORMS", "register", "transform_names"]
