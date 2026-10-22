"""Legacy connection + address classes for the disc driver.

Keeps the pre-D3.disc :class:`BaseConnection` /
:class:`BaseAddress` shape alive for the two callers that still need
them:

- :mod:`ovid2.config.connections` — instantiates a connection via
  :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` (dotted path).
- :mod:`ovid2.config.addresses` — instantiates an address via
  :data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` (dotted path).

Both catalogues are retargeted to point at the classes in this module
so every existing import site (``from ovid2.connections.disc import
DiscConnection``) becomes a one-liner rewrite ``from
ovid2.drivers.disc import DiscConnection`` with no behaviour change.

The pipeline-side :class:`DiscAddress` reader/writer delegates to
:mod:`ovid2.drivers.disc.spark` so the pyspark import stays confined
to that single file, keeping ``test_no_pyspark_outside_engine``
green.
"""

from __future__ import annotations

import os
from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult
from ovid2.core.addresses.base import BaseAddress
from ovid2.drivers.disc.metadata import normalise_disc_args
from ovid2.drivers.disc.probe import probe_auth_from_host_path

# NOTE: this module deliberately does NOT import ``pyspark.sql`` — even
# under ``TYPE_CHECKING``. The contract test
# :mod:`tests.contract.test_no_pyspark_outside_engine` inspects imports
# via AST and does not distinguish TYPE_CHECKING guards. Signatures
# below use ``Any`` for the DataFrame / SparkSession types; the real
# type contract is enforced by :class:`BaseAddress`.


class DiscConnection(BaseConnection):
    """Filesystem endpoint. No secrets.

    Args (from the JSON):
        host_path (str, required): Absolute directory on the host.
        container_path (str, optional): Absolute directory inside the
            container. Defaults to ``host_path``.
        mount_mode (str, optional): ``rw`` (default) or ``ro``.

    Legacy JSONs may still carry ``root_path`` instead of
    ``host_path``; :func:`~ovid2.drivers.disc.metadata.normalise_disc_args`
    widens the payload at construction time.
    """

    type_name = "disc"

    def _normalised(self) -> dict[str, Any]:
        """Return the widened arg triple."""
        return normalise_disc_args(self.args)

    @property
    def host_path(self) -> str:
        """Return the host-side path (previously ``root_path``)."""
        return self._normalised()["host_path"]

    @property
    def container_path(self) -> str:
        """Return the container-side path (defaults to :attr:`host_path`)."""
        return self._normalised()["container_path"]

    @property
    def mount_mode(self) -> str:
        """Return the bind-mount mode (``rw`` or ``ro``)."""
        return self._normalised()["mount_mode"]

    def test(self) -> ConnectionTestResult:
        """Return ``ok`` when ``host_path`` exists and is readable."""
        normalised = self._normalised()
        probe = probe_auth_from_host_path(normalised["host_path"], normalised)
        # Downgrade the D1 ProbeResult to the pre-D1 ConnectionTestResult
        # shape the web tier + legacy adapter still consume today.
        return ConnectionTestResult(
            ok=probe.ok,
            code=probe.code,
            message=probe.message,
            detail=dict(probe.detail),
        )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Combine the container root with a per-address sub-path.

        Path resolution is done against ``container_path`` (the value
        the pipeline sees inside the ETL container). ``host_path`` is
        only used by :meth:`test` and by the mount builder — the
        pipeline never sees it directly.

        An empty sub-path resolves to the root itself.
        """
        normalised = self._normalised()
        root = normalised["container_path"] or normalised["host_path"]
        sub = str(location.get("path") or "")
        combined = os.path.join(root, sub) if sub else root
        result: dict[str, Any] = {"path": combined}
        if "format" in location:
            result["format"] = location["format"]
        if "lowercase_columns" in location:
            result["lowercase_columns"] = bool(location["lowercase_columns"])
        if "single_file" in location:
            result["single_file"] = bool(location["single_file"])
        return result


class DiscAddress(BaseAddress):
    """Address for local or network-mounted disc storage.

    Config keys:
        path (str, required): Root directory for pipeline-scoped reads/writes.
        format (str, optional): ``csv`` (default), ``parquet``, or ``json``.
        lowercase_columns (bool, optional): If ``True``, all CSV headers are
            normalized to lowercase at read time. Default ``False``.

    All I/O methods delegate to :mod:`ovid2.drivers.disc.spark` — the
    only file in the disc package permitted to import pyspark.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            self.root_path: str = str(config["path"])
        except KeyError as exc:
            raise ValueError("DiscAddress config missing required 'path' key") from exc
        self.format: str = str(config.get("format", "csv"))
        self.lowercase_columns: bool = bool(config.get("lowercase_columns", False))
        # ``single_file`` matches the pre-2026-08 disc contract when
        # unset — legacy JSONs never carried this key, and legacy
        # callers always got one merged CSV per table.
        single_file_raw = config.get("single_file")
        self.single_file: bool = True if single_file_raw is None else bool(single_file_raw)

    # ------------------------------------------------------------------ read

    def _get_read_path(self, pipeline: str, batch_id: str, file_pattern: str) -> str:
        """Resolve a pipeline-scoped read path (case-insensitive, glob-aware).

        Preserved as a public method (via the double-underscore-free
        name) because :mod:`ovid2.core.pipeline` and the Datavant
        hash-token pre-processor peek at it. Delegates to the module-
        level helper in :mod:`ovid2.drivers.disc.spark`.
        """
        from ovid2.drivers.disc.spark import resolve_read_path

        return resolve_read_path(self.root_path, pipeline, batch_id, file_pattern)

    def _detect_delimiter(self, file_path: str) -> str:
        """Auto-detect the delimiter of a CSV file from its first line."""
        from ovid2.drivers.disc.spark import detect_delimiter

        return detect_delimiter(file_path)

    def read(
        self,
        spark: Any,
        pipeline: str,
        batch_id: str,
        file_name: str,
        delimiter: str | None = None,
        *,
        table_name: str | None = None,
    ) -> Any:
        """Read a DataFrame from disc.

        ``table_name`` is accepted for interface uniformity with
        :class:`SnowflakeAddress` (which uses it as a fall-back when
        ``file_name`` is a glob) but is ignored here — disc reads
        always resolve through ``file_name`` and the on-disk layout.
        """
        del table_name  # unused; disc reads always use file_name
        from ovid2.drivers.disc.spark import read_dataframe

        return read_dataframe(
            spark,
            self.root_path,
            pipeline,
            batch_id,
            file_name,
            delimiter,
            self.format,
            self.lowercase_columns,
        )

    # ----------------------------------------------------------------- write

    def _get_write_path(self, pipeline: str, batch_id: str) -> str:
        """Return the write directory for ``{root_path}/{pipeline}/{batch_id}``."""
        return os.path.join(self.root_path, pipeline, batch_id)

    def write(
        self,
        df: Any,
        spark: Any,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Write a DataFrame, merging Spark part-files into one output file."""
        del spark  # unused; write_dataframe uses the writer built into the DF
        from ovid2.drivers.disc.spark import write_dataframe

        write_dataframe(
            df,
            self.root_path,
            pipeline,
            batch_id,
            table_name,
            delimiter,
            self.format,
            single_file=self.single_file,
        )


__all__ = ["DiscAddress", "DiscConnection"]
