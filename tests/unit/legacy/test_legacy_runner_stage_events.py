"""Legacy runner: per-table phase events and scratch cleanup.

The ``legacy_ovid`` runner maps *every* table (phase 1), then fixes them
(phase 2), then publishes them (phase 3). Before the fix, a table emitted
``table.started`` in phase 1 and nothing until phase 3, so its run-page
clock ticked through every other table's work (several "running" tables
at once). These tests pin the per-table ``stage.progress`` pause / resume
events, and that the run's tmpfs scratch is removed however the run ends.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from ovid2.core.events import Event
from ovid2.legacy import csv_roundtrip, fix_stage, runner

pytestmark = pytest.mark.unit

_IID = "20261124-120000-abc123"


class _Bus:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)


class _Formatter:
    def format(self, _df: Any) -> str:
        return "df"


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fail_fix: str = "") -> None:
    import ovid2.config.settings as settings

    monkeypatch.setattr(settings, "WORK_DIR", tmp_path)
    monkeypatch.setattr(settings, "LEGACY_SCRATCH_ROOT", tmp_path)
    monkeypatch.setenv("OVID2_INSTANCE_ID", _IID)
    monkeypatch.delenv("OVID2_LEGACY_KEEP_SCRATCH", raising=False)
    monkeypatch.setattr("ovid2.cdm.registry.resolve_cdm_for_pipeline", lambda _p: None)

    def _write_tab(_df: Any, folder: str, name: str) -> None:
        Path(folder, name).write_text("x\n", encoding="utf-8")
        # A round-trip scratch dir, as the real mapper stage creates.
        Path(csv_roundtrip._mkscratch("legacy_rt_"), "t.csv").write_text("x", encoding="utf-8")

    monkeypatch.setattr("ovid2.legacy.fix_shim.write_tab_file", _write_tab)
    monkeypatch.setattr(fix_stage, "fixer_tables", lambda _p: ["a", "b"])

    def _fixes(_spark: Any, *, table: str, work_root: str, **_kw: Any) -> None:
        if table == fail_fix:
            raise RuntimeError("fix boom")
        Path(work_root, "fixer_output", f"fixed_{table}.csv").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(fix_stage, "run_table_fixes", _fixes)
    monkeypatch.setattr(fix_stage, "run_multi_table_fixes", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_publish_fixed", lambda *_a, **_k: None)


def _run(bus: _Bus) -> runner.LegacyRunResult:
    return runner.run_legacy_pipeline(
        engine=object(),  # type: ignore[arg-type]
        spark=None,  # type: ignore[arg-type]
        pipeline="demo",
        batch_id="in/b1",
        tables=["a", "b"],
        transformations=[],
        transformation_names=["format", "map", "fix"],
        input_address=object(),  # type: ignore[arg-type]
        output_address=object(),  # type: ignore[arg-type]
        input_address_name="in",
        bus=bus,
        instance_id=_IID,
        load_formatter=lambda *_a, **_k: _Formatter(),
    )


def _trace(bus: _Bus) -> list[tuple[str, str, str]]:
    out = []
    for e in bus.events:
        p = e.payload
        state = (p.get("extra") or {}).get("state", "") if e.kind == "stage.progress" else ""
        detail = f"{p.get('stage')}:{state}" if e.kind == "stage.progress" else ""
        out.append((e.kind, str(p.get("table")), detail))
    return out


def test_tables_pause_between_phases(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    bus = _Bus()
    result = _run(bus)
    assert result.succeeded == ["a", "b"]
    assert _trace(bus) == [
        ("table.started", "a", ""),
        ("stage.progress", "a", "format:active"),
        ("stage.progress", "a", "execute:active"),
        ("stage.progress", "a", "map:waiting"),
        ("table.started", "b", ""),
        ("stage.progress", "b", "format:active"),
        ("stage.progress", "b", "execute:active"),
        ("stage.progress", "b", "map:waiting"),
        ("stage.progress", "a", "fix:active"),
        ("stage.progress", "a", "fix:waiting"),
        ("stage.progress", "b", "fix:active"),
        ("stage.progress", "b", "fix:waiting"),
        ("stage.progress", "a", "write:active"),
        ("table.completed", "a", ""),
        ("stage.progress", "b", "write:active"),
        ("table.completed", "b", ""),
    ]


def test_only_one_table_is_ever_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Folded through the run-page fold, at most one table runs at a time."""
    from ovid2.web.services.run_items_svc import fold_events

    _setup(monkeypatch, tmp_path)
    bus = _Bus()
    _run(bus)
    seen: list[dict[str, Any]] = []
    for e in bus.events:
        seen.append(e.to_json_dict())
        running = [r.item for r in fold_events(seen) if r.status == "running"]
        assert len(running) <= 1, running


