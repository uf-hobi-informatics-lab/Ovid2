"""Base adapter interface.

An adapter reads a shape of source data described by a :class:`FormatterSpec`
and returns a normalised, still-lazy Spark DataFrame that the engine can
project into CDM columns.

Adapters are framework-level. New ones live under
``ovid2.formatters.adapters``; per-pipeline registration is forbidden.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ovid2.formatters.spec import FormatterSpec

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine
    from ovid2.core.engines.protocols import EngineFunctions


@dataclass
class AdapterContext:
    """Runtime context handed to every adapter.

    Attributes:
        spark: Active Spark session. Retained for backward compatibility;
            engine-portable code should prefer :attr:`engine` /
            :attr:`functions` so it works under Snowpark too.
        input_address: Where to read raw source files from.
        pipeline: Pipeline name.
        batch_id: Batch identifier.
        engine: Active compute engine. ``None`` when the context is
            constructed outside a full pipeline run (e.g. a unit test that
            only exercises the Spark path). Engine-portable formatters read
            :attr:`functions` / :attr:`engine` instead of touching
            ``pyspark.sql.functions`` directly.
        input_address_name: Filename stem of the input address config.
            Used by :func:`ovid2.formatters.adapters._sources.read_source`
            to resolve a native :class:`Driver` and materialise the source
            through the engine's stager when the engine is not
            Spark-compatible (Snowpark). ``None`` on the Spark path (the
            already-loaded ``input_address`` is used directly).
    """

    spark: SparkSession
    input_address: BaseAddress
    pipeline: str
    batch_id: str
    engine: BaseEngine | None = None
    input_address_name: str | None = None

    @property
    def functions(self) -> EngineFunctions:
        """Return the engine's column-expression DSL.

        Convenience accessor so engine-portable formatters can write
        ``ctx.functions.stable_hash(...)`` instead of reaching for the
        engine object. This is the intended path for the framework design
        recorded in :mod:`ovid2.core.engines.base` — the pipeline never
        imports ``pyspark.sql.functions`` directly.

        Raises:
            RuntimeError: When no engine is attached (the context was built
                without one). This means the formatter needs the portable
                DSL but was driven outside a full pipeline run; attach an
                engine via the pipeline or pass one in a test.
        """
        if self.engine is None:
            raise RuntimeError(
                "AdapterContext has no engine attached; cannot expose "
                "'functions'. Drive the formatter through run_pipeline "
                "(which sets ctx.engine) or pass an engine in a test."
            )
        return self.engine.get_functions()


class BaseAdapter(ABC):
    """Abstract adapter contract.

    Concrete adapters implement :meth:`resolve`, which reads and combines
    the raw sources into a single wide DataFrame with columns that the
    ``projections:`` block can reference directly.
    """

    @abstractmethod
    def resolve(self, spec: FormatterSpec, ctx: AdapterContext) -> DataFrame:
        """Return the pre-projection DataFrame for ``spec``.

        The output DataFrame must be lazy and must expose every column
        that the spec's ``projections`` values reference (using either
        ``source_name.column`` or ``prefix.column`` after joins).
        """
