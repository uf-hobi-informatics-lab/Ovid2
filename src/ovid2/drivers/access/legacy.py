"""Legacy connection + address classes for the Access driver.

Keeps the pre-Driver-refactor :class:`BaseConnection` /
:class:`BaseAddress` shape alive for the two catalogues that resolve
classes by dotted path:

- :data:`ovid2.connections.registry.CONNECTION_TYPE_MAP` →
  :class:`AccessConnection`.
- :data:`ovid2.config.addresses.ADDRESS_TYPE_MAP` →
  :class:`AccessAddress`.

The pipeline-side :class:`AccessAddress` reader delegates to
:mod:`ovid2.drivers.access.spark` so the pyspark/JDBC import stays
confined to that single file (keeps ``test_no_pyspark_outside_engine``
green).
"""

from __future__ import annotations

import os
from typing import Any

from ovid2.connections.base import BaseConnection, ConnectionTestResult
from ovid2.core.addresses.base import BaseAddress
from ovid2.drivers.access.metadata import normalise_access_args
from ovid2.drivers.access.probe import probe_auth_from_host_path

# NOTE: this module deliberately does NOT import ``pyspark.sql`` — even
# under ``TYPE_CHECKING``. Signatures below use ``Any`` for the
# DataFrame / SparkSession types; the real contract is enforced by
# :class:`BaseAddress`.


class AccessConnection(BaseConnection):
    """Microsoft Access file endpoint. No secrets.

    Args (from the JSON):
        host_path (str, required): Absolute directory on the host that
            holds the ``.accdb`` / ``.mdb`` file(s).
        container_path (str, optional): Path inside the container.
            Defaults to ``host_path``.
        mount_mode (str, optional): ``ro`` (default) or ``rw``.
    """

    type_name = "access"

    def _normalised(self) -> dict[str, Any]:
        """Return the widened arg triple."""
        return normalise_access_args(self.args)

    @property
    def host_path(self) -> str:
        """Return the host-side directory path."""
        return str(self._normalised()["host_path"])

    @property
    def container_path(self) -> str:
        """Return the container-side directory path."""
        return str(self._normalised()["container_path"])

    @property
    def mount_mode(self) -> str:
        """Return the bind-mount mode (``ro`` or ``rw``)."""
        return str(self._normalised()["mount_mode"])

    def test(self) -> ConnectionTestResult:
        """Return ``ok`` when ``host_path`` exists and is readable."""
        normalised = self._normalised()
        probe = probe_auth_from_host_path(normalised["host_path"], normalised)
        return ConnectionTestResult(
            ok=probe.ok,
            code=probe.code,
            message=probe.message,
            detail=dict(probe.detail),
        )

    def as_address_config(self, location: dict[str, Any]) -> dict[str, Any]:
        """Combine the container directory with a per-address file path."""
        normalised = self._normalised()
        root = normalised["container_path"] or normalised["host_path"]
        sub = str(location.get("path") or "")
        combined = os.path.join(root, sub) if sub else root
        result: dict[str, Any] = {"path": combined}
        for key in ("tables", "exclude_tables"):
            if key in location:
                result[key] = location[key]
        if "lowercase_columns" in location:
            result["lowercase_columns"] = bool(location["lowercase_columns"])
        return result


class AccessAddress(BaseAddress):
    """Address for a Microsoft Access database file.

    Config keys:
        path (str, required): Container-visible path to the ``.accdb`` /
            ``.mdb`` file.
        lowercase_columns (bool, optional): Lowercase column names on
            read. Default ``False``.

    Reads delegate to :mod:`ovid2.drivers.access.spark` — the only file
    in the package permitted to import pyspark / touch JDBC.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        try:
            self.file_path: str = str(config["path"])
        except KeyError as exc:
            raise ValueError("AccessAddress config missing required 'path' key") from exc
        self.lowercase_columns: bool = bool(config.get("lowercase_columns", False))

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
        """Read one Access table (keyed on ``table_name``/``file_name``)."""
        del pipeline, batch_id, delimiter
        from ovid2.drivers.access.spark import read_table

        target_table = table_name or file_name
        if not target_table:
            raise ValueError("AccessAddress.read requires a table_name or file_name.")
        return read_table(
            spark,
            self.file_path,
            target_table,
            self.lowercase_columns,
        )

    def write(
        self,
        df: Any,
        spark: Any,
        pipeline: str,
        batch_id: str,
        table_name: str,
        delimiter: str | None = None,
    ) -> None:
        """Access is a read-only source — writes are unsupported."""
        raise NotImplementedError(
            "AccessAddress is a read-only source; it cannot be a write "
            "destination. Route the copy to a disc / snowflake / object "
            "store address instead."
        )


__all__ = ["AccessAddress", "AccessConnection"]
