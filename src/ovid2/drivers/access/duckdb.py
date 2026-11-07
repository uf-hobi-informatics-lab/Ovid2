"""DuckDB data plane for the access driver (read-only, UCanAccess via JDBC).

Reads stream ``SELECT *`` from the ``.accdb`` / ``.mdb`` file through
:func:`ovid2.drivers.access.reader.iter_rows_no_spark` (jaydebeapi +
UCanAccess, the Spark-free reader the Data Space explorer uses) into
scratch Parquet (all VARCHAR). Access is a source only: ``persist``
raises, exactly like the Spark path.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast

from ovid2.core.engines.duckdb_stager import (
    DuckDBStagingStrategy,
    local_scratch_dir,
    register_duckdb_strategy,
)
from ovid2.drivers._dbapi_duckdb import rows_to_parquet

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.drivers.base import Driver


@register_duckdb_strategy
class DuckDBAccessStrategy(DuckDBStagingStrategy):
    """access -> DuckDB (read-only)."""

    driver_type = "access"

    def materialise(
        self,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> DuckFrame:
        """Spool one Access table into scratch Parquet."""
        del pipeline, batch_id, delimiter
        from ovid2.drivers.access.reader import iter_rows_no_spark

        engine: Any = self._engine
        table = table_name or file_name
        path = _readable_file(driver)
        columns, batches = iter_rows_no_spark(path, table, None, 50_000)
        target = os.path.join(local_scratch_dir(engine), f"{table}.parquet")
        rows_to_parquet(columns, batches, target)
        frame = engine.read_parquet(target)
        if bool((getattr(driver, "location", None) or {}).get("lowercase_columns")):
            frame = frame.toDF(*[c.lower() for c in frame.columns])
        return cast("DuckFrame", frame)

    def persist(
        self,
        df: DuckFrame,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Access addresses are read-only."""
        raise NotImplementedError("access is a read-only driver; it cannot be an output address")


def _readable_file(driver: Driver) -> str:
    """Return the ``.accdb`` path visible to this process (container or host)."""
    container = driver._container_file_path()  # type: ignore[attr-defined]
    if os.path.exists(container):
        return str(container)
    return str(driver._host_file_path_for(driver.location))  # type: ignore[attr-defined]
