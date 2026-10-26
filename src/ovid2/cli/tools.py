"""``ovid2 tools`` — ad-hoc utilities.

Ships several commands:

- ``raw_upload`` — reads every file under a pipeline batch directory
  and writes it (unchanged, string-typed) to the output address.
  Handy for archiving source-of-truth data before the ETL pipeline
  transforms it.
- ``test-engine`` — probe a compute engine and print JSON.
- ``migrate-addresses`` — W7 legacy → connection-shape.
- ``import-cdm`` — xlsx → ``resources/cdms/<cdm>/`` YAML (C1).
- ``import-vocabulary`` — slice OMOP CONCEPT.csv into per-vocabulary
  YAMLs under ``resources/semantic/vocabularies/`` (S3).
- ``copy`` — one-shot address-to-address data move (legacy shim).
- ``move`` — driver-native successor to ``copy`` (M1). Same CLI
  surface; data plane routes through :mod:`ovid2.drivers.registry`.
- ``new-pipeline`` — scaffold a new pipeline package from a pipeline
  template (copies an existing pipeline and rewrites its identity).
- ``ods`` — Ovid Data Space read-only helpers.

``raw_upload`` writes one table per file (``RAW_<pipeline>_<batch>_<file>``)
so the output address can persist a per-run raw snapshot.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

from ovid2.core.docker import is_inside_docker
from ovid2.core.events import instance_finished, instance_started
from ovid2.core.isolation import OvidInstance
from ovid2.core.loaders import load_address, load_engine

logger = logging.getLogger(__name__)


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the ``tools`` subcommand tree on the top-level parser."""
    parser = subparsers.add_parser(
        "tools",
        help="Ad-hoc tools (raw_upload, migrate-addresses, import-cdm, import-vocabulary, ...).",
        description="One-off utilities that share the Ovid2 address + engine stack.",
    )
    parser.set_defaults(_command="tools")
    tool_subparsers = parser.add_subparsers(dest="tool", metavar="TOOL")

    raw = tool_subparsers.add_parser(
        "raw_upload",
        help="Upload raw source files to the output address without formatting.",
    )
    raw.add_argument("--input_address", required=True)
    raw.add_argument("--output_address", required=True)
    raw.add_argument("--pipeline", required=True)
    raw.add_argument("--batch_id", required=True)
    raw.add_argument("--engine", default="pyspark")
    raw.add_argument(
        "--file",
        nargs="+",
        default=None,
        help="Explicit file names / globs to upload. Defaults to every "
        "file in the batch directory.",
    )
    raw.set_defaults(_entry=_raw_upload_entry, _tool="raw_upload")

    # ``ovid2 tools test-engine <name>`` — probe an engine and emit JSON.
    test_engine = tool_subparsers.add_parser(
        "test-engine",
        help="Probe a compute engine and print a JSON result to stdout.",
        description=(
            "Loads the named engine (either a config file under "
            "$XDG_CONFIG_HOME/ovid2/engines/ or a bare type like 'pyspark'), "
            "calls .test(), and prints the result as JSON on the last line "
            "of stdout. Consumed by the web tier's POST "
            "/api/v1/engines/{name}/test endpoint via subprocess."
        ),
    )
    test_engine.add_argument(
        "name",
        help="Engine config name, or a bare type name (pyspark / databricks_spark / snowpark).",
    )
    test_engine.set_defaults(_entry=_test_engine_entry, _tool="test-engine")

    # ``ovid2 tools probe-connection <name>`` — resolve a connection's
    # secrets and run its auth probe *in the executor trust domain*, then
    # emit a JSON result. The web tier shells out to this so the
    # long-lived API process never resolves plaintext secrets. See
    # .opencode/knowledge/delta/adr-harness-api-executor-trust-model.md.
    probe_conn = tool_subparsers.add_parser(
        "probe-connection",
        help="Resolve a connection's secrets, probe it, print JSON.",
        description=(
            "Loads the named connection (secrets included), runs the "
            "driver auth probe, and prints one JSON object on the last "
            "line of stdout. Consumed by the web tier's POST "
            "/api/v1/connections/{name}/test endpoint via subprocess so "
            "the API process itself never holds plaintext credentials."
        ),
    )
    probe_conn.add_argument("name", help="Connection config name.")
    probe_conn.set_defaults(_entry=_probe_connection_entry, _tool="probe-connection")

    # ``ovid2 tools browse-storage <name> [--prefix P]`` — list one level
    # of a storage account, resolving secrets in the executor domain.
    browse = tool_subparsers.add_parser(
        "browse-storage",
        help="List one level of a storage account, print JSON.",
        description=(
            "Resolves the named storage account (secrets included), calls "
            "the driver's list_dir capability, and prints one JSON object "
            "on the last line of stdout. Consumed by the web tier's "
            "storage-browse endpoint via subprocess so the API process "
            "never holds plaintext credentials."
        ),
    )
    browse.add_argument("name", help="Storage account (connection) name.")
    browse.add_argument(
        "--prefix",
        default=None,
        help="Sub-path/prefix to descend into.",
    )
    browse.set_defaults(_entry=_browse_storage_entry, _tool="browse-storage")

    # ``ovid2 tools list-source-tables <address> --pipeline P --batch_id B``
    # — expand a copy/move ``all`` request to concrete table specs (largest
    # first) in the executor trust domain. The web tier calls this at
    # draft/submit time so the reviewed draft shows the real roster.
    list_src = tool_subparsers.add_parser(
        "list-source-tables",
        help="Expand a copy/move source to its concrete tables, print JSON.",
        description=(
            "Resolves the named source address (secrets included), calls "
            "the driver's expand_all_tables + table_sizes capabilities, "
            "and prints one JSON object on the last line of stdout: "
            '{"ok", "supported", "type", "tables": [spec...], '
            '"sizes": {file_name: bytes}, "message"}. Tables are ordered '
            "largest first when sizes are known. Metadata only — no data "
            "is read."
        ),
    )
    list_src.add_argument("address", help="Source address config name.")
    list_src.add_argument("--pipeline", required=True, help="Pipeline name.")
    list_src.add_argument("--batch_id", required=True, help="Batch identifier.")
    list_src.set_defaults(_entry=_list_source_tables_entry, _tool="list-source-tables")

    # ``ovid2 tools probe-output <address>`` — the overwrite gate's
    # pre-flight: does the output location already hold tables? The
    # address is the exact write location, so the web tier asks the
    # operator to confirm before a run overwrites existing data.
    probe_out = tool_subparsers.add_parser(
        "probe-output",
        help="List the tables already present at an output address, print JSON.",
        description=(
            "Resolves the named output address (secrets included) and lists "
            "the tables already at its exact location. Prints one JSON object "
            '{"ok", "known", "type", "tables": [name...], "message"} on the '
            "last line of stdout. Metadata only — no data is read."
        ),
    )
    probe_out.add_argument("address", help="Output address config name.")
    probe_out.set_defaults(_entry=_probe_output_entry, _tool="probe-output")

    # ``ovid2 tools test-llm <name>`` / ``ovid2 tools llm-chat`` — LLM
    # executor commands (L1). The web tier uses the faster
    # ``python -m ovid2.llm`` entry; these are the operator-facing twins.
    test_llm = tool_subparsers.add_parser(
        "test-llm",
        help="Probe an LLM connection (resolves its API key), print JSON.",
    )
    test_llm.add_argument("name", help="LLM connection name.")
    test_llm.set_defaults(_entry=_test_llm_entry, _tool="test-llm")

    llm_chat = tool_subparsers.add_parser(
        "llm-chat",
        help="Run one routed LLM chat turn; request JSON on stdin, JSON lines out.",
    )
    llm_chat.set_defaults(_entry=_llm_chat_entry, _tool="llm-chat")

    # ``ovid2 tools migrate-addresses`` — W7 legacy → connection-shape.
    from ovid2.tools import migrate_addresses as _migrate_addresses

    _migrate_addresses.register(tool_subparsers)

    # ``ovid2 tools import-cdm`` — xlsx → resources/cdms/<cdm>/ YAML (C1).
    from ovid2.tools import import_cdm as _import_cdm

    _import_cdm.register(tool_subparsers)

    # ``ovid2 tools import-vocabulary`` — slice OMOP CONCEPT.csv into
    # per-vocabulary YAMLs under resources/semantic/vocabularies/ (S3).
    from ovid2.tools import import_vocabulary as _import_vocabulary

    _import_vocabulary.register(tool_subparsers)

    # ``ovid2 tools copy`` — one-shot address-to-address data move (C1).
    from ovid2.tools import copy as _copy

    _copy.register(tool_subparsers)

    # ``ovid2 tools move`` — driver-native successor to ``copy`` (M1).
    from ovid2.tools import move as _move

    _move.register(tool_subparsers)

    # ``ovid2 tools new-pipeline`` — scaffold a pipeline package from a
    # pipeline template (pipeline-centric panel, Phase 3).
    from ovid2.tools import new_pipeline as _new_pipeline

    _new_pipeline.register(tool_subparsers)

    # ``ovid2 tools ods ...`` — Ovid Data Space read-only helpers.
    _register_ods(tool_subparsers)

    def _fallback(_args: argparse.Namespace) -> int:
        parser.print_help(sys.stderr)
        return 2

    parser.set_defaults(_entry=_fallback)


