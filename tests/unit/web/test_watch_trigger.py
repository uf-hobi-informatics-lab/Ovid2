"""Phase-B ``watch`` trigger + web-worker watch dispatch."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ovid2.web.models import (
    PipelineConfigDetail,
    PipelineTrigger,
    PipelineWrite,
    StorageBrowseEntry,
    StorageBrowseResponse,
)
from ovid2.web.services import pipelines_svc, watch_svc
from ovid2.web.worker import Worker


def _browse(names_kinds: list[tuple[str, str]]) -> StorageBrowseResponse:
    return StorageBrowseResponse(
        storage="c_in",
        type="disc",
        prefix="",
        supported=True,
        entries=[StorageBrowseEntry(name=n, kind=k, path=n) for n, k in names_kinds],
    )


@pytest.fixture()
def watch_pipeline(isolated_xdg: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Register a stub pipeline package + W7 connection-shape addresses.

    Returns the pipeline (pipeline) name. The input address is a
    connection-shape address wrapping a disc connection so watch
    validation + scanning have a storage account to browse.
    """
    root = tmp_path / "pipelines_root"
    pkg = root / "ovid2_pipelines" / "tp_watch"
    (pkg / "formatters").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "pipeline_settings.py").write_text(
        "mappers_type = 'pcornet_mappers'\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(root))
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]

    connections = Path(os.environ["OVID2_CONNECTIONS_DIR"])
    (connections / "c_in.json").write_text(
        json.dumps({"type": "disc", "args": {"host_path": str(tmp_path)}}),
        encoding="utf-8",
    )
    configs = Path(os.environ["OVID2_CONFIGS_DIR"])
    (configs / "in_watch.json").write_text(
        json.dumps({"connection": "c_in", "location": {}}), encoding="utf-8"
    )
    (configs / "out_watch.json").write_text(
        json.dumps({"src_name": "disc", "src_args": {"path": str(tmp_path / "out")}}),
        encoding="utf-8",
    )
    yield "tp_watch"
    for mod in list(sys.modules):
        if mod == "ovid2_pipelines" or mod.startswith("ovid2_pipelines."):
            del sys.modules[mod]


# --------------------------------------------------------------------------- #
# cursor-diff logic
# --------------------------------------------------------------------------- #


def _detail(pipeline: str) -> PipelineConfigDetail:
    return PipelineConfigDetail(
        name="wp",
        path="wp.json",
        kind="etl",
        cdm=None,
        pipeline=pipeline,
        input_address="in_watch",
        output_address="out_watch",
        trigger=PipelineTrigger(type="watch"),
    )


@pytest.mark.unit
def test_scan_detects_new_folder_once(watch_pipeline: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_svc.storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: _browse([("2024_01", "dir"), ("readme.txt", "file")]),
    )
    detail = _detail(watch_pipeline)
    first = watch_svc.scan_for_new_batches(detail)
    assert first == ["2024_01"]
    watch_svc.record_seen(detail.name, detail.input_address, "2024_01")
    second = watch_svc.scan_for_new_batches(detail)
    assert second == []


@pytest.mark.unit
def test_scan_returns_only_folders_after_cursor(
    watch_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        watch_svc.storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: _browse(
            [("2024_01", "dir"), ("2024_02", "dir"), ("2024_03", "dir")]
        ),
    )
    detail = _detail(watch_pipeline)
    watch_svc.record_seen(detail.name, detail.input_address, "2024_02")
    assert watch_svc.scan_for_new_batches(detail) == ["2024_03"]


@pytest.mark.unit
def test_scan_skips_files(watch_pipeline: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watch_svc.storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: _browse([("data.csv", "file"), ("x.txt", "file")]),
    )
    assert watch_svc.scan_for_new_batches(_detail(watch_pipeline)) == []


@pytest.mark.unit
def test_scan_unsupported_driver_returns_empty(
    watch_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        watch_svc.storage_browse_svc,
        "browse_storage",
        lambda name, prefix=None: StorageBrowseResponse(
            storage="c_in", type="snowflake", supported=False, entries=[]
        ),
    )
    assert watch_svc.scan_for_new_batches(_detail(watch_pipeline)) == []


