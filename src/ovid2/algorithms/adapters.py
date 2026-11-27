"""Thin adapters: make every native algorithm object satisfy :class:`Algorithm`.

Each adapter wraps the native class / spec **without changing it** and
forwards :meth:`apply` to the native entry point, so the facade adds no
behaviour. Parameterised kinds take an :class:`Invocation` (native context
plus the step's params); a bare native context means "no params".

Executor tier (imported by :mod:`ovid2.algorithms.registry`); never by the
web / MCP tiers.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ovid2.algorithms.protocol import AlgorithmKind

__all__ = [
    "DeduplicatorAlgorithm",
    "FixerAlgorithm",
    "FormatterAlgorithm",
    "HealthCheckAlgorithm",
    "Invocation",
    "MapperAlgorithm",
    "TransformAlgorithm",
    "requires_of",
]


@dataclass(frozen=True)
class Invocation:
    """One call of an algorithm: its native context plus step params.

    Attributes:
        context: The kind's native context (``AdapterContext``,
            ``MapOpContext``, the pipeline context dict, ``TransformContext``,
            ``OpContext``).
        params: Step parameters (mapper / transform ops); the formatter
            adapter reads ``params["spec"]``.
    """

    context: Any
    params: Mapping[str, Any] = field(default_factory=dict)


def _split(ctx: Any) -> tuple[Any, Mapping[str, Any]]:
    if isinstance(ctx, Invocation):
        return ctx.context, ctx.params
    return ctx, {}


def requires_of(native: Any) -> frozenset[str]:
    """Return the capability names ``native`` declares (``REQUIRES``)."""
    declared = getattr(native, "REQUIRES", None)
    if declared is None:
        declared = getattr(native, "__requires__", None) or ()
    return frozenset(str(getattr(c, "value", c)) for c in declared)


def summary_of(native: Any) -> str:
    """Return the first docstring line of ``native`` (class or function)."""
    return (inspect.getdoc(native) or "").splitlines()[0] if inspect.getdoc(native) else ""


@dataclass(frozen=True)
class _ClassAlgorithm:
    """Shared shape: a registered class plus its name."""

    name: str
    native: type[Any]

    @property
    def requires(self) -> frozenset[str]:
        """Capabilities declared on the native class (``REQUIRES``)."""
        return requires_of(self.native)

    def requires_for(self, params: Mapping[str, Any]) -> frozenset[str]:
        """Return :attr:`requires` plus what a step with ``params`` adds.

        A native class may define ``requires_for(params)`` for
        parameter-dependent needs (``random_sample`` with a ``seed`` needs
        ``rand_seeded``).
        """
        extra = getattr(self.native, "requires_for", None)
        if not callable(extra):
            return self.requires
        return self.requires | frozenset(str(getattr(c, "value", c)) for c in extra(dict(params)))


@dataclass(frozen=True)
class FormatterAlgorithm(_ClassAlgorithm):
    """A formatter adapter (``adapter:`` in a formatter spec)."""

    @property
    def kind(self) -> AlgorithmKind:
        """Always :attr:`AlgorithmKind.FORMATTER`."""
        return AlgorithmKind.FORMATTER

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Resolve ``params['spec']`` through the adapter (``frame`` unused)."""
        del frame
        context, params = _split(ctx)
        return self.native().resolve(params["spec"], context)


@dataclass(frozen=True)
class DeduplicatorAlgorithm(_ClassAlgorithm):
    """The composite-key deduplicator, configured from the pipeline context."""

    @property
    def kind(self) -> AlgorithmKind:
        """Always :attr:`AlgorithmKind.DEDUPLICATOR`."""
        return AlgorithmKind.DEDUPLICATOR

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Deduplicate ``frame`` on the context table's composite key."""
        context, params = _split(ctx)
        dedup = self.native(
            context["table_name"],
            cdm=context.get("cdm"),
            engine=context.get("engine"),
            key_fields=params.get("key_fields"),
        )
        return dedup.deduplicate(frame)


@dataclass(frozen=True)
class MapperAlgorithm(_ClassAlgorithm):
    """A mapper op (``op:`` in a mapper spec)."""

    @property
    def kind(self) -> AlgorithmKind:
        """Always :attr:`AlgorithmKind.MAPPER`."""
        return AlgorithmKind.MAPPER

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Run the op with ``Invocation(MapOpContext, params)``."""
        context, params = _split(ctx)
        return self.native().apply(frame, dict(params), context)


@dataclass(frozen=True)
class FixerAlgorithm(_ClassAlgorithm):
    """A common fixer (``fix:`` in a fixer chain)."""

    @property
    def kind(self) -> AlgorithmKind:
        """Always :attr:`AlgorithmKind.FIXER`."""
        return AlgorithmKind.FIXER

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Run the fixer with the pipeline context dict (params merged in)."""
        context, params = _split(ctx)
        merged = {**context, **params} if params else context
        return self.native().apply(frame, merged)


@dataclass(frozen=True)
class TransformAlgorithm(_ClassAlgorithm):
    """A library transform (``op:`` in a transform spec)."""

    @property
    def kind(self) -> AlgorithmKind:
        """Always :attr:`AlgorithmKind.TRANSFORM`."""
        return AlgorithmKind.TRANSFORM

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Run the op with ``Invocation(TransformContext, params)``."""
        context, params = _split(ctx)
        return self.native().apply(frame, dict(params), context)


@dataclass(frozen=True)
class HealthCheckAlgorithm:
    """A declarative health check: its :class:`CheckSpec` plus operator.

    Attributes:
        name: The check id.
        native: The :class:`CheckSpec`.
        operator: The resolved operator callable (``None`` when the spec names
            an unregistered operator — the runner reports that as an error).
    """

    name: str
    native: Any
    operator: Callable[[Any, Any], Any] | None

    @property
    def kind(self) -> AlgorithmKind:
        """Always :attr:`AlgorithmKind.HEALTH_CHECK`."""
        return AlgorithmKind.HEALTH_CHECK

    @property
    def requires(self) -> frozenset[str]:
        """Capabilities declared on the operator (``__requires__``)."""
        return requires_of(self.operator) if self.operator is not None else frozenset()

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Run the operator against ``OpContext`` and return its metric rows."""
        del frame
        context, _params = _split(ctx)
        if self.operator is None:
            raise LookupError(f"unknown operator '{self.native.operator}'")
        return self.operator(context, self.native)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.algorithms.adapters is a library module.")
