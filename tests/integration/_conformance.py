"""Per-kind drivers for the algorithm conformance matrix.

Each fixture under ``tests/fixtures/algorithms/<kind>/<name>/case.yaml`` is
data only::

    dataset: name            # optional shared tables (fixtures/algorithms/_datasets/<name>.yaml)
    params: {...}            # step params (mapper / transform / fixer src1… )
    tables:                  # every frame the algorithm sees, all-string cells
      input: {columns: [...], rows: [[...], ...]}
      demographic: {...}     # extra tables are registered as temp views
    files:                   # optional files written to a tmp dir (formatters)
      Demographic_x.txt: "PATID|SEX\\n1|F\\n"
    spec: {...}              # formatter spec / health-check override
    prepare: [...]           # optional Spark-SQL select list applied to ``input``
    lists: {name: csv text}  # material/lists/<name>.csv for list-based fixers
    compare: rows|count|columns   # default rows
    float_tol: 1e-9
    note: why the fixture looks like this (traps covered)
    expected: {columns: [...], rows: [[...], ...]}

:func:`run_case` builds the frames on the engine through the portable
surface (``engine.create_typed_dataframe``), runs the algorithm through
``ovid2.algorithms.registry`` and returns normalised rows. The test module
asserts that every engine covering the algorithm's ``requires`` returns the
fixture's ``expected`` rows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "algorithms"


@dataclass
class Case:
    """One parsed fixture."""

    kind: str
    name: str
    path: Path
    params: dict[str, Any] = field(default_factory=dict)
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    compare: str = "rows"
    float_tol: float = 1e-9
    note: str = ""
    expected: dict[str, Any] = field(default_factory=dict)
    engines: list[str] | None = None
    prepare: list[str] = field(default_factory=list)
    lists: dict[str, str] = field(default_factory=dict)
    run_date: str = "2026-01-01"
    pipeline: str = "conformance"


def load_case(kind: str, name: str) -> Case | None:
    """Return the fixture for ``(kind, name)``, or ``None`` when absent."""
    path = FIXTURE_ROOT / kind / name / "case.yaml"
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tables = dict(raw.get("tables") or {})
    run_date = raw.get("run_date")
    if raw.get("dataset"):
        dataset = yaml.safe_load(
            (FIXTURE_ROOT / "_datasets" / f"{raw['dataset']}.yaml").read_text(encoding="utf-8")
        )
        tables = {**dataset.get("tables", {}), **tables}
        for prior_name, prior_table in (dataset.get("prior") or {}).items():
            tables.setdefault(f"prior_{prior_name}", prior_table)
        run_date = run_date or dataset.get("run_date")
    return Case(
        kind=kind,
        name=name,
        path=path,
        params=dict(raw.get("params") or {}),
        tables=tables,
        files=dict(raw.get("files") or {}),
        spec=dict(raw.get("spec") or {}),
        compare=str(raw.get("compare") or "rows"),
        float_tol=float(raw.get("float_tol") or 1e-9),
        note=str(raw.get("note") or ""),
        expected=dict(raw.get("expected") or {}),
        engines=list(raw["engines"]) if raw.get("engines") else None,
        prepare=[str(e) for e in raw.get("prepare") or []],
        lists={str(k): str(v) for k, v in (raw.get("lists") or {}).items()},
        run_date=str(run_date) if run_date else "2026-01-01",
        pipeline=str(raw.get("pipeline") or "conformance"),
    )


# --------------------------------------------------------------------------- #
# Normalisation                                                                #
# --------------------------------------------------------------------------- #


def _cell(value: Any) -> Any:
    """Normalise one cell: ``None`` stays ``None``; numbers → float; rest → str.

    Null and empty string stay distinct (a classic engine trap).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return "NaN" if isinstance(value, float) and math.isnan(value) else float(value)
    if hasattr(value, "isoformat"):  # date / datetime
        return value.isoformat(sep=" ") if hasattr(value, "hour") else value.isoformat()
    if type(value).__name__ == "Decimal":
        return float(value)
    return str(value)


def normalise(rows: list[Any]) -> list[tuple[Any, ...]]:
    """Return rows as tuples of comparable cells, sorted (row order is not portable)."""
    out = [tuple(_cell(v) for v in tuple(row)) for row in rows]
    return sorted(out, key=lambda r: tuple("\x00" if c is None else str(c) for c in r))


