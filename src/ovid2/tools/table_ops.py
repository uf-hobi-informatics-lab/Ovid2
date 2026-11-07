"""``ovid2 tools describe-table | sample-table | count-rows | export-table``.

Executor-tier commands behind the web Data Space explorer. Each one
resolves a storage account (connection) *with its secrets* — so the web
API process never does — calls one of the driver table-introspection
hooks (:meth:`~ovid2.drivers.base.Driver.describe_table`,
``sample_rows``, ``count_rows``, ``iter_rows``) and prints exactly one
JSON object on the last line of stdout, which the web tier's
``_broker.run_tool_json`` parses.

Addressing: ``<connection> --path <listing path>`` where the path is the
one ``browse-storage`` returned for the ``file`` / ``table`` entry
(relative to the storage-account root): ``landing/2026_03/DEMOGRAPHIC.csv``,
``CHAR_DB/SITE_X/DEMOGRAPHIC``, ``drugs.accdb/NDC``.

JSON schema (every command)::

    {"ok": bool, "supported": bool, "type": str, "path": str,
     "message": str | null, ...payload}

``supported=false`` means the storage type has no such capability
(``NotImplementedError``) — the explorer greys the control out.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ovid2.drivers.base import DriverRowAccess

logger = logging.getLogger(__name__)

__all__ = ["register", "run_table_op"]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the four table subcommands under ``ovid2 tools``."""

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("connection", help="Storage account (connection) name.")
        p.add_argument(
            "--path",
            required=True,
            help="Listing path of the table / data file under the storage account.",
        )

    describe = subparsers.add_parser(
        "describe-table",
        help="Describe one table / data file (fields, row count, size, DDL), print JSON.",
    )
    _common(describe)
    describe.set_defaults(_entry=_describe_entry, _tool="describe-table")

    sample = subparsers.add_parser(
        "sample-table", help="Print the first N rows of a table / data file as JSON."
    )
    _common(sample)
    sample.add_argument("--limit", type=int, default=100, help="Rows (max 1000).")
    sample.set_defaults(_entry=_sample_entry, _tool="sample-table")

    count = subparsers.add_parser(
        "count-rows", help="Exact row count of a table / data file (may scan), print JSON."
    )
    _common(count)
    count.set_defaults(_entry=_count_entry, _tool="count-rows")

    export = subparsers.add_parser(
        "export-table",
        help="Export a table / data file to csv / xlsx / pdf / docx, print JSON.",
    )
    _common(export)
    export.add_argument(
        "--format", dest="fmt", choices=["csv", "xlsx", "pdf", "docx"], default="csv"
    )
    export.add_argument("--delimiter", default=",", help="CSV delimiter (one character; 'tab').")
    export.add_argument(
        "--header-case", choices=["asis", "upper", "lower"], default="asis", dest="header_case"
    )
    export.add_argument(
        "--limit", type=int, default=None, help="Maximum rows (default: the whole table)."
    )
    export.add_argument("--out", required=True, help="Output file path.")
    export.add_argument(
        "--in-process",
        dest="in_process",
        action="store_true",
        help=(
            "Run the export in this process even when unbounded "
            "(default: exports over the host row limit run in a container)."
        ),
    )
    export.set_defaults(_entry=_export_entry, _tool="export-table")


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _driver_for(connection: str) -> tuple[str, DriverRowAccess]:
    """Return ``(type_name, driver)`` for a connection, secrets resolved.

    Narrowed to :class:`~ovid2.drivers.base.DriverRowAccess`: these tools
    are the engine-less **row access** data plane (``describe_table`` is
    on the protocol too).
    """
    from ovid2.config import connections as _connections
    from ovid2.drivers.registry import DRIVERS, instantiate

    raw = _connections.load_connection_config(connection)
    type_name = str(raw.get("type") or "")
    if type_name not in DRIVERS:
        raise LookupError(f"Storage type '{type_name}' is not registered.")
    driver = instantiate(
        type_name,
        args=dict(raw.get("args") or {}),
        secrets=dict(_connections.resolve_secrets(raw)),
    )
    return type_name, driver


