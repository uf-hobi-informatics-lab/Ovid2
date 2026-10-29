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
from typing import Any

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
    export.set_defaults(_entry=_export_entry, _tool="export-table")


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _driver_for(connection: str) -> tuple[str, Any]:
    """Return ``(type_name, driver)`` for a connection, secrets resolved."""
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


def _export_entry(args: argparse.Namespace) -> int:
    """Entry for ``ovid2 tools export-table``."""
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