def rows_equal(got: list[tuple[Any, ...]], want: list[tuple[Any, ...]], tol: float) -> bool:
    """Compare two sorted row lists with a float tolerance."""
    if len(got) != len(want):
        return False
    for a, b in zip(got, want, strict=True):
        if len(a) != len(b):
            return False
        for x, y in zip(a, b, strict=True):
            if x == y:
                continue
            if x is None or y is None:
                return False
            try:
                if abs(float(x) - float(y)) <= tol * max(1.0, abs(float(y))):
                    continue
            except (TypeError, ValueError):
                pass
            return False
    return True


# --------------------------------------------------------------------------- #
# Frame building                                                               #
# --------------------------------------------------------------------------- #


def build_frame(engine: Any, table: dict[str, Any]) -> Any:
    """Build an all-string frame on ``engine`` from ``{columns, rows}``."""
    columns = [str(c) for c in table["columns"]]
    rows = [tuple(None if v is None else str(v) for v in r) for r in table.get("rows") or []]
    return engine.create_typed_dataframe(rows, [(c, "string") for c in columns])


def register_tables(engine: Any, case: Case) -> Any:
    """Build every fixture table; register all but ``input`` as temp views."""
    session = engine.get_session()
    frames = {name: build_frame(engine, t) for name, t in case.tables.items()}
    if case.prepare and "input" in frames:
        # Typed inputs (arrays, …) are derived with portable Spark SQL.
        frames["input"] = frames["input"].selectExpr(*case.prepare)
    for view_name, view_frame in frames.items():
        if view_name != "input":
            engine.register_temp_view(session, view_name, view_frame)
    return frames


# --------------------------------------------------------------------------- #
# Per-kind runners                                                             #
# --------------------------------------------------------------------------- #


def _cdm() -> Any:
    from ovid2.cdm.registry import get_cdm

    return get_cdm("pcornet_7_0")


def run_case(engine: Any, case: Case, tmp_path: Path) -> tuple[list[str], list[Any]]:
    """Run ``case`` on ``engine``; return ``(columns, collected rows)``."""
    from ovid2.algorithms.adapters import Invocation
    from ovid2.algorithms.registry import registry
    from ovid2.core.engines.dsl import use_engine

    algorithm = registry.get(case.kind, case.name)
    with use_engine(engine):
        frames = register_tables(engine, case)
        session = engine.get_session()
        frame = frames.get("input")
        if case.kind == "transform":
            from ovid2.transforms.base import TransformContext
            from ovid2.transforms.runtime import engine_context_for

            functions, family = engine_context_for(frame, engine)
            ctx = TransformContext(session=session, functions=functions, engine_family=family)
            out = algorithm.apply(frame, Invocation(ctx, case.params))
        elif case.kind == "mapper":
            import types

            from ovid2.mappers.base import MapOpContext

            dictionaries = types.SimpleNamespace(**(case.spec.get("dictionaries") or {}))
            ctx = MapOpContext(
                spark=session,
                pipeline="conformance",
                table="input",
                cdm=_cdm(),
                pipeline_dictionaries=dictionaries,
                secrets=dict(case.spec.get("secrets") or {}),
            )
            out = algorithm.apply(frame, Invocation(ctx, case.params))
        elif case.kind == "fixer":
            restore = _install_lists(case, tmp_path) if case.lists else None
            context: dict[str, Any] = {
                "spark": session,
                "engine": engine,
                "pipeline": "conformance",
                "batch_id": "b",
                "table_name": str(case.spec.get("table_name") or "input"),
                "cdm": _cdm(),
                **{k: frames[v] for k, v in (case.spec.get("context_frames") or {}).items()},
                **case.params,
            }
            try:
                out = algorithm.apply(frame, context)
                columns, rows = _collect(out, case)
            finally:
                if restore is not None:
                    restore()
            return columns, rows
        elif case.kind == "deduplicator":
            context = {
                "table_name": "input",
                "engine": engine,
                "cdm": _cdm(),
            }
            out = algorithm.apply(frame, Invocation(context, case.params))
        elif case.kind == "formatter":
            out = _run_formatter(engine, case, tmp_path)
        elif case.kind == "health_check":
            return _run_health_check(engine, case, frames)
        else:  # pragma: no cover
            raise AssertionError(case.kind)
        return _collect(out, case)


