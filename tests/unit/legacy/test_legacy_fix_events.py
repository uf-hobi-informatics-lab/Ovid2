"""Legacy fix stage: one ``fix.*`` event per fix (run console "fixes" list).

A ``legacy_ovid`` port ran its fixes without telling the run console, so
the run page listed no fixes for a ported pipeline (``acme``) while the
native path (``acme_from_disc``) showed every one. These tests pin that the
legacy fix stage reports each fix of a table's ``fixes`` dict — through
fix scripts (``runpy``) and through the vendored in-process chain
(``print_fixer_status`` / ``print_failure_message``) — and that the runner
turns the reports into ``fix.applied`` / ``fix.skipped`` / ``fix.failed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ovid2.core.events import Event
from ovid2.legacy import fix_shim, fix_stage, runner
from ovid2.legacy.fix_stage import FixProgress, LegacyFixEntry

pytestmark = pytest.mark.unit

_A = LegacyFixEntry("common", "patid_orphans_fix", "1.0", "mapped_demographic.csv", "NotUsed")
_B = LegacyFixEntry("custom", "delete_when_null_fix", "1.0", "DX", "notUsed")
_C = LegacyFixEntry("common", "replication_fix", "1.0", "mapped_encounter.csv", "NotUsed")


def _collect() -> tuple[list[FixProgress], Any]:
    seen: list[FixProgress] = []
    return seen, seen.append


def _summary(seen: list[FixProgress]) -> list[tuple[str, str, int, int, str]]:
    out = []
    for p in seen:
        state = "skipped" if p.skipped else ("failed" if p.error is not None else "applied")
        out.append((p.table, p.entry.fix_name, p.index, p.total, state))
    return out


# -- fix scripts (runpy) ----------------------------------------------------


def _script_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bodies: dict[str, str]) -> None:
    """Point ``_script_path`` at tiny scripts whose body is ``bodies[fix_name]``."""
    for name, body in bodies.items():
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    monkeypatch.setattr(fix_stage, "_script_path", lambda e, _p: tmp_path / f"{e.fix_name}.py")
    monkeypatch.setattr(fix_shim, "quarantine", lambda *_a: None)


def _run_scripts(entries: list[LegacyFixEntry], table: str, cb: Any) -> None:
    with fix_stage._reporting(cb, table, entries):
        for entry in entries:
            fix_stage.run_fix_script(
                entry,
                partner="demo",
                folder_label="in/b1",
                mapped_table_name=f"mapped_{table}.csv",
                fixed_table_name=f"fixed_{table}.csv",
                input_path="/nowhere/",
                output_path="/nowhere/",
            )


def test_fix_scripts_report_applied_and_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skip = (
        "from ovid2.legacy.fix_shim import CommonFuncitons as C\n"
        "C.print_fixer_status(current_count='**', total_count='**', fix_type='c', fix_name='x')\n"
        "C.print_failure_message('f', 'demo', 'job', 'Path does not exist: ref.csv')\n"
    )
    _script_env(
        monkeypatch,
        tmp_path,
        {_A.fix_name: "pass\n", _B.fix_name: skip, _C.fix_name: "pass\n"},
    )
    seen, cb = _collect()
    _run_scripts([_A, _B, _C], "diagnosis", cb)
    assert _summary(seen) == [
        ("diagnosis", "patid_orphans_fix", 1, 3, "applied"),
        ("diagnosis", "delete_when_null_fix", 2, 3, "skipped"),
        ("diagnosis", "replication_fix", 3, 3, "applied"),
    ]
    assert seen[1].error == "Path does not exist: ref.csv"


def test_fatal_fix_script_reports_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fatal = (
        "from ovid2.legacy.fix_shim import CommonFuncitons as C\n"
        "C.print_failure_message('f', 'demo', 'job', 'column DX not found')\n"
    )
    _script_env(monkeypatch, tmp_path, {_A.fix_name: "pass\n", _B.fix_name: fatal})
    seen, cb = _collect()
    with pytest.raises(fix_stage.LegacyFixError):
        _run_scripts([_A, _B], "diagnosis", cb)
    assert _summary(seen) == [
        ("diagnosis", "patid_orphans_fix", 1, 2, "applied"),
        ("diagnosis", "delete_when_null_fix", 2, 2, "failed"),
    ]
    assert seen[1].error == "column DX not found"


def test_raising_fix_script_reports_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _script_env(monkeypatch, tmp_path, {_A.fix_name: "raise ValueError('bad input')\n"})
    seen, cb = _collect()
    with pytest.raises(ValueError):
        _run_scripts([_A], "encounter", cb)
    assert _summary(seen) == [("encounter", "patid_orphans_fix", 1, 1, "failed")]
    assert seen[0].error == "bad input"


def test_no_callback_reports_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _script_env(monkeypatch, tmp_path, {_A.fix_name: "pass\n"})
    _run_scripts([_A], "encounter", None)  # must not raise


def test_duplicate_entries_get_their_own_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _script_env(monkeypatch, tmp_path, {_A.fix_name: "pass\n", _B.fix_name: "pass\n"})
    seen, cb = _collect()
    _run_scripts([_A, _B, _A], "vital", cb)
    assert [p.index for p in seen] == [1, 2, 3]


# -- in-process chain (vendored fix_chain) ------------------------------------


def test_chain_status_lines_report_each_fix() -> None:
    """``fix_chain.run_fix_chain`` announces fix *n* before running it."""
    seen, cb = _collect()
    cf = fix_shim.CommonFuncitons
    with fix_stage._reporting(cb, "procedures", [_A, _B, _C]):
        cf.print_fixer_status(current_count=1, total_count=3, fix_type="common", fix_name="a")
        cf.print_fixer_status(current_count=2, total_count=3, fix_type="common", fix_name="b")
        cf.print_failure_message("f", "demo", "common_b_1.0", "lists/x.csv does not exist in")
        cf.print_fixer_status(current_count=3, total_count=3, fix_type="common", fix_name="c")
        fix_stage.flush_chain_fix()
    assert _summary(seen) == [
        ("procedures", "patid_orphans_fix", 1, 3, "applied"),
        ("procedures", "delete_when_null_fix", 2, 3, "skipped"),
        ("procedures", "replication_fix", 3, 3, "applied"),
    ]


def test_chain_failure_marks_the_pending_fix_failed() -> None:
    seen, cb = _collect()
    with fix_stage._reporting(cb, "procedures", [_A, _B]):
        fix_shim.CommonFuncitons.print_fixer_status(current_count=1, total_count=2)
        fix_shim.CommonFuncitons.print_fixer_status(current_count=2, total_count=2)
        fix_stage.flush_chain_fix(error="boom")
    assert _summary(seen) == [
        ("procedures", "patid_orphans_fix", 1, 2, "applied"),
        ("procedures", "delete_when_null_fix", 2, 2, "failed"),
    ]


def test_chain_then_script_fallback_reports_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chain-unsupported fix runs as a script between chain fixes."""
    _script_env(monkeypatch, tmp_path, {_B.fix_name: "pass\n"})
    seen, cb = _collect()
    with fix_stage._reporting(cb, "lab_result_cm", [_A, _B, _C]):
        fix_shim.CommonFuncitons.print_fixer_status(current_count=1, total_count=3)
        fix_stage.run_fix_script(
            _B,
            partner="demo",
            folder_label="in/b1",
            mapped_table_name="mapped_lab_result_cm.csv",
            fixed_table_name="fixed_lab_result_cm.csv",
            input_path="/nowhere/",
            output_path="/nowhere/",
        )
        fix_shim.CommonFuncitons.print_fixer_status(current_count=3, total_count=3)
        fix_stage.flush_chain_fix()
    assert [(p.entry.fix_name, p.index) for p in seen] == [
        ("patid_orphans_fix", 1),
        ("delete_when_null_fix", 2),
        ("replication_fix", 3),
    ]


