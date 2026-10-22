"""Base formatter class.

Pipeline formatters are usually declarative YAML specs executed by
:mod:`ovid2.formatters.engine` (P4). This module defines the *escape hatch*
class that ``adapter: custom`` specs point at.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

    from ovid2.formatters.adapters.base import AdapterContext


class BaseFormatter(ABC):
    """Abstract base class for hand-written formatter classes.

    All pipeline formatters that cannot be expressed as YAML specs (rare —
    only ~5-10% of the total, e.g. ACME VITAL) subclass this and implement
    :meth:`format`. Everything must be lazy.
    """

    #: Runtime context (Spark session, input address, pipeline, batch).
    #: Populated by :class:`~ovid2.formatters.adapters.custom.CustomAdapter`
    #: immediately before :meth:`format` so multi-source custom formatters
    #: can read sibling source files through the same code path the
    #: declarative adapters use. ``None`` when a formatter is driven
    #: outside the custom adapter (e.g. a direct unit test).
    _context: AdapterContext | None = None

    def set_context(self, ctx: AdapterContext) -> None:
        """Attach the adapter runtime context.

        Backward-compatible: formatters that never touch :attr:`context`
        behave exactly as before. Multi-source custom formatters use it to
        read additional OMOP source files with correct globs / excludes via
        :func:`ovid2.formatters.adapters._sources.read_source`.
        """
        self._context = ctx

    @property
    def context(self) -> AdapterContext:
        """Return the attached :class:`AdapterContext`.

        Raises:
            RuntimeError: When accessed before :meth:`set_context` — this
                means the formatter needs sibling-source access but was not
                driven through the custom adapter.
        """
        if self._context is None:
            raise RuntimeError(
                f"{type(self).__name__} requires an AdapterContext (multi-source "
                "formatter) but none was attached. Drive it through the custom "
                "adapter, or call set_context(...) in a test."
            )
        return self._context

    @property
    @abstractmethod
    def table_name(self) -> str:
        """Return the CDM table name (e.g. ``'condition'``)."""

    @property
    @abstractmethod
    def input_file_name(self) -> str:
        """Return the input file glob (e.g. ``'*condition_occurrence_*.csv'``)."""

    @property
    def auxiliary_tables(self) -> list[str]:
        """Return auxiliary table names to register as temp views.

        Override when the formatter needs cross-file lookups. Default: none.
        """
        return []

    @property
    def primary_source_optional(self) -> bool:
        """Whether the table's primary input source is optional.

        When ``True`` and the primary source file is absent, the pipeline's
        eager placeholder read (``core.pipeline._read_with_pipeline_hook``)
        degrades to an empty DataFrame instead of raising
        ``FileNotFoundError`` — the self-reading adapters
        (``source_passthrough`` / ``union_of_specs`` / ``custom``) already
        honour ``optional`` on their own re-read, so the table produces an
        empty CDM-shaped output rather than failing the whole run.

        Default ``False`` preserves the historical hard-fail behaviour for
        tables whose primary input must be present.
        """
        return False

    @abstractmethod
    def format(self, df: DataFrame) -> DataFrame:
        """Transform the raw source into CDM-shaped output.

        This MUST be lazy — no ``.collect()`` / ``.count()`` / intermediate
        writes. The pipeline's final ``output_address.write()`` is the only
        materialisation point.

        Args:
            df: Raw source DataFrame.

        Returns:
            CDM-shaped lazy DataFrame.
        """
