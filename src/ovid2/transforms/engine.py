"""Transform spec engine.

Walks the ordered ``steps`` list on a
:class:`~ovid2.transforms.spec.TransformSpec` and applies each op to the
incoming DataFrame. Every op is stateless and lazy — only the final
``write()`` at the end of a pipeline triggers execution.

Two guardrails run before any op executes (the whole chain is
checked first, so a bad step 5 never half-applies steps 1–4):

1. **Existence** — the op name must be in
   :class:`~ovid2.algorithms.registry.AlgorithmRegistry` (kind ``transform``); unknown ops fail fast
   (unlike the mapper engine, which only discovers unknown ops at the
   step that uses them).
2. **Parameters** — the step's params are validated against the op's
   :attr:`~ovid2.transforms.base.BaseTransform.params_schema`
   (:func:`validate_step_params`).

Every op runs on every engine (no family gating; see
``adr-engine-universality.md``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ovid2.algorithms.adapters import Invocation
from ovid2.algorithms.protocol import AlgorithmKind
from ovid2.algorithms.registry import registry as algorithms
from ovid2.transforms.base import DataFrame, TransformContext
from ovid2.transforms.spec import TransformSpec, TransformSpecValidationError

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineFunctions, EngineSession


def validate_step_params(op: str, params: dict[str, Any]) -> list[str]:
    """Return schema errors for one step's ``params`` (empty when valid).

    Args:
        op: Registered op name.
        params: The step's parameters (every key except ``op``).
    """
    if not algorithms.has(AlgorithmKind.TRANSFORM, op):
        return [f"unknown transform op '{op}'"]
    transform_class = algorithms.native(AlgorithmKind.TRANSFORM, op)
    from jsonschema import Draft202012Validator

    schema = transform_class().params_schema
    validator = Draft202012Validator(schema)
    return [
        f"{'/'.join(str(p) for p in e.path) or '<params>'}: {e.message}"
        for e in sorted(validator.iter_errors(params), key=lambda e: list(e.path))
    ]


def run(
    df: DataFrame,
    spec: TransformSpec,
    *,
    session: EngineSession,
    functions: EngineFunctions,
    engine_family: str = "spark",
    pipeline: str | None = None,
    secrets: dict[str, Any] | None = None,
) -> DataFrame:
    """Execute a transform spec against ``df``.

    Args:
        df: The DataFrame to transform.
        spec: The transform spec to apply.
        session: The engine session (portable query context).
        functions: The engine's portable column-expression DSL.
        engine_family: Active engine family (context only).
        pipeline: Optional owning pipeline name (for context only).
        secrets: Optional pipeline-scoped secret bag.

    Returns:
        The transformed DataFrame (still lazy).

    Raises:
        TransformSpecValidationError: On an unknown op name.
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

    for index, step in enumerate(spec.steps, 1):
        if not algorithms.has(AlgorithmKind.TRANSFORM, step.op):
            raise TransformSpecValidationError(
                f"Unknown transform op '{step.op}' in spec '{spec.name}'. "
                f"Registered ops: {', '.join(algorithms.names(AlgorithmKind.TRANSFORM))}."
            )
        errors = validate_step_params(step.op, step.params)
        if errors:
            raise TransformSpecValidationError(
                f"Spec '{spec.name}' step {index} ({step.op}): " + "; ".join(errors)
            )

    for step in spec.steps:
        df = algorithms.get(AlgorithmKind.TRANSFORM, step.op).apply(
            df, Invocation(ctx, step.params)
        )

    return df


__all__ = ["run", "validate_step_params"]
