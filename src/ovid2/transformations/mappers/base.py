"""Base mapper class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame


class BaseMapper(ABC):
    """Abstract base class for table mappers.

    A mapper applies pipeline-specific value dictionaries to a formatted
    DataFrame, producing the final CDM-shaped output.
    """

    @property
    @abstractmethod
    def table_name(self) -> str:
        """Return the CDM table name this mapper targets."""

    @abstractmethod
    def map(self, df: DataFrame, pipeline_dictionaries: Any, pipeline: str) -> DataFrame:
        """Apply dictionary-based mapping to ``df``.

        Args:
            df: Formatted, deduplicated DataFrame.
            pipeline_dictionaries: The pipeline's ``dictionaries`` module.
            pipeline: Pipeline name.

        Returns:
            Mapped DataFrame (still lazy).
        """
