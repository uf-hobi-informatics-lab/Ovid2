"""Transform spec engine.

Walks the ordered ``steps`` list on a
:class:`~ovid2.transforms.spec.TransformSpec` and applies each op to the
incoming DataFrame. Every op is stateless and lazy — only the final
``write()`` at the end of a pipeline triggers execution.

Two guardrails run before each op:

1. **Existence** — the op name must be in
   :data:`ovid2.transforms.registry.TRANSFORMS`; unknown ops fail fast
   (unlike the mapper engine, which only discovers unknown ops at the
   step that uses them).
2. **Family gating** — an op that declares ``requires_family`` is
   refused up front when the active engine family differs, with a clear
   error rather than a deep-in-execution failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.transforms.base import DataFrame, TransformContext
from ovid2.transforms.registry import TRANSFORMS
from ovid2.transforms.spec import TransformSpec, TransformSpecValidationError

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineFunctions, EngineSession


class TransformFamilyError(RuntimeError):
    """Raised when a transform requires a compute family the engine lacks."""


def run(
    df: "DataFrame",
    spec: TransformSpec,
    *,
    session: "EngineSession",
    functions: "EngineFunctions",
    engine_family: str = "spark",
    pipeline: str | None = None,
    secrets: dict[str, Any] | None = None,
) -> "DataFrame":
    """Execute a transform spec against ``df``.

    Args:
        df: The DataFrame to transform.
        spec: The transform spec to apply.
        session: The engine session (portable query context).
        functions: The engine's portable column-expression DSL.
        engine_family: ``'spark'`` / ``'snowpark'`` — used to gate ops
            that declare ``requires_family``.
        pipeline: Optional owning pipeline name (for context only).
        secrets: Optional pipeline-scoped secret bag.

    Returns:
        The transformed DataFrame (still lazy).

    Raises:
        TransformSpecValidationError: On an unknown op name.
        TransformFamilyError: When an op requires a different family.
    """
    if not spec.enabled:
        return df

    ctx = TransformContext(
        session=session,
        functions=functions,
        engine_family=engine_family,
        pipeline=pipeline,
        secrets=secrets,
    )

    for step in spec.steps:
        transform_class = TRANSFORMS.get(step.op)
        if transform_class is None:
            raise TransformSpecValidationError(
                f"Unknown transform op '{step.op}' in spec '{spec.name}'. "
                f"Registered ops: {', '.join(sorted(TRANSFORMS))}."
            )
        required = transform_class.requires_family
        if required is not None and required != engine_family:
            raise TransformFamilyError(
                f"Transform '{step.op}' requires engine family '{required}' "
                f"but the active engine family is '{engine_family}'."
            )
        df = transform_class().apply(df, step.params, ctx)

    return df


__all__ = ["TransformFamilyError", "run"]
