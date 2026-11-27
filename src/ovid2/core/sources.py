"""Resolved, read-only source handles for algorithms.

Formatters, adapters and health checks read their inputs through a
:class:`SourceHandle` built by the pipeline (``core.loaders`` /
``cli``). The handle owns every storage concern an algorithm must not
know about:

* resolving an address *name* to a native :class:`~ovid2.drivers.base.Driver`
  (``driver_from_address``);
* choosing between the engine's native data plane (Spark-family engines
  with a legacy address) and the engine's
  :class:`~ovid2.core.engines.stager.EngineStager` (every other engine,
  and Spark runs whose input has no native Spark plane);
* advertising whether rows carry a local file path
  (:attr:`SourceHandle.is_local_path`) instead of the algorithm
  type-checking a concrete address class.

Algorithm trees (``formatters`` / ``mappers`` / ``transforms`` /
``transformations``) import this module, never ``ovid2.drivers`` or
``ovid2.connections`` (``tests/contract/test_algorithms_engine_and_storage_free.py``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.core.addresses.base import BaseAddress
    from ovid2.core.engines.base import BaseEngine

__all__ = ["SourceHandle", "local_file_reader", "read_local_file"]


@dataclass
class SourceHandle:
    """Read-only view of one run input: data plane + stager, already resolved.

    Attributes:
        session: Active engine session (the Spark session on the native
            Spark plane).
        input_address: The legacy address object (native Spark plane), or
            ``None`` when the input has no Spark data plane.
        pipeline: Pipeline name (label).
        batch_id: Batch label.
        engine: Active compute engine; ``None`` only for legacy unit tests
            that drive the Spark plane directly.
        input_address_name: Address config name, resolved to a driver on the
            stager path.
    """

    session: Any
    input_address: BaseAddress | None
    pipeline: str
    batch_id: str
    engine: BaseEngine | None = None
    input_address_name: str | None = None

    @property
    def uses_stager(self) -> bool:
        """Whether reads go through the engine's stager (not the native plane)."""
        engine = self.engine
        return engine is not None and (
            not getattr(engine, "session_is_spark", True) or self.input_address is None
        )

    @property
    def is_local_path(self) -> bool:
        """Whether frames read here expose per-row local file names.

        ``True`` only on the native plane of an address that declares
        ``is_local_path`` (disc). Algorithms use it to decide whether a
        file-name based filter (``exclude:`` globs) is meaningful.
        """
        if self.uses_stager:
            return False
        return bool(getattr(self.input_address, "is_local_path", False))

    def read(self, glob: str, *, table_name: str | None = None) -> Any:
        """Read ``glob`` as a lazy engine-native frame.

        Raises:
            FileNotFoundError: When no file / object matches.
            ValueError: When the stager path is needed but no address name
                was supplied.
        """
        if self.uses_stager:
            return _read_via_stager(
                engine=self.engine,
                pipeline=self.pipeline,
                batch_id=self.batch_id,
                glob=glob,
                table_name=table_name,
                input_address_name=self.input_address_name,
            )
        if self.input_address is None:
            raise ValueError("SourceHandle: no input address and no engine to stage through.")
        return self.input_address.read(
            self.session,
            self.pipeline,
            self.batch_id,
            glob,
            table_name=table_name,
        )


def _read_via_stager(
    *,
    engine: Any,
    pipeline: str,
    batch_id: str,
    glob: str,
    table_name: str | None,
    input_address_name: str | None,
) -> Any:
    """Materialise ``glob`` through the engine's stager."""
    if not input_address_name:
        raise ValueError(
            "read_source: a non-Spark engine reads through the stager and "
            "needs the input address name, but 'input_address_name' was not "
            "supplied. Thread ctx.input_address_name through the adapter "
            "context (run_pipeline / load_formatter do this automatically)."
        )
    from ovid2.drivers.registry import driver_from_address

    driver = driver_from_address(input_address_name, pipeline=pipeline)
    return engine.get_stager().materialise(
        driver,
        pipeline,
        batch_id,
        glob,
        table_name=table_name,
    )


def read_local_file(
    engine: BaseEngine,
    path: str,
    delimiter: str,
    *,
    pipeline: str = "",
    batch_id: str = "",
    table_name: str | None = None,
) -> Any:
    """Load one local delimited file through ``engine``'s stager.

    A synthetic ``disc`` driver rooted at the file's directory lets every
    engine use its own bridge: Spark reads the CSV directly; Snowpark
    ``PUT``s it to a stage; DuckDB scans it.
    """
    from ovid2.drivers.disc.driver import DiscDriver

    parent = os.path.dirname(path)
    driver = DiscDriver(
        args={"host_path": parent, "container_path": parent},
        secrets={},
        location={"format": "csv"},
    )
    base = os.path.basename(path)
    return engine.get_stager().materialise(
        driver,
        pipeline,
        batch_id,
        base,
        delimiter,
        table_name=table_name if table_name is not None else os.path.splitext(base)[0],
    )


def local_file_reader(
    engine: BaseEngine, *, pipeline: str = "", batch_id: str = ""
) -> Callable[[str, str], Any]:
    """Return a ``(path, delimiter) -> frame`` reader bound to ``engine``."""

    def _reader(path: str, delimiter: str) -> Any:
        return read_local_file(engine, path, delimiter, pipeline=pipeline, batch_id=batch_id)

    return _reader


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("ovid2.core.sources is a library module.")
