"""Built-in map ops.

Every op is a :class:`~ovid2.mappers.base.BaseMapOp` subclass registered
in :data:`ovid2.mappers.registry.MAP_OPS`. Adding a new op is a
framework-level change; specs never declare custom Python ops (the
``class:`` escape hatch on the spec covers that case).

Ships with:

- ``translate_by_dict`` — apply a pipeline dictionary to one column.
- ``translate_by_concept`` — apply a semantic vocabulary lookup (S2).
- ``hash_id`` — SHA-256 hash a column with a per-pipeline salt.
- ``case_map`` — upper/lower/title case one column.
- ``regex_replace`` — regex substitution on one column.
- ``literal`` — overwrite a column with a constant.
- ``passthrough`` — explicit no-op.
- ``apply_transforms`` — run a public-library transform chain (bridge to
  :mod:`ovid2.transforms`).
"""

from __future__ import annotations

from ovid2.mappers.ops.apply_transforms import ApplyTransformsOp
from ovid2.mappers.ops.case_map import CaseMapOp
from ovid2.mappers.ops.hash_id import HashIdOp
from ovid2.mappers.ops.literal import LiteralOp
from ovid2.mappers.ops.passthrough import PassthroughOp
from ovid2.mappers.ops.regex_replace import RegexReplaceOp
from ovid2.mappers.ops.translate_by_concept import TranslateByConceptOp
from ovid2.mappers.ops.translate_by_dict import TranslateByDictOp

__all__ = [
    "ApplyTransformsOp",
    "CaseMapOp",
    "HashIdOp",
    "LiteralOp",
    "PassthroughOp",
    "RegexReplaceOp",
    "TranslateByConceptOp",
    "TranslateByDictOp",
]