def _raw_upload_entry(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools raw_upload``."""
    # Host mode delegates to Docker; container mode does the work.
    if not is_inside_docker():
        print(
            "[ovid2] tools raw_upload must currently be run inside the Ovid2 "
            "container (host-side dispatch lands in P6).",
            file=sys.stderr,
        )
        return 2

    instance = OvidInstance(instance_id=os.environ.get("OVID2_INSTANCE_ID") or None)

    from ovid2.config.settings import WORK_DIR
    from ovid2.display.bus import JsonlEventBus

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    bus = JsonlEventBus(WORK_DIR / f"events-{instance.instance_id}.jsonl")
    bus.emit(instance_started(instance_id=instance.instance_id, pipeline=args.pipeline))

    input_address = load_address(args.input_address, pipeline=args.pipeline)
    output_address = load_address(args.output_address, pipeline=args.pipeline)
    engine = load_engine(args.engine)
    # raw_upload reads/writes through the Spark-typed ``BaseAddress`` API;
    # ``cli/`` may not import pyspark (even for typing), so keep it ``Any``.
    spark: Any = engine.get_session()

    files = args.file or _discover_batch_files(input_address, args.pipeline, args.batch_id)
    failures = 0
    for file_name in files:
        try:
            table_name = _raw_table_name(args.pipeline, args.batch_id, file_name)
            df = input_address.read(
                spark,
                args.pipeline,
                args.batch_id,
                file_name,
                table_name=table_name,
            )
            output_address.write(df, spark, args.pipeline, args.batch_id, table_name)
        except Exception as exc:
            logger.exception("raw_upload failed for %s: %s", file_name, exc)
            failures += 1

    engine.stop_session()
    bus.emit(instance_finished(instance_id=instance.instance_id, success=failures == 0))
    bus.close()
    return 0 if failures == 0 else 1


def _discover_batch_files(input_address: object, pipeline: str, batch_id: str) -> list[str]:
    """Return the file names directly inside a disc address directory.

    The address is the exact location of the data; ``pipeline`` /
    ``batch_id`` do not contribute path segments.
    """
    del pipeline, batch_id
    from ovid2.drivers.disc import DiscAddress

    if not isinstance(input_address, DiscAddress):
        raise RuntimeError(
            "raw_upload requires --file when the input address is not a DiscAddress."
        )

    batch_dir = Path(input_address.root_path)
    if not batch_dir.is_dir():
        raise FileNotFoundError(f"Batch directory not found: {batch_dir}")
    return [entry.name for entry in sorted(batch_dir.iterdir()) if entry.is_file()]


def _raw_table_name(pipeline: str, batch_id: str, file_name: str) -> str:
    """Return a stable table name for a raw upload target."""
    base = file_name.split("/")[-1]
    stem = base.rsplit(".", 1)[0]
    safe_batch = batch_id.replace("/", "_").replace("-", "_")
    safe_stem = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in stem)
    return f"RAW_{pipeline.upper()}_{safe_batch}_{safe_stem}".upper()


def _test_engine_entry(args: argparse.Namespace) -> int:
    """Probe the named engine and print a JSON result on stdout.

    The JSON schema mirrors :class:`~ovid2.core.engines.base.EngineTestResult`::

        {"ok": bool, "code": str, "message": str, "detail": dict}

    The web tier's ``engines_svc.test_engine`` parses the last JSON line
    of this subprocess's stdout, so we deliberately keep the output
    minimal: one JSON object, one newline, nothing else on stdout.
    Diagnostics from the engine's own logging path land on stderr.
    """
    import json as _json

    result: dict[str, object] = {}
    try:
        engine = load_engine(args.name)
        outcome = engine.test()
        result = {
            "ok": bool(outcome.ok),
            "code": str(outcome.code),
            "message": str(outcome.message),
            "detail": dict(outcome.detail),
        }
        with contextlib.suppress(Exception):
            engine.stop_session()
    except (FileNotFoundError, ValueError, KeyError) as exc:
        result = {
            "ok": False,
            "code": "config",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }
    except Exception as exc:
        result = {
            "ok": False,
            "code": "error",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }

    # Emit exactly one JSON line on stdout.
    sys.stdout.write(_json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _test_llm_entry(args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools test-llm <name>``."""
    from ovid2.llm import cli as _llm_cli

    return _llm_cli.probe(args.name)


def _llm_chat_entry(_args: argparse.Namespace) -> int:
    """Entry point for ``ovid2 tools llm-chat`` (stdin request)."""
    from ovid2.llm import cli as _llm_cli

    return _llm_cli.chat()


def _probe_connection_entry(args: argparse.Namespace) -> int:
    """Resolve a connection's secrets, run its probe, emit JSON.

    Runs in the executor trust domain: this is the process that reads
    the plaintext secret. The web tier only shells out to it and parses
    the last JSON line of stdout, so the API process never holds a
    credential. JSON schema::

        {"ok": bool, "code": str, "message": str, "detail": dict}
    """
    import json as _json

    from ovid2.config import connections as _connections
    from ovid2.drivers import probe as _driver_probe
    from ovid2.drivers.registry import DRIVERS, instantiate

    result: dict[str, object]
    try:
        raw = _connections.load_connection_config(args.name)
        type_name = str(raw.get("type") or "")
        if type_name not in DRIVERS:
            result = {
                "ok": False,
                "code": "config",
                "message": f"Connection type '{type_name}' is not registered.",
                "detail": {},
            }
        else:
            secrets_payload = _connections.resolve_secrets(raw)
            driver = instantiate(
                type_name,
                args=dict(raw.get("args") or {}),
                secrets=secrets_payload,
            )
            probe_result = _driver_probe.run_test(driver)
            detail = dict(getattr(probe_result, "detail", {}) or {})
            # Surface a resolved auth-mode label (azure_blob) so the web
            # tier can display it without instantiating the connector
            # itself. Best-effort; never includes credential material.
            with contextlib.suppress(Exception):
                connection = _connections.load_connection(args.name)
                resolver = getattr(connection, "_auth_mode", None)
                if resolver is not None:
                    detail.setdefault("auth_mode", str(resolver()))
            result = {
                "ok": bool(probe_result.ok),
                "code": str(probe_result.code),
                "message": str(probe_result.message),
                "detail": detail,
            }
    except (FileNotFoundError, PermissionError, ValueError, KeyError) as exc:
        result = {
            "ok": False,
            "code": "config",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }
    except Exception as exc:
        result = {
            "ok": False,
            "code": "error",
            "message": str(exc),
            "detail": {"exception_type": type(exc).__name__},
        }

    sys.stdout.write(_json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _browse_storage_entry(args: argparse.Namespace) -> int:
    """Resolve a storage account, list one level, emit JSON.

    Executor-domain counterpart to the web tier's storage-browse
    endpoint. JSON schema::

        {"ok": bool, "supported": bool, "type": str, "prefix": str,
         "message": str|None,
         "entries": [{"name","kind","path"[,"size_bytes"]}, ...]}
    """
    import json as _json

    from ovid2.config import connections as _connections
    from ovid2.drivers.registry import DRIVERS, instantiate

    prefix = getattr(args, "prefix", None)
    result: dict[str, object]
    try:
        raw = _connections.load_connection_config(args.name)
        type_name = str(raw.get("type") or "")
        if type_name not in DRIVERS:
            result = {
                "ok": False,
                "supported": False,
                "type": type_name,
                "prefix": prefix or "",
                "message": f"Storage type '{type_name}' is not registered.",
                "entries": [],
            }
        else:
            secrets_payload = _connections.resolve_secrets(raw)
            driver = instantiate(
                type_name,
                args=dict(raw.get("args") or {}),
                secrets=secrets_payload,
            )
            try:
                raw_entries = driver.list_dir(prefix)
                result = {
                    "ok": True,
                    "supported": True,
                    "type": type_name,
                    "prefix": prefix or "",
                    "message": None,
                    "entries": [e.to_dict() for e in (raw_entries or [])],
                }
            except NotImplementedError as exc:
                result = {
                    "ok": True,
                    "supported": False,
                    "type": type_name,
                    "prefix": prefix or "",
                    "message": (
                        f"Browsing is not available for '{type_name}' storage accounts ({exc})."
                    ),
                    "entries": [],
                }
    except (FileNotFoundError, PermissionError, ValueError, KeyError) as exc:
        result = {
            "ok": False,
            "supported": True,
            "type": "",
            "prefix": prefix or "",
            "message": str(exc),
            "entries": [],
        }
    except Exception as exc:
        result = {
            "ok": False,
            "supported": True,
            "type": "",
            "prefix": prefix or "",
            "message": f"Listing failed: {exc}",
            "entries": [],
        }

    sys.stdout.write(_json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _probe_output_entry(args: argparse.Namespace) -> int:
    """List the tables already at an output address, emit JSON.

    Executor-domain counterpart of the web tier's overwrite pre-flight
    (:mod:`ovid2.web.services.output_probe_svc`). JSON schema::

        {"ok": bool, "known": bool, "type": str, "tables": [str, ...],
         "message": str|None}

    ``known=False`` means occupancy cannot be determined for this
    driver / host (the executor's own gate still runs at run time).
    """
    import json as _json

    from ovid2.drivers.overwrite import probe_output_tables
    from ovid2.drivers.registry import driver_from_address

    result: dict[str, object] = {
        "ok": False,
        "known": False,
        "type": "",
        "tables": [],
        "message": None,
    }
    try:
        driver = driver_from_address(args.address)
        result["type"] = driver.type_name
        tables = probe_output_tables(driver)
        result["ok"] = True
        if tables is None:
            result["message"] = "occupancy cannot be determined for this output."
        else:
            result["known"] = True
            result["tables"] = tables
    except (FileNotFoundError, PermissionError, ValueError, KeyError) as exc:
        result["message"] = str(exc)
    except Exception as exc:
        result["message"] = f"probe failed: {exc}"

    sys.stdout.write(_json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _list_source_tables_entry(args: argparse.Namespace) -> int:
    """Expand a copy/move source to concrete ``--tables`` specs, emit JSON.

    Executor-domain counterpart of the web tier's draft-time ``all``
    expansion (:mod:`ovid2.web.services._table_expand`). JSON schema::

        {"ok": bool, "code": "ok"|"unsupported"|"unavailable"|"error",
         "supported": bool, "type": str, "tables": [str, ...],
         "sizes": {str: int}, "message": str|None}

    ``supported=False`` means the driver type cannot enumerate tables;
    ``ok=False`` with ``supported=True`` means it can but listing failed
    (missing folder, bad schema) — ``message`` says why.
    """
    import json as _json

    from ovid2.drivers.registry import driver_from_address
    from ovid2.tools.table_order import sort_by_size
    from ovid2.tools.table_specs import TableSpecError, parse_table_specs

    # ``code``: ``ok`` | ``unsupported`` (driver type cannot enumerate) |
    # ``unavailable`` (it can, but not from this host — e.g. SDK / jars
    # missing; the copy executor re-expands ``all`` at run time) |
    # ``error`` (a concrete listing failure — missing folder, bad schema).
    result: dict[str, object] = {
        "ok": False,
        "code": "unsupported",
        "supported": False,
        "type": "",
        "tables": [],
        "sizes": {},
        "message": None,
    }
    try:
        driver = driver_from_address(args.address, pipeline=args.pipeline)
        result["type"] = driver.type_name
        if not type(driver).supports_expand_all():
            result["message"] = (
                f"'{driver.type_name}' sources cannot list their tables; "
                "pass an explicit tables list."
            )
        else:
            result["supported"] = True
            result["code"] = "error"
            expanded = driver.expand_all_tables(pipeline=args.pipeline, batch_id=args.batch_id)
            if expanded is None:
                result["code"] = "unavailable"
                result["message"] = "table enumeration is unavailable for this source."
            elif not expanded:
                result["message"] = "the source contains no tables."
            else:
                specs = parse_table_specs(expanded)
                try:
                    sizes = driver.table_sizes(
                        [s.file_name for s in specs],
                        pipeline=args.pipeline,
                        batch_id=args.batch_id,
                    )
                except Exception as exc:  # sizes are advisory
                    logger.warning("table_sizes failed for %s: %s", args.address, exc)
                    sizes = {}
                ordered = sort_by_size(specs, sizes) if sizes else specs
                result["tables"] = [
                    s.table_name if s.table_name == s.file_name else f"{s.table_name}={s.file_name}"
                    for s in ordered
                ]
                result["sizes"] = sizes
                result["ok"] = True
                result["code"] = "ok"
    except NotImplementedError as exc:
        result["code"] = "unavailable"
        result["message"] = f"listing is unavailable on this host: {exc}"
    except (FileNotFoundError, PermissionError, ValueError, KeyError, TableSpecError) as exc:
        result["code"] = "error"
        result["message"] = str(exc)
    except Exception as exc:
        result["code"] = "error"
        result["message"] = f"Listing failed: {exc}"

    sys.stdout.write(_json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


def _register_ods(
    tool_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the ``ovid2 tools ods`` subcommand tree."""
    ods = tool_subparsers.add_parser(
        "ods",
        help="Ovid Data Space helpers (list, resolve, dimensions).",
        description=(
            "Read-only utilities over the Ovid Data Space: every storage "
            "account is a tree of plain directories, /<account>/<path…>. "
            "Output is JSON on stdout so the commands compose with other tools."
        ),
    )
    ods_subparsers = ods.add_subparsers(dest="ods_command", metavar="COMMAND")

    ls = ods_subparsers.add_parser(
        "ls",
        help="List children of a Data Space path.",
        description=(
            "List the direct children of a Data Space path. '/' lists storage "
            "accounts; '/<account>/<folder…>' lists that folder or "
            "DATABASE/SCHEMA."
        ),
    )
    ls.add_argument("path", nargs="?", default="/", help="Data Space path (default '/').")
    ls.set_defaults(_entry=_ods_ls_entry, _tool="ods.ls")

    resolve = ods_subparsers.add_parser(
        "resolve",
        help="Resolve a Data Space path to its storage account + exact location.",
    )
    resolve.add_argument("path", help="Data Space path (e.g. /inbox/landing/2026_03).")
    resolve.set_defaults(_entry=_ods_resolve_entry, _tool="ods.resolve")

    dims = ods_subparsers.add_parser(
        "dimensions",
        help="List every storage type and the addresses using it.",
    )
    dims.set_defaults(_entry=_ods_dimensions_entry, _tool="ods.dimensions")

    def _ods_fallback(_args: argparse.Namespace) -> int:
        ods.print_help(sys.stderr)
        return 2

    ods.set_defaults(_entry=_ods_fallback)


def _ods_ls_entry(args: argparse.Namespace) -> int:
    """Print the children of a Data Space path as JSON."""
    import json as _json

    from ovid2 import dataspace
    from ovid2.dataspace.path import OdsPathError
    from ovid2.dataspace.resolver import OdsResolutionError

    try:
        supported, entries, message = dataspace.list_children(args.path)
    except (OdsPathError, OdsResolutionError, FileNotFoundError, ValueError) as exc:
        print(_json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    payload = {
        "path": args.path,
        "supported": supported,
        "message": message,
        "children": [
            {"name": e.name, "kind": e.kind, "path": e.path, "type": e.type} for e in entries
        ],
    }
    sys.stdout.write(_json.dumps(payload) + "\n")
    return 0


def _ods_resolve_entry(args: argparse.Namespace) -> int:
    """Print the resolution of a Data Space path as JSON."""
    import json as _json

    from ovid2 import dataspace
    from ovid2.dataspace.path import OdsPathError
    from ovid2.dataspace.resolver import OdsResolutionError

    try:
        target = dataspace.resolve(args.path)
    except (OdsPathError, OdsResolutionError) as exc:
        print(_json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    payload = {
        "path": target.path.as_str(),
        "connection": target.connection,
        "type": target.type,
        "location": target.location,
        "batch_id": target.batch_id,
        "address_name": target.address_name,
        "candidates": list(target.candidates),
    }
    sys.stdout.write(_json.dumps(payload) + "\n")
    return 0


def _ods_dimensions_entry(args: argparse.Namespace) -> int:
    """Print the dimension catalogue as JSON."""
    import json as _json

    from ovid2 import dataspace
    from ovid2.dataspace.registry import list_addresses_by_dimension

    payload = {
        "dimensions": [
            {
                "name": dim,
                "addresses": list_addresses_by_dimension(dim),
            }
            for dim in dataspace.list_dimensions()
        ],
    }
    sys.stdout.write(_json.dumps(payload) + "\n")
    return 0
