"""Base transformer class for all ETL operations.

Every transformer implements :meth:`transform(df, context) -> DataFrame` and
receives the same context dictionary. The pipeline chains transformers in
canonical order: format → deduplicate → map → fix.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class BaseTransformer(ABC):
    """Abstract base class for all pipeline transformers.

    Concrete transformers extend this class and implement :meth:`transform`.
    Everything must remain lazy — contract tests reject ``.collect()`` and
    friends inside subclass implementations.
    """

    @abstractmethod
    def transform(self, df: DataFrame, context: dict[str, Any]) -> DataFrame:
        """Apply this transformation to a DataFrame.

        Args:
            df: Input Spark DataFrame.
            context: Pipeline context dict. Common keys:

                - ``table_name`` (str): CDM table name.
                - ``pipeline`` (str): Pipeline name.
                - ``input_address``: Input :class:`BaseAddress` instance.
                - ``spark``: Active Spark session.
                - ``batch_id`` (str): Batch identifier.
                - ``event_bus``: The instance's event bus (P3).

        Returns:
            Transformed Spark DataFrame (still lazy).
        """
