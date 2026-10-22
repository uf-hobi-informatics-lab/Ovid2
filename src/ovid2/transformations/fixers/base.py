"""Base class for common fixers.

Every fixer implements a single, reusable data-quality correction that
operates on a Spark DataFrame using lazy transformations. Fixers receive
a context dict containing reference DataFrames and configuration needed
for cross-table operations.

Named parameter aliases (P10)
=============================

Historically fixer configs used two positional fields — ``src1`` and
``src2`` — whose meaning varied per fixer (sometimes "reference table",
sometimes "target column", sometimes "list file name"). New configs may
use per-fixer named fields declared in :attr:`BaseFixer.ALIASES`. The
:class:`~ovid2.transformations.fix.FixTransformer` normalises every
config entry into the legacy ``src1`` / ``src2`` keys before calling
:meth:`apply`, so existing fixer implementations do not need to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class BaseFixer(ABC):
    """Abstract base class for data-quality fix operations.

    All operations MUST be lazy — no ``.collect()``, ``.toPandas()``, or
    intermediate CSV writes. Enforced by
    ``tests/contract/test_no_collect_in_src.py``.

    Attributes:
        ALIASES: Optional mapping ``{named_field: 'src1' | 'src2'}``
            describing the canonical name for each positional field the
            fixer expects. Subclasses override this to provide
            self-documenting config keys; the runner rewrites named
            fields into ``src1`` / ``src2`` before calling :meth:`apply`.
        SCOPE: Optional CDM scope classifier — either ``'generic'``
            (the fix depends only on data-driven column names and works
            across CDMs) or a specific CDM name like ``'pcornet_7_0'``
            when the fix hard-codes CDM-specific columns, tables, or
            valuesets. Consulted by
            :data:`ovid2.transformations.fixers.common.registry.FIXER_SCOPES`
            and enforced by contract test ``test_fixer_cdm_scope``.
    """

    ALIASES: ClassVar[dict[str, str]] = {}
    SCOPE: ClassVar[str] = "generic"

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the fix name identifier (e.g. ``'patid_orphans'``)."""

    @abstractmethod
    def apply(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Apply the fix to a DataFrame.

        Args:
            df: The DataFrame to fix.
            context: Runtime context. Common keys:

                - ``input_address``: For reading reference tables.
                - ``spark``: Active Spark session.
                - ``pipeline``: Pipeline name.
                - ``batch_id``: Batch identifier.
                - ``src1``: Fix-specific parameter 1 (varies).
                - ``src2``: Fix-specific parameter 2 (varies).

        Returns:
            The fixed DataFrame (still lazy).
        """
