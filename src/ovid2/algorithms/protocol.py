"""The :class:`Algorithm` protocol — one shape for every kind of algorithm.

Ovid2 has six kinds of algorithm, each with its own historical base class
and registry:

=================  ==============================================  ============================================
Kind               Native shape                                    Registry (unchanged, delegated to)
=================  ==============================================  ============================================
``formatter``      formatter *adapter* (:class:`BaseAdapter`)       ``formatters.adapters.ADAPTERS``
``deduplicator``   :class:`Deduplicator`                            the single composite-key deduplicator
``mapper``         :class:`BaseMapOp`                               ``mappers.registry.MAP_OPS``
``fixer``          :class:`BaseFixer`                               ``transformations.fixers.common.registry``
``transform``      :class:`BaseTransform`                           ``transforms.registry.TRANSFORMS``
``health_check``   YAML :class:`CheckSpec` + operator callable      ``transformations.quality.registry``
=================  ==============================================  ============================================

:class:`Algorithm` is what :class:`~ovid2.algorithms.registry.AlgorithmRegistry`
hands out for any of them: ``kind``, ``name``, ``requires`` (the engine
capabilities it needs — see :class:`~ovid2.core.engines.protocols.Capability`)
and ``apply(frame, ctx)``. The thin adapters in
:mod:`ovid2.algorithms.adapters` wrap each native object without changing it,
so every old call path keeps working.

Tier-neutral: no engine, driver or pipeline imports.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = ["Algorithm", "AlgorithmKind"]


class AlgorithmKind(str, Enum):
    """The six algorithm kinds the facade exposes."""

    FORMATTER = "formatter"
    DEDUPLICATOR = "deduplicator"
    MAPPER = "mapper"
    FIXER = "fixer"
    TRANSFORM = "transform"
    HEALTH_CHECK = "health_check"


@runtime_checkable
class Algorithm(Protocol):
    """An engine-neutral unit of data work.

    Attributes:
        kind: Which registry the algorithm lives in.
        name: The name it is registered under.
        requires: Engine capabilities (``Capability`` values) the algorithm
            needs. Checked against the engine before a run is planned.
        native: The wrapped native class / spec, for call sites that still
            use the kind's own API.
    """

    @property
    def kind(self) -> AlgorithmKind:
        """Return the algorithm kind."""
        ...

    @property
    def name(self) -> str:
        """Return the registered name."""
        ...

    @property
    def requires(self) -> frozenset[str]:
        """Return the engine capabilities the algorithm needs."""
        ...

    @property
    def native(self) -> Any:
        """Return the wrapped native class / spec (unchanged)."""
        ...

    def apply(self, frame: Any, ctx: Any) -> Any:
        """Run the algorithm.

        Args:
            frame: The input engine frame (ignored by health checks, which
                read their tables through ``ctx``).
            ctx: The kind's native context (``AdapterContext`` for
                formatters, ``MapOpContext`` + params for mappers, the
                pipeline context dict for fixers / deduplicators,
                ``TransformContext`` + params for transforms, ``OpContext``
                for health checks).

        Returns:
            The output frame (health checks: their metric rows).
        """
        ...


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.algorithms.protocol is a library module.")