def run_table_op(op: str, connection: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """Run one table operation and return its JSON payload (never raises).

    Args:
        op: ``describe`` | ``sample`` | ``count`` | ``export``.
        connection: Storage account name.
        path: Listing path of the table.
        **kwargs: ``limit`` (sample / export), ``fmt`` / ``delimiter`` /
            ``header_case`` / ``out`` (export).
    """
    result: dict[str, Any] = {
        "ok": False,
        "supported": True,
        "type": "",
        "path": path,
        "message": None,
    }
    try:
        type_name, driver = _driver_for(connection)
        result["type"] = type_name
        if op == "describe":
            result["table"] = driver.describe_table(path).to_dict()
        elif op == "sample":
            result["sample"] = driver.sample_rows(path, int(kwargs.get("limit") or 100)).to_dict()
        elif op == "count":
            result["row_count"] = int(driver.count_rows(path))
        elif op == "export":
            from ovid2.exports import export_rows

            columns, batches = driver.iter_rows(path, kwargs.get("limit"))
            delimiter = str(kwargs.get("delimiter") or ",")
            if delimiter.lower() in ("tab", "\\t"):
                delimiter = "\t"
            outcome = export_rows(
                columns,
                batches,
                Path(str(kwargs["out"])),
                fmt=str(kwargs.get("fmt") or "csv"),
                delimiter=delimiter,
                header_case=str(kwargs.get("header_case") or "asis"),
                title=path.rstrip("/").rsplit("/", 1)[-1],
            )
            result["export"] = outcome.to_dict()
        else:
            raise ValueError(f"unknown table op {op!r}")
        result["ok"] = True
    except NotImplementedError as exc:
        result.update(
            ok=True,
            supported=False,
            message=f"Not available for '{result['type'] or 'this'}' storage ({exc}).",
        )
    except (FileNotFoundError, PermissionError, ValueError, KeyError, LookupError) as exc:
        result["message"] = str(exc)
    except Exception as exc:
        logger.exception("table op %s failed for %s:%s", op, connection, path)
        result["message"] = f"{type(exc).__name__}: {exc}"
    return result


def _emit(result: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(result, default=str) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _describe_entry(args: argparse.Namespace) -> int:
    """Entry for ``ovid2 tools describe-table``."""
    return _emit(run_table_op("describe", args.connection, args.path))


def _sample_entry(args: argparse.Namespace) -> int:
    """Entry for ``ovid2 tools sample-table``."""
    return _emit(run_table_op("sample", args.connection, args.path, limit=args.limit))


def _count_entry(args: argparse.Namespace) -> int:
    """Entry for ``ovid2 tools count-rows``."""
    return _emit(run_table_op("count", args.connection, args.path))


def host_export_row_limit() -> int:
    """Row ceiling for an export run in-process on the host.

    Matches the web tier's synchronous-download cap
    (``$OVID2_EXPORT_MAX_ROWS``, default 100 000) so the "Download" button
    keeps its latency; anything larger — or unbounded (the background
    ``export`` job kind) — runs in a driver container so full-table row
    data never passes through a host process.
    """
    import os

    try:
        return max(1, int(os.environ.get("OVID2_EXPORT_MAX_ROWS", "") or 100_000))
    except ValueError:
        return 100_000


def _export_runs_in_container(args: argparse.Namespace) -> bool:
    """Return whether this export must be dispatched to a container.

    In-container invocations always run in-process (we *are* the
    container). On the host, bounded small exports
    (``--limit`` ≤ :func:`host_export_row_limit`) stay in-process for
    latency; full-table exports go to a container. ``--in-process``
    forces the host path (tests / hosts without docker).
    """
    from ovid2.core.docker import is_inside_docker

    if is_inside_docker() or getattr(args, "in_process", False):
        return False
    limit = getattr(args, "limit", None)
    return limit is None or int(limit) > host_export_row_limit()


def _run_export_host_mode(args: argparse.Namespace) -> int:
    """Dispatch a full-table export to a single driver container.

    The container gets the scoped secrets of ``args.connection`` only
    (:func:`ovid2.core.docker._scoped_secret_mounts` reads
    ``args.connection``), the source connection's disc / Access mounts,
    and the artifact's parent directory read-write at the same absolute
    path. Its JSON result line is printed to our stdout unchanged.
    """
    import os

    from ovid2.config import connections as _connections
    from ovid2.core.docker import submit_to_docker
    from ovid2.core.isolation import OvidInstance, cleanup_stale_instances
    from ovid2.drivers.registry import DRIVERS, instantiate
    from ovid2.tools.extra_mounts import ExtraMount

    out = Path(str(args.out)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    mounts: list[ExtraMount] = [
        ExtraMount(host=str(out.parent), container=str(out.parent), mode="rw")
    ]
    try:
        raw = _connections.load_connection_config(args.connection)
        type_name = str(raw.get("type") or "")
        if type_name in DRIVERS:
            driver = instantiate(type_name, args=dict(raw.get("args") or {}), secrets={})
            for req in driver.container_requirements_for({}):
                host = getattr(req, "host_path", None)
                container = getattr(req, "container_path", None)
                if host and container:
                    mounts.append(ExtraMount(host=str(host), container=str(container), mode="ro"))
    except (FileNotFoundError, ValueError, KeyError) as exc:
        return _emit(
            {"ok": False, "supported": True, "type": "", "path": args.path, "message": str(exc)}
        )

    cleanup_stale_instances()
    instance = OvidInstance(instance_id=os.environ.get("OVID2_INSTANCE_ID") or None)
    args.command = "tools export-table"
    args.out = str(out)
    args.parsed_extra_mounts = mounts
    args.no_spark_cluster = True
    print(
        f"[ovid2] export-table: running in a driver container (id={instance.instance_id}).",
        file=sys.stderr,
    )
    return submit_to_docker(args, instance)


def _export_entry(args: argparse.Namespace) -> int:
    """Entry for ``ovid2 tools export-table``."""
    if _export_runs_in_container(args):
        return _run_export_host_mode(args)
    return _emit(
        run_table_op(
            "export",
            args.connection,
            args.path,
            limit=args.limit,
            fmt=args.fmt,
            delimiter=args.delimiter,
            header_case=args.header_case,
            out=args.out,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Registered under `ovid2 tools`; not a script.")