def test_failed_fix_still_pauses_and_fails_the_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path, fail_fix="a")
    bus = _Bus()
    result = _run(bus)
    assert "a" in result.failed and result.succeeded == ["b"]
    trace = _trace(bus)
    assert ("stage.progress", "a", "fix:waiting") in trace
    assert ("table.failed", "a", "") in trace


def test_scratch_is_removed_on_success_and_on_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)
    _run(_Bus())
    assert not (tmp_path / f"legacy_rt-{_IID}").exists()
    assert not list((tmp_path / "legacy_scratch").rglob(_IID))

    def _boom(*_a: Any, **_k: Any) -> None:
        raise KeyboardInterrupt  # a cancel escapes the runner

    monkeypatch.setattr(runner, "_publish_fixed", _boom)
    with pytest.raises(KeyboardInterrupt):
        _run(_Bus())
    assert not (tmp_path / f"legacy_rt-{_IID}").exists()
    assert not list((tmp_path / "legacy_scratch").rglob(_IID))


def test_keep_scratch_env_keeps_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("OVID2_LEGACY_KEEP_SCRATCH", "1")
    _run(_Bus())
    assert (tmp_path / f"legacy_rt-{_IID}").is_dir()


def test_scratch_dir_falls_back_without_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ovid2.config.settings as settings

    monkeypatch.setattr(settings, "LEGACY_SCRATCH_ROOT", tmp_path)
    monkeypatch.delenv("OVID2_INSTANCE_ID", raising=False)
    assert csv_roundtrip.run_scratch_dir() == str(tmp_path)
    monkeypatch.setenv("OVID2_INSTANCE_ID", _IID)
    assert csv_roundtrip.run_scratch_dir() == os.path.join(str(tmp_path), f"legacy_rt-{_IID}")


def test_table_scratch_is_freed_after_each_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A table's round-trip dirs are removed once its mapped file is written.

    Keeping every table's ``legacy_rt_*`` dirs until the run ended filled
    the ``WORK_DIR`` tmpfs on a full acme feed (ENOSPC on later tables).
    """
    _setup(monkeypatch, tmp_path)
    rt_root = tmp_path / f"legacy_rt-{_IID}"
    seen_during: list[int] = []

    real_stage = runner._drop_new_scratch

    def _spy(before: set[str]) -> None:
        seen_during.append(len([p for p in rt_root.iterdir() if p.name.startswith("legacy_rt_")]))
        real_stage(before)
        assert not [p for p in rt_root.iterdir() if p.name.startswith("legacy_rt_")]

    monkeypatch.setattr(runner, "_drop_new_scratch", _spy)
    _run(_Bus())
    # One round-trip dir existed while each table ran, none survived it.
    assert seen_during == [1, 1]


def test_keep_scratch_env_keeps_table_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv("OVID2_LEGACY_KEEP_SCRATCH", "1")
    _run(_Bus())
    rt_root = tmp_path / f"legacy_rt-{_IID}"
    assert len([p for p in rt_root.iterdir() if p.name.startswith("legacy_rt_")]) == 2
