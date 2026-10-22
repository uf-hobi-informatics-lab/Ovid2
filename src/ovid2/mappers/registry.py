"""Registry of every built-in map op.

The engine looks up ops by name via :data:`MAP_OPS`. Third-party
extensions should call :func:`register` at import time.
"""

from __future__ import annotations

from ovid2.mappers.base import BaseMapOp
from ovid2.mappers.ops import (
    ApplyTransformsOp,
    CaseMapOp,
    HashIdOp,
    LiteralOp,
    PassthroughOp,
    RegexReplaceOp,
    TranslateByConceptOp,
    TranslateByDictOp,
)

MAP_OPS: dict[str, type[BaseMapOp]] = {
    "translate_by_dict": TranslateByDictOp,
    "translate_by_concept": TranslateByConceptOp,
    "hash_id": HashIdOp,
    "case_map": CaseMapOp,
    "regex_replace": RegexReplaceOp,
    "literal": LiteralOp,
    "passthrough": PassthroughOp,
    "apply_transforms": ApplyTransformsOp,
}


def register(name: str, op_class: type[BaseMapOp], *, overwrite: bool = False) -> None:
    """Register a new map op.

    Args:
        name: Op identifier used in spec ``op:`` fields.
        op_class: The :class:`BaseMapOp` subclass.
        overwrite: When ``True``, replaces an existing entry.

    Raises:
        ValueError: If ``name`` already registered and ``overwrite=False``.
    """
    if name in MAP_OPS and not overwrite:
        raise ValueError(f"Map op '{name}' is already registered.")
    MAP_OPS[name] = op_class
