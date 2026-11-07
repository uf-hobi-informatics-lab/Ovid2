"""Object-store data plane shared by the DuckDB s3 / azure_blob / gcs strategies.

ovid2 addresses on object stores hold the same text artefacts as disc
(``{prefix}/{table}.csv`` single files, or Spark part directories), so
the DuckDB strategies share one flow:

* **Read** — list the objects matching ``file_name`` under the address
  prefix (a single object, a glob, or a Spark part directory), download
  them into the run's scratch directory with the cloud SDK (credentials
  resolved exactly as the Spark/Snowpark paths do), and read them with
  the disc CSV contract. Parquet / JSON objects are read the same way.
* **Write** — write ``{table}.{fmt}`` into scratch with
  :func:`~ovid2.core.engines.duckdb_stager.write_frame_local`, then
  upload it to ``{prefix}/{table}.{fmt}`` (a single object for CSV
  ``single_file``; every part for a directory artefact). Existing
  objects under the target are replaced (overwrite confirmation happens
  upstream in ``cli/etl.py``).

Each store implements :class:`ObjectStoreClient` (list / download /
upload / delete). No ``import duckdb`` here.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, cast

from ovid2.config.settings import CSV_DELIMITER
from ovid2.core.engines.duckdb_stager import (
    DuckDBStagingStrategy,
    local_scratch_dir,
    parquet_glob,
    read_csv_frame,
    write_frame_local,
)

if TYPE_CHECKING:
    from ovid2.core.engines._duck_frame import DuckFrame
    from ovid2.drivers.base import Driver

__all__ = ["ObjectStoreClient", "ObjectStoreStrategy", "merged_config"]


def merged_config(driver: Driver) -> dict[str, Any]:
    """Return the driver's merged ``args + secrets + location`` config."""
    try:
        _, merged = driver.as_legacy_src_args()
        return dict(merged)
    except Exception:
        return dict(getattr(driver, "location", None) or {})


class ObjectStoreClient(ABC):
    """Minimal object-store verbs the strategies need."""

    @abstractmethod
    def list_keys(self, prefix: str) -> list[str]:
        """Return every object key starting with ``prefix``."""

    @abstractmethod
    def download(self, key: str, dest: str) -> None:
        """Download ``key`` to the local path ``dest``."""

    @abstractmethod
    def upload(self, src: str, key: str) -> None:
        """Upload the local file ``src`` to ``key``."""

    @abstractmethod
    def delete(self, keys: list[str]) -> None:
        """Delete ``keys`` (missing keys are ignored)."""


def _join_key(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def _is_data_object(key: str) -> bool:
    base = key.rsplit("/", 1)[-1]
    return bool(base) and not base.startswith(("_", "."))


class ObjectStoreStrategy(DuckDBStagingStrategy):
    """Download -> local DuckDB -> upload strategy for object stores."""

    def client(self, config: dict[str, Any]) -> ObjectStoreClient:  # pragma: no cover - abstract
        """Build the store client from the merged driver config."""
        raise NotImplementedError

    @staticmethod
    def prefix(config: dict[str, Any]) -> str:
        """Return the address key prefix (no leading / trailing slash)."""
        return str(config.get("prefix") or "").strip("/")

    def _resolve_keys(self, client: ObjectStoreClient, prefix: str, file_name: str) -> list[str]:
        """Resolve ``file_name`` to object keys, mirroring disc ``resolve_read_path``.

        * Glob: sorted matches; the first matching *object* wins,
          otherwise every data object of the first matching *directory*
          (a Spark part dir such as ``CONCEPT.csv/part-*.csv``).
        * Plain name: the object itself, or every data object under it.
        """
        target = _join_key(prefix, file_name)
        if any(ch in file_name for ch in "*?["):
            parent = _join_key(prefix, os.path.dirname(file_name))
            candidates = sorted(client.list_keys(parent + "/" if parent else ""))
            objects = [
                k for k in candidates if fnmatch.fnmatchcase(k, target) and _is_data_object(k)
            ]
            if objects:
                return objects[:1]
            in_dirs = [
                k
                for k in candidates
                if _is_data_object(k) and fnmatch.fnmatchcase(os.path.dirname(k), target)
            ]
            if in_dirs:
                first_dir = os.path.dirname(in_dirs[0])
                return [k for k in in_dirs if os.path.dirname(k) == first_dir]
            raise FileNotFoundError(f"No objects matching '{file_name}' under '{prefix}/'")
        keys = client.list_keys(target)
        if target in keys:
            return [target]
        under = sorted(k for k in keys if k.startswith(target + "/") and _is_data_object(k))
        if under:
            return under
        raise FileNotFoundError(f"No object '{file_name}' under '{prefix}/'")

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
        """Download the matching objects and read them with the disc contract."""
        del pipeline, batch_id, table_name
        config = merged_config(driver)
        client = self.client(config)
        keys = self._resolve_keys(client, self.prefix(config), file_name)
        scratch = local_scratch_dir(self._engine)
        local: list[str] = []
        for i, key in enumerate(keys):
            dest = os.path.join(scratch, f"{i:05d}-{os.path.basename(key)}")
            client.download(key, dest)
            local.append(dest)
        fmt = str(config.get("format") or "csv")
        engine: Any = self._engine
        conn = engine.connection()
        lowercase = bool(config.get("lowercase_columns") or False)
        if fmt == "csv":
            from ovid2.drivers.disc.spark import detect_delimiter

            return cast(
                "DuckFrame",
                read_csv_frame(
                    engine,
                    local,
                    delimiter=delimiter or detect_delimiter(local[0]),
                    lowercase_columns=lowercase,
                    name=file_name,
                ),
            )
        if fmt == "parquet":
            frame = engine.wrap(conn.read_parquet(parquet_glob(local)))
        elif fmt == "json":
            frame = engine.wrap(conn.read_json(local))
        else:
            raise ValueError(f"Unsupported format: {fmt}")
        if lowercase:
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
        """Write locally, then upload to ``{prefix}/{table}.{fmt}``."""
        del pipeline, batch_id
        config = merged_config(driver)
        client = self.client(config)
        fmt = str(config.get("format") or "csv")
        single_file = bool(config.get("single_file", False))
        scratch = local_scratch_dir(self._engine)
        artefact = write_frame_local(
            self._engine,
            df,
            scratch,
            table_name,
            fmt=fmt,
            delimiter=delimiter or CSV_DELIMITER,
            single_file=single_file and fmt == "csv",
        )
        target = _join_key(self.prefix(config), f"{table_name}.{fmt}")
        stale = [k for k in client.list_keys(target) if k == target or k.startswith(target + "/")]
        uploads: list[tuple[str, str]] = []
        if os.path.isdir(artefact):
            for name in sorted(os.listdir(artefact)):
                uploads.append((os.path.join(artefact, name), _join_key(target, name)))
        else:
            uploads.append((artefact, target))
        for src, key in uploads:
            client.upload(src, key)
        fresh = {key for _src, key in uploads}
        client.delete([k for k in stale if k not in fresh])
        shutil.rmtree(scratch, ignore_errors=True)
