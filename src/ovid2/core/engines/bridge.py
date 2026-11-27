"""Arrow bridge: give every engine every storage type (engine universality).

Each storage type has a complete DuckDB strategy
(``drivers/<type>/duckdb.py``: disc, s3, azure_blob, gcs, snowflake,
postgres, mssql, access, databricks). An engine with no *native* path to
a driver bridges through it:

* **read** — the DuckDB strategy materialises the source (its exact
  Spark-contract text: all VARCHAR, trimmed, empty → NULL), which is
  streamed as Arrow record batches into the target engine
  (``createDataFrame`` from Arrow on Spark, ``write_arrow`` into a
  transient table on Snowpark);
* **write** — the engine's frame is streamed out as Arrow into a DuckDB
  frame and persisted by the DuckDB strategy (same bytes / same typed
  DDL every engine produces for that driver).

The bridge is engine-agnostic: :class:`BridgeEngine` adapters describe
how one engine imports / exports Arrow. Spark's native paths stay
preferred where they exist (``spark_capable`` drivers); Snowpark keeps
its server-side staging strategies; the bridge only fills the gaps, so
the engine × storage matrix is complete
(``tests/contract/test_engine_storage_matrix_complete.py``).

No engine library is imported at module scope.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.drivers.base import Driver

_log = logging.getLogger(__name__)

__all__ = ["bridge_read", "bridge_write", "sidecar"]

#: Arrow batch size for engine hand-offs.
_BATCH_ROWS = 131_072


class _Sidecar:
    """A private DuckDB engine used only to reach storage for another engine."""

    def __init__(self) -> None:
        from ovid2.core.engines.duckdb import DuckDBEngine

        self.engine = DuckDBEngine()
        self.stager = self.engine.get_stager()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.engine.stop_session()


_SIDECARS: dict[int, _Sidecar] = {}


def sidecar(engine: Any) -> _Sidecar:
    """Return (creating once) the DuckDB sidecar attached to ``engine``."""
    key = id(engine)
    car = _SIDECARS.get(key)
    if car is None:
        car = _Sidecar()
        _SIDECARS[key] = car
        cleanup = getattr(engine, "register_cleanup", None)
        if callable(cleanup):
            cleanup(car.close)
        else:
            import atexit

            atexit.register(car.close)
    return car


def _arrow_batches(frame: Any) -> Iterator[Any]:
    """Stream a DuckDB frame as Arrow record batches."""
    yield from frame.to_arrow_batches(_BATCH_ROWS)


def bridge_read(
    engine: Any,
    driver: Driver,
    pipeline: str,
    batch_id: str,
    file_name: str,
    delimiter: str | None = None,
    *,
    table_name: str | None = None,
) -> Any:
    """Read ``driver`` through the DuckDB strategy and import it into ``engine``."""

    car = sidecar(engine)
    frame = car.stager.materialise(
        driver, pipeline, batch_id, file_name, delimiter, table_name=table_name
    )
    return import_arrow(engine, arrow_table(frame), name=table_name or file_name)


def arrow_table(frame: Any) -> Any:
    """Collect a DuckDB frame into a ``pyarrow.Table`` (empty frames keep their schema)."""
    import pyarrow as pa

    batches = list(_arrow_batches(frame))
    if batches:
        return pa.Table.from_batches(batches)
    return frame.to_arrow()


def bridge_write(
    engine: Any,
    df: Any,
    driver: Driver,
    pipeline: str,
    batch_id: str,
    table_name: str,
    delimiter: str | None = None,
) -> None:
    """Export ``df`` from ``engine`` as Arrow and persist it with the DuckDB strategy."""
    car = sidecar(engine)
    table = export_arrow(engine, df)
    frame = car.engine.get_session().from_arrow(table)  # type: ignore[attr-defined]
    car.stager.persist(frame, driver, pipeline, batch_id, table_name, delimiter)


def import_arrow(engine: Any, table: Any, *, name: str) -> Any:
    """Turn a ``pyarrow.Table`` into ``engine``'s native frame."""
    family = str(getattr(getattr(engine, "family", ""), "value", getattr(engine, "family", "")))
    if family == "spark":
        session = engine.get_session()
        if table.num_rows == 0:
            from ovid2.core.engines.pyspark import spark_type

            schema = ", ".join(f"`{f.name}` string" for f in table.schema)
            return session.createDataFrame([], spark_type(schema) if schema else "_c0 string")
        return session.createDataFrame(table.to_pandas(types_mapper=_string_mapper))
    if family == "snowpark":
        return _snowpark_import(engine, table, name=name)
    if family == "duckdb":
        return engine.get_session().from_arrow(table)
    raise RuntimeError(f"bridge: no Arrow import for engine family {family!r}")


def export_arrow(engine: Any, df: Any) -> Any:
    """Turn ``engine``'s frame into a ``pyarrow.Table``."""
    import pyarrow as pa

    family = str(getattr(getattr(engine, "family", ""), "value", getattr(engine, "family", "")))
    if family == "spark":
        # Spark >= 4.0 (the image pins 4.1) streams Arrow batches to the driver.
        return df.toArrow()
    if family == "snowpark":
        # ``to_arrow_batches`` yields ``pyarrow.Table`` chunks (not RecordBatches).
        parts = list(df.to_arrow_batches())
        if parts:
            return pa.concat_tables(
                [p if isinstance(p, pa.Table) else pa.Table.from_batches([p]) for p in parts]
            )
        names = [c[1:-1].replace('""', '"') if c[:1] == c[-1:] == '"' else c for c in df.columns]
        return pa.table({c: pa.array([], pa.string()) for c in names})
    if family == "duckdb":
        return df.to_arrow()
    raise RuntimeError(f"bridge: no Arrow export for engine family {family!r}")


def _string_mapper(arrow_type: Any) -> Any:
    """Keep Arrow strings as Python objects (Spark ``StringType``) in pandas."""
    del arrow_type
    return None


def _snowpark_import(engine: Any, table: Any, *, name: str) -> Any:
    """Load Arrow into a transient table in the Snowpark staging schema."""
    from ovid2.drivers.snowflake.staging.arrow_import import import_arrow

    return import_arrow(engine.native(), table, name=name)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Library module.")
