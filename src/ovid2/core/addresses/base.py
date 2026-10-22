"""Base storage-address interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession


class BaseAddress(ABC):
    """Abstract base class for storage addresses.

    An *address* encapsulates one endpoint the pipeline can read from or
    write to (a directory tree, a Snowflake database, a JDBC source, …).
    Concrete implementations are configured via JSON files in ``configs/``
    and loaded through :func:`ovid2.config.addresses.load_address`.
    """

    @abstractmethod
    def __init__(self, config: dict[str, Any]) -> None:
        """Construct the address from its JSON ``src_args`` block.

        Args:
            config: Address-specific configuration.
        """

    @abstractmethod
    def read(
        self,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> DataFrame:
        """Read data as a lazy Spark DataFrame.

        Args:
            spark: Active Spark session.
            pipeline: Pipeline name (used for path/schema scoping).
            batch_id: Batch identifier.
            file_name: Table or file name (may be a glob for disc addresses).
            delimiter: Optional CSV delimiter override.
            table_name: CDM table being loaded (keyword-only). Optional
                hint for addresses that name their objects after CDM
                tables rather than filesystem paths — e.g.
                :class:`SnowflakeAddress` uses it as a fall-back when
                ``file_name`` is a disc-style glob that isn't a valid
                SQL identifier. Addresses that ignore this argument
                should accept it verbatim to keep the interface uniform.

        Returns:
            Lazy Spark DataFrame.
        """

    @abstractmethod
    def write(
        self,
        df: DataFrame,
        spark: SparkSession,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write a DataFrame to this address.

        This is the **only** step in the pipeline that materialises the
        lazy plan.

        Args:
            df: DataFrame to write.
            spark: Active Spark session.
            pipeline: Pipeline name.
            batch_id: Batch identifier.
            table_name: Destination table/file name.
            delimiter: Optional CSV delimiter override.
        """

    def create_complementary_tables(
        self, spark: SparkSession, pipeline: str, batch_id: str
    ) -> None:
        """Create empty CDM placeholders for tables missing from this run.

        Default implementation is a no-op; databases that need placeholders
        (Snowflake) override.

        Args:
            spark: Active Spark session.
            pipeline: Pipeline name.
            batch_id: Batch identifier.
        """
        return None