# -- runner: progress -> events ----------------------------------------------


@pytest.mark.parametrize(
    ("progress", "kind"),
    [
        (FixProgress("diagnosis", _A, 1, 5), "fix.applied"),
        (FixProgress("diagnosis", _A, 1, 5, error="missing", skipped=True), "fix.skipped"),
        (FixProgress("diagnosis", _A, 1, 5, error="boom"), "fix.failed"),
    ],
)
def test_runner_builds_fix_events(progress: FixProgress, kind: str) -> None:
    event: Event = runner._fix_event(progress, instance_id="iid", pipeline="acme", batch_id="b")
    assert event.kind == kind
    p = event.payload
    assert (p["table"], p["fix"], p["fix_type"], p["current_fix"], p["total_fixes"]) == (
        "diagnosis",
        "patid_orphans_fix",
        "common",
        1,
        5,
    )
    assert (p["src1"], p["src2"]) == ("mapped_demographic.csv", "NotUsed")
    if kind == "fix.failed":
        assert p["error"] == "boom"
    if kind == "fix.skipped":
        assert p["message"] == "missing"


def test_runner_wires_on_fix_into_the_fix_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end through ``run_legacy_pipeline``: fix reports become bus events."""
    import ovid2.config.settings as settings

    monkeypatch.setattr(settings, "WORK_DIR", tmp_path)
    monkeypatch.setenv("OVID2_INSTANCE_ID", "20261125-000000-abcdef")
    monkeypatch.setattr("ovid2.cdm.registry.resolve_cdm_for_pipeline", lambda _p: None)
    monkeypatch.setattr(
        "ovid2.legacy.fix_shim.write_tab_file",
        lambda _df, folder, name: Path(folder, name).write_text("x\n", encoding="utf-8"),
    )
    monkeypatch.setattr(fix_stage, "fixer_tables", lambda _p: ["a"])

    def _fixes(_spark: Any, *, table: str, work_root: str, on_fix: Any = None, **_k: Any) -> None:
        on_fix(FixProgress(table, _A, 1, 2))
        on_fix(FixProgress(table, _B, 2, 2, error="ref missing", skipped=True))
        Path(work_root, "fixer_output", f"fixed_{table}.csv").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(fix_stage, "run_table_fixes", _fixes)
    monkeypatch.setattr(fix_stage, "run_multi_table_fixes", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_publish_fixed", lambda *_a, **_k: None)

    class _Bus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        def emit(self, event: Event) -> None:
            self.events.append(event)

    class _Formatter:
        def format(self, _df: Any) -> str:
            return "df"

    bus = _Bus()
    runner.run_legacy_pipeline(
        engine=object(),  # type: ignore[arg-type]
        spark=None,  # type: ignore[arg-type]
        pipeline="demo",
        batch_id="in/b1",
        tables=["a"],
        transformations=[],
        transformation_names=["format", "map", "fix"],
        input_address=object(),  # type: ignore[arg-type]
        output_address=object(),  # type: ignore[arg-type]
        input_address_name="in",
        bus=bus,
        instance_id="20261125-000000-abcdef",
        load_formatter=lambda *_a, **_k: _Formatter(),
    )
    fixes = [(e.kind, e.payload["fix"]) for e in bus.events if e.kind.startswith("fix.")]
    assert fixes == [("fix.applied", "patid_orphans_fix"), ("fix.skipped", "delete_when_null_fix")]


def test_stage_labels() -> None:
    from ovid2.transformations.deduplicate import DeduplicateTransformer
    from ovid2.transformations.map import MapTransformer
    from ovid2.transformations.transform import TransformStageTransformer

    assert runner._stage_label(DeduplicateTransformer()) == "deduplicate"
    assert runner._stage_label(TransformStageTransformer()) == "transform"
    assert runner._stage_label(MapTransformer()) == "map"

    class MappingReportTransformer:
        pass

    assert runner._stage_label(MappingReportTransformer()) == "mapping_report"
