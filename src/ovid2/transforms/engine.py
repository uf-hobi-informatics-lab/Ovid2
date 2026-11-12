"""Transform spec engine.

Walks the ordered ``steps`` list on a
:class:`~ovid2.transforms.spec.TransformSpec` and applies each op to the
incoming DataFrame. Every op is stateless and lazy — only the final
``write()`` at the end of a pipeline triggers execution.

Three guardrails run before any op executes (the whole chain is
checked first, so a bad step 5 never half-applies steps 1–4):

1. **Existence** — the op name must be in
   :data:`ovid2.transforms.registry.TRANSFORMS`; unknown ops fail fast
   (unlike the mapper engine, which only discovers unknown ops at the
   step that uses them).
2. **Parameters** — the step's params are validated against the op's
   :attr:`~ovid2.transforms.base.BaseTransform.params_schema`
   (:func:`validate_step_params`).
3. **Family gating** — an op that declares ``requires_family`` is
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


def validate_step_params(op: str, params: dict[str, Any]) -> list[str]:
    """Return schema errors for one step's ``params`` (empty when valid).

    Args:
        op: Registered op name.
        params: The step's parameters (every key except ``op``).
    """
    transform_class = TRANSFORMS.get(op)
    if transform_class is None:
        return [f"unknown transform op '{op}'"]
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

    for index, step in enumerate(spec.steps, 1):
        transform_class = TRANSFORMS.get(step.op)
        if transform_class is None:
            raise TransformSpecValidationError(
                f"Unknown transform op '{step.op}' in spec '{spec.name}'. "
                f"Registered ops: {', '.join(sorted(TRANSFORMS))}."
            )
        errors = validate_step_params(step.op, step.params)
        if errors:
            raise TransformSpecValidationError(
                f"Spec '{spec.name}' step {index} ({step.op}): " + "; ".join(errors)
            )
        required = transform_class.requires_family
        if required is not None and required != engine_family:
            raise TransformFamilyError(
                f"Transform '{step.op}' requires engine family '{required}' "
                f"but the active engine family is '{engine_family}'."
            )

    for step in spec.steps:
        df = TRANSFORMS[step.op]().apply(df, step.params, ctx)

    return df


__all__ = ["TransformFamilyError", "run", "validate_step_params"]
