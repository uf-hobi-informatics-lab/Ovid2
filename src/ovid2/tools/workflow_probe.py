"""``ovid2 tools workflow-probe`` — aggregate counts for workflow branch conditions.

A workflow step may declare *data probes* (adr-workflows.md, "Branching"):
after the step succeeds, the workflow runner queues one ``workflow_probe``
job that runs this tool against the step's output address. Each probe
reads one table lazily on the in-process DuckDB engine, applies an
optional row filter (checked by :mod:`ovid2.config.sql_expr_guard`) and
computes one aggregate (:data:`~ovid2.config.workflow_conditions.PROBE_AGGS`).
Only the numbers leave the executor — the last stdout line is::

    {"ok": bool, "address": str, "results": {name: int|null},
     "errors": {name: str}, "message": str|null}

The aggregate is built from the DSL (never SQL text); the user filter is
the only expression and it passes the allow-list guard first. Exit code
is 0 when every probe produced a number, 1 otherwise (a failed probe
fails the step, so a broken condition never silently routes a branch).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

__all__ = ["register", "run_probes"]


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``workflow-probe`` on the ``ovid2 tools`` parser."""
    p = subparsers.add_parser(
        "workflow-probe",
        help="Compute aggregate counts at an address for workflow branch conditions.",
        description=(
            "Runs the data probes of a workflow step against its output address "
            "(in-process DuckDB) and prints one JSON object with the numbers on "
            "the last line. Numbers only; no row values are emitted."
        ),
    )
    p.add_argument("--address", dest="address", required=True, help="Address name.")
    p.add_argument(
        "--spec",
        required=True,
        help='JSON list of probes: [{"name", "table", "agg", "column"?, "where"?}].',
    )
    p.add_argument("--engine", default="duckdb", help="Engine (default: duckdb, in-process).")
    p.set_defaults(_entry=_entry, _tool="workflow-probe")


def _source_name(driver: Any, table: str) -> str:
    """Map a logical table name to the driver's source name (case-insensitive)."""
    try:
        specs = driver.expand_all_tables() or []
    except (FileNotFoundError, NotImplementedError, ValueError, OSError):
        specs = []
    low = table.lower()
    for spec in specs:
        logical, _, source = str(spec).partition("=")
        if logical.lower() == low:
            return source or logical
    return table


def _aggregate(df: Any, probe: dict[str, Any]) -> int:
    """Return one aggregate of ``df`` (DSL only; no SQL text)."""
    from ovid2.core.engines import dsl as F

    agg = probe.get("agg") or "count"
    column = probe.get("column") or ""
    if column:
        # Column names are matched case-insensitively like the CDM loaders do.
        by_lower = {c.lower(): c for c in df.columns}
        if column.lower() not in by_lower:
            raise KeyError(f"column '{column}' not found in table '{probe['table']}'")
        column = by_lower[column.lower()]
    if agg == "count":
        expr = F.count(F.col(column)) if column else F.count(F.lit(1))
    elif agg == "count_distinct":
        expr = F.countDistinct(F.col(column))
    elif agg == "non_null_count":
        expr = F.count(F.col(column))
    else:  # null_count
        expr = F.sum(F.when(F.col(column).isNull(), F.lit(1)).otherwise(F.lit(0)))
    row = df.agg(expr.alias("n")).first()
    value = row[0] if row is not None else 0
    return int(value or 0)


def run_probes(
    address: str, probes: list[dict[str, Any]], *, engine_name: str = "duckdb"
) -> dict[str, Any]:
    """Run ``probes`` against ``address``; return the JSON result dict."""
    from ovid2.config.sql_expr_guard import ensure_safe_expression, for_engine_family
    from ovid2.config.workflow_conditions import MAX_PROBES, validate_probe

    result: dict[str, Any] = {
        "ok": False,
        "address": address,
        "results": {},
        "errors": {},
        "message": None,
    }
    if not isinstance(probes, list) or not probes:
        result["message"] = "no probes given"
        return result
    if len(probes) > MAX_PROBES:
        result["message"] = f"at most {MAX_PROBES} probes"
        return result
    for i, probe in enumerate(probes):
        errs = validate_probe(probe, where=f"probe[{i}]")
        if errs:
            result["message"] = "; ".join(errs)
            return result

    from ovid2.core.engines import dsl as F
    from ovid2.core.loaders import load_engine
    from ovid2.drivers.registry import driver_from_address

    engine = load_engine(engine_name)
    try:
        driver = driver_from_address(address)
        stager = engine.get_stager()
        fam = getattr(engine, "family", "")
        family = str(getattr(fam, "value", fam) or "")
        for probe in probes:
            name = str(probe["name"])
            table = str(probe["table"])
            try:
                df = stager.materialise(
                    driver, "", "", _source_name(driver, table), table_name=table
                )
                flt = probe.get("where")
                if flt:
                    predicate = ensure_safe_expression(str(flt), where=f"probe '{name}' filter")
                    df = df.where(F.expr(for_engine_family(predicate, family)))
                result["results"][name] = _aggregate(df, probe)
            except Exception as exc:
                result["results"][name] = None
                result["errors"][name] = (
                    str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
                )
    finally:
        engine.stop_session()
    result["ok"] = not result["errors"]
    if result["errors"]:
        result["message"] = "; ".join(f"{k}: {v}" for k, v in result["errors"].items())
    return result


def _entry(args: argparse.Namespace) -> int:
    """CLI entry: print progress lines, then the JSON result on the last line."""
    try:
        probes = json.loads(args.spec)
    except json.JSONDecodeError as exc:
        result: dict[str, Any] = {
            "ok": False,
            "address": args.address,
            "results": {},
            "errors": {},
            "message": f"--spec is not JSON: {exc}",
        }
    else:
        try:
            result = run_probes(args.address, probes, engine_name=args.engine)
        except (FileNotFoundError, PermissionError, KeyError, ValueError) as exc:
            result = {
                "ok": False,
                "address": args.address,
                "results": {},
                "errors": {},
                "message": str(exc),
            }
    for name, value in (result.get("results") or {}).items():
        print(f"[ovid2] workflow-probe {name} = {value}")
    if not result.get("ok"):
        print(f"error: workflow-probe: {result.get('message')}", file=sys.stderr)
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via `ovid2 tools workflow-probe`.")
