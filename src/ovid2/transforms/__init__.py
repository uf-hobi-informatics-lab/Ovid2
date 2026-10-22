"""Ovid2 public transformations library.

A CDM-agnostic, pipeline-type-free menu of general-purpose lazy
DataFrame transformations (formatting, correction, reshape, sampling)
that users and Agent Ovid can discover and compose into reusable chains.

See :mod:`ovid2.transforms.base` for the design and the distinction from
mapper ops, fixers, and formatter adapters.
"""

from __future__ import annotations

from ovid2.transforms.base import BaseTransform, TransformContext
from ovid2.transforms.engine import TransformFamilyError, run
from ovid2.transforms.registry import TRANSFORMS, register, transform_names
from ovid2.transforms.spec import (
    TransformSpec,
    TransformSpecNotFoundError,
    TransformSpecValidationError,
    TransformStepSpec,
    find_spec,
    load_from_dict,
    load_from_yaml,
    load_spec,
)

__all__ = [
    "TRANSFORMS",
    "BaseTransform",
    "TransformContext",
    "TransformFamilyError",
    "TransformSpec",
    "TransformSpecNotFoundError",
    "TransformSpecValidationError",
    "TransformStepSpec",
    "find_spec",
    "load_from_dict",
    "load_from_yaml",
    "load_spec",
    "register",
    "run",
    "transform_names",
]
