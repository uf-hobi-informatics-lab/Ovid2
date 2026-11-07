"""DuckDB data plane for the disc driver.

The **only** module in :mod:`ovid2.drivers.disc` (besides ``spark.py``
for Spark) that touches an engine library — and it does so only through
the DuckDB engine / stager helpers, never via ``import duckdb``
directly. Mirrors :mod:`ovid2.drivers.disc.spark` exactly:

* Read: ``resolve_read_path`` + ``detect_delimiter`` (shared, pyspark-free
  helpers), then the Spark CSV contract (all VARCHAR, trimmed, empty ->
  NULL) via :func:`~ovid2.core.engines.duckdb_stager.read_csv_relation`;
  Parquet / JSON read natively.
* Write: ``{root}/{table}.{fmt}`` — a single CSV file by default, the
  Spark part-dir shape when ``single_file`` is false.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ovid2.config.settings import CSV_DELIMITER
from ovid2.core.engines.duckdb_stager import (
    DuckDBStagingStrategy,
    parquet_glob,
    read_csv_frame,
    register_duckdb_strategy,
    write_frame_local,
)
from ovid2.drivers.disc.spark import detect_delimiter, resolve_read_path

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.drivers.base import Driver
    from ovid2.drivers.disc.driver import DiscDriver


def read_disc(
    engine: Any,
    root: str,
    file_name: str,
    delimiter: str | None,
    fmt: str,
    lowercase_columns: bool,
) -> DuckFrame:
    """Read ``file_name`` under ``root`` into a lazy :class:`DuckFrame`."""
    path = resolve_read_path(root, file_name)
    conn = engine.connection()
    if fmt == "csv":
        return cast(
            "DuckFrame",
            read_csv_frame(
                engine,
                path,
                delimiter=delimiter or detect_delimiter(path),
                lowercase_columns=lowercase_columns,
                name=file_name,
            ),
        )
    if fmt == "parquet":
        rel = conn.read_parquet(parquet_glob(path))
    elif fmt == "json":
        import os

        target = os.path.join(path, "*.json") if os.path.isdir(path) else path
        rel = conn.read_json(target)
    else:
        raise ValueError(f"Unsupported format: {fmt}")
    frame = engine.wrap(rel)
    if lowercase_columns:
        frame = frame.toDF(*[c.lower() for c in frame.columns])
    return cast("DuckFrame", frame)


@register_duckdb_strategy
class DuckDBDiscStrategy(DuckDBStagingStrategy):
    """disc <-> DuckDB."""

    driver_type = "disc"

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
        """Read from the disc address (exact location; no pipeline/batch suffix)."""
        del pipeline, batch_id, table_name
        view = cast("DiscDriver", driver)._merged_location_view()
        root = cast("DiscDriver", driver)._data_root()
        return read_disc(
            self._engine,
            root or view["root"],
            file_name,
            delimiter,
            view["format"],
            view["lowercase_columns"],
        )

    def persist(
        self,
        df: DuckFrame,
        driver: Driver,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write ``{root}/{table}.{fmt}``."""
        del pipeline, batch_id
        view = cast("DiscDriver", driver)._merged_location_view()
        write_frame_local(
            self._engine,
            df,
            view["root"],
            table_name,
            fmt=view["format"],
            delimiter=delimiter or CSV_DELIMITER,
            single_file=view["single_file"],
        )
