"""Ships-with transform vocabulary.

Each concrete :class:`~ovid2.transforms.base.BaseTransform` is re-exported
here and wired into :data:`ovid2.transforms.registry.TRANSFORMS`.

Categories:

* **formatting** — case, trim, regex, pad, rename, cast, coalesce, default.
* **correction** — null_if, clamp, standardize_null, dedup, distinct,
  filter_expr, strip_non_numeric/alpha.
* **reshape** — reduce, pivot, unpivot, explode, row_number, rank,
  dense_rank, select/drop columns.
* **sampling** — random_sample, limit, stratified_sample, head_by_group.
"""

from __future__ import annotations

from ovid2.transforms.ops.case import (
    ToLowerTransform,
    ToTitleTransform,
    ToUpperTransform,
)
from ovid2.transforms.ops.columns import (
    CastTransform,
    CoalesceColumnsTransform,
    DefaultValueTransform,
    DropColumnsTransform,
    PadTransform,
    RenameColumnsTransform,
    SelectColumnsTransform,
)
from ovid2.transforms.ops.correction import (
    ClampTransform,
    DedupTransform,
    DistinctTransform,
    FilterExprTransform,
    NullIfTransform,
    StandardizeNullTransform,
)
from ovid2.transforms.ops.regex import (
    RegexReplaceTransform,
    StripNonAlphaTransform,
    StripNonNumericTransform,
)
from ovid2.transforms.ops.reshape import (
    AddRowNumberTransform,
    DenseRankTransform,
    ExplodeTransform,
    PivotTransform,
    RankTransform,
    ReduceTransform,
    UnpivotTransform,
)
from ovid2.transforms.ops.sampling import (
    HeadByGroupTransform,
    LimitTransform,
    RandomSampleTransform,
    StratifiedSampleTransform,
)
from ovid2.transforms.ops.trim import (
    NormalizeWhitespaceTransform,
    TrimTransform,
)

__all__ = [
    "AddRowNumberTransform",
    "CastTransform",
    "ClampTransform",
    "CoalesceColumnsTransform",
    "DedupTransform",
    "DefaultValueTransform",
    "DenseRankTransform",
    "DistinctTransform",
    "DropColumnsTransform",
    "ExplodeTransform",
    "FilterExprTransform",
    "HeadByGroupTransform",
    "LimitTransform",
    "NormalizeWhitespaceTransform",
    "NullIfTransform",
    "PadTransform",
    "PivotTransform",
    "RandomSampleTransform",
    "RankTransform",
    "ReduceTransform",
    "RegexReplaceTransform",
    "RenameColumnsTransform",
    "SelectColumnsTransform",
    "StandardizeNullTransform",
    "StratifiedSampleTransform",
    "StripNonAlphaTransform",
    "StripNonNumericTransform",
    "ToLowerTransform",
    "ToTitleTransform",
    "ToUpperTransform",
    "TrimTransform",
    "UnpivotTransform",
]
