"""Declarative mapper subsystem (P10).

Mappers apply per-column, per-table transformations after the format
and deduplicate stages and before the fix stage. Every mapper is a
:class:`~ovid2.mappers.spec.MapperSpec` — an ordered list of stateless
ops. The engine walks the list and applies each op lazily.

Public API::

    from ovid2.mappers import run, load_spec, MAP_OPS

    spec = load_spec(pipeline="example_pcornet", table="demographic")
    df = run(df, spec, spark=spark, cdm=cdm)

Adding a new op is a framework-level change — subclass
:class:`~ovid2.mappers.base.BaseMapOp` and call
:func:`ovid2.mappers.registry.register`.
"""

from __future__ import annotations

from ovid2.mappers.base import BaseMapOp, MapOpContext
from ovid2.mappers.engine import load_secrets, run
from ovid2.mappers.registry import MAP_OPS, register
from ovid2.mappers.spec import (
    MapperOpSpec,
    MapperSpec,
    MapperSpecNotFoundError,
    MapperSpecValidationError,
    find_spec,
    load_from_dict,
    load_from_yaml,
    load_spec,
)

__all__ = [
    "MAP_OPS",
    "BaseMapOp",
    "MapOpContext",
    "MapperOpSpec",
    "MapperSpec",
    "MapperSpecNotFoundError",
    "MapperSpecValidationError",
    "find_spec",
    "load_from_dict",
    "load_from_yaml",
    "load_secrets",
    "load_spec",
    "register",
    "run",
]