@pytest.mark.unit
def test_scan_legacy_address_returns_empty(
    watch_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = _detail(watch_pipeline).model_copy(update={"input_address": "out_watch"})
    assert watch_svc.scan_for_new_batches(detail) == []


# --------------------------------------------------------------------------- #
# _validate_trigger acceptance / rejection
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_write_watch_route_accepts_browsable_input(
    watch_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ovid2.web.services.storage_browse_svc as sbs

    monkeypatch.setattr(
        sbs, "browse_storage", lambda name, prefix=None: _browse([("2024_01", "dir")])
    )
    result = pipelines_svc.write_pipeline(
        "wp",
        PipelineWrite(
            kind="etl",
            pipeline=watch_pipeline,
            input_address="in_watch",
            output_address="out_watch",
            trigger=PipelineTrigger(type="watch"),
        ),
        actor="test",
        create_only=True,
    )
    assert result.action == "created"
    detail = pipelines_svc.get_pipeline("wp")
    assert detail is not None
    assert detail.trigger.type == "watch"


@pytest.mark.unit
def test_write_watch_route_rejects_legacy_input(watch_pipeline: str) -> None:
    with pytest.raises(pipelines_svc.PipelineError, match="connection-shape"):
        pipelines_svc.write_pipeline(
            "wp",
            PipelineWrite(
                kind="etl",
                pipeline=watch_pipeline,
                input_address="out_watch",  # legacy address, no connection
                output_address="out_watch",
                trigger=PipelineTrigger(type="watch"),
            ),
            actor="test",
            create_only=True,
        )


@pytest.mark.unit
def test_compute_next_fire_none_for_watch() -> None:
    assert pipelines_svc.compute_next_fire(PipelineTrigger(type="watch")) is None


@pytest.mark.unit
def test_due_pipelines_skips_watch(watch_pipeline: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import ovid2.web.services.storage_browse_svc as sbs

    monkeypatch.setattr(
        sbs, "browse_storage", lambda name, prefix=None: _browse([("2024_01", "dir")])
    )
    pipelines_svc.write_pipeline(
        "wp",
        PipelineWrite(
            kind="etl",
            pipeline=watch_pipeline,
            input_address="in_watch",
            output_address="out_watch",
            trigger=PipelineTrigger(type="watch"),
        ),
        actor="test",
        create_only=True,
    )
    assert all(d.name != "wp" for d in pipelines_svc.due_pipelines())


# --------------------------------------------------------------------------- #
# worker dispatch
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_worker_dispatch_fires_one_job_per_new_folder(
    watch_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ovid2.web.services.storage_browse_svc as sbs

    monkeypatch.setattr(
        sbs,
        "browse_storage",
        lambda name, prefix=None: _browse([("2024_01", "dir"), ("2024_02", "dir")]),
    )
    pipelines_svc.write_pipeline(
        "wp",
        PipelineWrite(
            kind="etl",
            pipeline=watch_pipeline,
            input_address="in_watch",
            output_address="out_watch",
            trigger=PipelineTrigger(type="watch"),
        ),
        actor="test",
        create_only=True,
    )

    fired: list[str] = []

    def _fake_run_now(name: str, *, actor: str = "web", batch_id: str | None = None):
        fired.append(str(batch_id))
        from ovid2.web.models import JobSubmitResponse

        return JobSubmitResponse(
            job_id=f"job-{batch_id}", instance_id=f"i-{batch_id}", status="queued"
        )

    monkeypatch.setattr(pipelines_svc, "run_now", _fake_run_now)

    worker = Worker(max_parallel=0)
    worker._dispatch_watch_pipelines()
    assert sorted(fired) == ["2024_01", "2024_02"]

    # Second tick: cursor advanced, nothing new.
    fired.clear()
    worker._dispatch_watch_pipelines()
    assert fired == []


@pytest.mark.unit
def test_worker_watch_gated_by_route_dispatch_flag(
    watch_pipeline: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ovid2.web.services.storage_browse_svc as sbs
    from ovid2.web.models import GlobalSettings, SettingsWorker
    from ovid2.web.services import settings_svc

    monkeypatch.setattr(
        sbs, "browse_storage", lambda name, prefix=None: _browse([("2024_01", "dir")])
    )
    settings_svc.save_settings(
        GlobalSettings(worker=SettingsWorker(route_dispatch_enabled=False)),
        actor="test",
    )
    pipelines_svc.write_pipeline(
        "wp",
        PipelineWrite(
            kind="etl",
            pipeline=watch_pipeline,
            input_address="in_watch",
            output_address="out_watch",
            trigger=PipelineTrigger(type="watch"),
        ),
        actor="test",
        create_only=True,
    )
    fired: list[str] = []
    monkeypatch.setattr(
        pipelines_svc,
        "run_now",
        lambda name, *, actor="web", batch_id=None: fired.append(batch_id),
    )
    Worker(max_parallel=0)._dispatch_watch_pipelines()
    assert fired == []


@pytest.mark.unit
def test_worker_watch_debounce(watch_pipeline: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import ovid2.web.services.storage_browse_svc as sbs

    scans = {"n": 0}

    def _counting_browse(name: str, prefix: str | None = None) -> StorageBrowseResponse:
        scans["n"] += 1
        return _browse([])

    monkeypatch.setattr(sbs, "browse_storage", _counting_browse)
    pipelines_svc.write_pipeline(
        "wp",
        PipelineWrite(
            kind="etl",
            pipeline=watch_pipeline,
            input_address="in_watch",
            output_address="out_watch",
            trigger=PipelineTrigger(type="watch", watch_min_wait_minutes=60),
        ),
        actor="test",
        create_only=True,
    )
    worker = Worker(max_parallel=0)
    worker._dispatch_watch_pipelines()
    worker._dispatch_watch_pipelines()
    # The write itself validated the trigger (one browse) then the first
    # dispatch scanned (one browse); the second dispatch is debounced.
    assert scans["n"] == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
