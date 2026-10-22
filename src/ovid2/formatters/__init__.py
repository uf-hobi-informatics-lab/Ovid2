"""Declarative formatter engine.

Ovid2's flagship simplification: pipeline formatters are YAML specs (or
Python-dict ``spec(...)`` calls) that select an *adapter* and describe
sources, joins, projections. Hand-written ``BaseFormatter`` classes are
the exception, gated behind ``adapter: custom``.

Public surface:

- :class:`ovid2.formatters.base.BaseFormatter`  — escape-hatch ABC.
- :func:`ovid2.formatters.spec.spec`            — Python-dict spec factory.
- :func:`ovid2.formatters.spec.find_and_load`   — locate & validate a spec on disk.
- :func:`ovid2.formatters.engine.build_formatter` — pipeline entry point.
- :class:`ovid2.formatters.engine.FormatterSpecError` — spec/adapter errors.
- :data:`ovid2.formatters.adapters.ADAPTERS`     — adapter registry (framework-level).

See :mod:`ovid2.formatters.adapters` for the four shipped adapters.
"""

from __future__ import annotations

from ovid2.formatters.base import BaseFormatter
from ovid2.formatters.engine import (
    FormatterSpecError,
    build_formatter,
    execute_spec,
)
from ovid2.formatters.spec import (
    FormatterSpec,
    JoinSpec,
    SourceSpec,
    SpecNotFoundError,
    SpecValidationError,
    SubSpecRef,
    find_and_load,
    load_from_dict,
    load_from_yaml,
    spec,
)

__all__ = [
    "BaseFormatter",
    "FormatterSpec",
    "FormatterSpecError",
    "JoinSpec",
    "SourceSpec",
    "SpecNotFoundError",
    "SpecValidationError",
    "SubSpecRef",
    "build_formatter",
    "execute_spec",
    "find_and_load",
    "load_from_dict",
    "load_from_yaml",
    "spec",
]
