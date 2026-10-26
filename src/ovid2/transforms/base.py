"""Base classes for the public transformations library.

A *transform* is a single, composable, CDM-agnostic, pipeline-type-free
DataFrame-in / DataFrame-out operation named in a
:class:`~ovid2.transforms.spec.TransformSpec`. Transforms are the
building blocks of the "public transformations library" — a menu of
general-purpose reshaping, formatting, correction, and sampling
operations that both human users and Agent Ovid can discover (over the
``GET /api/v1/transforms`` catalogue and the ``list_transforms`` MCP
tool) and compose into reusable chains.

How transforms differ from the three older declarative layers:

* **Mapper ops** (:mod:`ovid2.mappers.ops`) translate *values* inside
  one CDM table for one pipeline. They are bound to a ``(pipeline,
  table)`` spec.
* **Fixers** (:mod:`ovid2.transformations.fixers`) repair
  CDM-specific data-integrity problems (dates after death, orphan
  foreign keys). Many carry a CDM scope.
* **Formatter adapters** (:mod:`ovid2.formatters.adapters`) pull raw
  sources in and line up columns.

Transforms are none of those: they are free-standing, reusable, and
know nothing about any CDM, table, or pipeline type.

Engine portability
-------------------

Transforms MUST be engine-portable. They never ``import pyspark`` —
enforced repo-wide by ``tests/contract/test_no_pyspark_outside_engine.py``.
Column-expression work goes through :attr:`TransformContext.functions`
(the :class:`~ovid2.core.engines.protocols.EngineFunctions` DSL);
DataFrame-shape work (``dropDuplicates``, ``sample``, ``groupBy`` …)
uses the portable DataFrame API that both PySpark and Snowpark expose.
An op that genuinely needs a Spark-only feature declares
``requires_family = "spark"`` so the engine gates it up front rather
than failing deep in execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

#: Engine-opaque DataFrame type. We deliberately do NOT import
#: ``pyspark.sql.DataFrame`` — the transforms tree is engine-portable and
#: contract-forbidden from importing pyspark
#: (``tests/contract/test_no_pyspark_outside_engine.py``). The concrete
#: type is whatever the active engine's session returns.
DataFrame = Any

if TYPE_CHECKING:
    from ovid2.core.engines.protocols import EngineFunctions, EngineSession


@dataclass(frozen=True)
class TransformContext:
    """Read-only context passed to every :meth:`BaseTransform.apply`.

    Attributes:
        session: The engine's session-agnostic query context
            (:class:`~ovid2.core.engines.protocols.EngineSession`).
            Used by the handful of ops that need to build a helper
            DataFrame or run a temp-view query.
        functions: The engine's portable column-expression DSL
            (:class:`~ovid2.core.engines.protocols.EngineFunctions`).
            All column work goes through this so transforms run under
            either compute family.
        engine_family: ``'spark'`` or ``'snowpark'`` — used by the
            engine to gate ops that declare ``requires_family``.
        pipeline: Optional owning pipeline name (``None`` for a
            shared-library chain run outside a pipeline).
        secrets: Optional pipeline-scoped secret bag. Transforms rarely
            need secrets; provided for symmetry with the mapper engine.
    """

    session: EngineSession
    functions: EngineFunctions
    engine_family: str = "spark"
    pipeline: str | None = None
    secrets: dict[str, Any] | None = None


class BaseTransform(ABC):
    """Abstract base class for every transform.

    Transforms are stateless — the engine constructs a fresh instance
    per apply. Configuration comes from the ``params`` dict declared in
    the :class:`~ovid2.transforms.spec.TransformStepSpec`.

    Subclasses declare four pieces of metadata (``name``, ``summary``,
    ``category``, ``params_schema``) so the transform can be published
    in the web-safe catalogue (:mod:`ovid2.config.transforms_catalogue`)
    for discovery by users and agents.
    """

    #: Compute family this transform requires, or ``None`` when it runs
    #: on any family. When set (e.g. ``'spark'``), the engine refuses to
    #: run the transform on a different family with a clear error rather
    #: than letting it fail mid-execution.
    requires_family: str | None = None

    #: Coarse grouping for the catalogue UI. One of ``'formatting'``,
    #: ``'correction'``, ``'reshape'``, ``'sampling'``.
    category: str = "formatting"

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the transform identifier, e.g. ``'to_upper'``."""

    @property
    @abstractmethod
    def summary(self) -> str:
        """Return a one-line human/agent-readable description."""

    @property
    def params_schema(self) -> dict[str, Any]:
        """Return a JSON-schema fragment for this op's ``params``.

        Defaults to an open object. Ops override this to give agents and
        the validator precise parameter contracts. The fragment is
        published in the catalogue and applied by the engine's optional
        per-op validation.
        """
        return {"type": "object"}

    @abstractmethod
    def apply(
        self,
        df: DataFrame,
        params: dict[str, Any],
        ctx: TransformContext,
    ) -> DataFrame:
        """Apply the transform to ``df`` using ``params`` and ``ctx``.

        Args:
            df: Input DataFrame.
            params: Op-specific parameters (validated against
                :attr:`params_schema`).
            ctx: Runtime context (session, portable functions, family).

        Returns:
            The transformed DataFrame (still lazy — no action triggered).
        """


__all__ = ["BaseTransform", "TransformContext"]
