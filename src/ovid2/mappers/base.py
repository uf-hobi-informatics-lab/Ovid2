"""Base class for map ops.

A map op is a single, composable DataFrame-in / DataFrame-out
transformation named in a :class:`~ovid2.mappers.spec.MapperSpec`. Ops
are CDM-agnostic — they operate on named columns, dictionary references,
and other declarative parameters, never on hardcoded PCORnet or OMOP
table names.

Concrete ops live in :mod:`ovid2.mappers.ops`; see the module-level
docstring there for the ships-with vocabulary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.cdm.handle import CdmHandle


@dataclass(frozen=True)
class MapOpContext:
    """Read-only context passed to every :class:`BaseMapOp.apply`.

    Attributes:
        spark: Active Spark session.
        pipeline: Pipeline name.
        table: CDM table being mapped.
        cdm: The CDM handle for this pipeline.
        pipeline_dictionaries: The pipeline's ``dictionaries`` module (or
            ``None`` when the pipeline has no dictionaries).
        secrets: Pipeline-scoped secret material, loaded once by the
            engine from ``$XDG_CONFIG_HOME/ovid2/secrets.d/<pipeline>.json``.
    """

    spark: "SparkSession"
    pipeline: str
    table: str
    cdm: "CdmHandle"
    pipeline_dictionaries: Any = None
    secrets: dict[str, Any] | None = None


class BaseMapOp(ABC):
    """Abstract base class for every map op.

    Ops are stateless — the engine constructs a new instance per apply.
    Configuration comes from the ``params`` dict declared in the
    :class:`~ovid2.mappers.spec.MapperOpSpec`.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the op identifier, e.g. ``'translate_by_dict'``."""

    @abstractmethod
    def apply(
        self,
        df: "DataFrame",
        params: dict[str, Any],
        ctx: MapOpContext,
    ) -> "DataFrame":
        """Apply the op to ``df`` using ``params`` and ``ctx``.

        Args:
            df: Input DataFrame.
            params: Op-specific parameters, validated by the engine
                against the op's JSON-schema fragment.
            ctx: Runtime context (Spark session, pipeline, dictionaries,
                secrets, CDM handle).

        Returns:
            The transformed DataFrame (still lazy).
        """