def _collect(out: Any, case: Case) -> tuple[list[str], list[Any]]:
    """Return ``(columns, rows)`` per the fixture's ``compare`` mode."""
    columns = list(out.columns)
    if case.compare == "count":
        return ["n"], [(float(out.count()),)]  # noqa: OVID2001 conformance oracle
    if case.compare == "columns":
        return ["column"], [(c,) for c in columns]
    return columns, list(out.collect())  # noqa: OVID2001 conformance oracle


def _install_lists(case: Case, tmp_path: Path) -> Any:
    """Write the fixture's reference lists, point the fixer at them; return a restorer."""
    import importlib

    lists_dir = tmp_path / "lists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    for name, text in case.lists.items():
        (lists_dir / f"{name}.csv").write_text(text, encoding="utf-8")
    module = importlib.import_module(f"ovid2.transformations.fixers.common.{case.name}")
    previous = module.COMMON_LISTS_DIR
    module.COMMON_LISTS_DIR = lists_dir

    def _restore() -> None:
        module.COMMON_LISTS_DIR = previous

    return _restore


def _run_formatter(engine: Any, case: Case, tmp_path: Path) -> Any:
    from ovid2.drivers.disc import DiscAddress
    from ovid2.formatters.adapters.base import AdapterContext
    from ovid2.formatters.engine import execute_spec
    from ovid2.formatters.spec import load_from_dict

    root = tmp_path / "in"
    root.mkdir(parents=True, exist_ok=True)
    for file_name, text in case.files.items():
        (root / file_name).write_text(text, encoding="utf-8")
    address = DiscAddress({"path": str(root)})
    spec = load_from_dict({"v": 1, **case.spec}, pipeline="conformance")
    session = engine.get_session()
    ctx = AdapterContext(
        spark=session,
        input_address=address if engine.session_is_spark else None,  # type: ignore[arg-type]
        pipeline="conformance",
        batch_id="b",
        engine=engine,
        input_address_name=None,
    )
    if not engine.session_is_spark:
        from ovid2.core.sources import SourceHandle

        class _LocalHandle(SourceHandle):
            """Stage the fixture folder through the engine's stager (disc driver)."""

            def read(self, glob: str, *, table_name: str | None = None) -> Any:
                from ovid2.drivers.disc.driver import DiscDriver

                driver = DiscDriver(
                    args={"host_path": str(root), "container_path": str(root)},
                    secrets={},
                    location={"format": "csv"},
                )
                return engine.get_stager().materialise(
                    driver, "conformance", "b", glob, table_name=table_name
                )

        ctx.source = _LocalHandle(
            session=session, input_address=None, pipeline="conformance", batch_id="b"
        )
    return execute_spec(spec, ctx)


def _run_health_check(
    engine: Any, case: Case, frames: dict[str, Any]
) -> tuple[list[str], list[Any]]:
    from ovid2.algorithms.registry import registry
    from ovid2.core.sources import local_file_reader
    from ovid2.transformations.quality.ops import OpContext

    algorithm = registry.get("health_check", case.name)
    tables = {
        k.upper(): v for k, v in frames.items() if k != "input" and not k.startswith("prior_")
    }
    prior = {k.upper()[6:]: v for k, v in frames.items() if k.startswith("prior_")}
    ctx = OpContext(
        session=engine.get_session(),
        cdm=_cdm(),
        available_tables=frozenset(tables),
        count_fn=lambda df: int(df.count()),  # noqa: OVID2001 conformance oracle
        pipeline=case.pipeline,
        run_date=__import__("datetime").date.fromisoformat(case.run_date),
        prior_tables=prior,
        functions=engine.get_functions(),
        frames=tables,
        reference_reader=local_file_reader(engine),
    )
    metric_rows = algorithm.apply(None, ctx)
    columns = ["source_table", "metric", "value_num", "value_str", "exception_flag"]
    out = [
        (
            m.source_table,
            m.metric,
            None if m.value_num is None else round(float(m.value_num), 6),
            m.value_str,
            bool(m.exception_flag),
        )
        for m in metric_rows
    ]
    return columns, out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("conformance harness; imported by the test module.")
