"""Unit tests for ``ovid2.web.services.runs_svc``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ovid2.web import db as web_db
from ovid2.web.services import runs_svc


def _seed_data_batch(rows: list[tuple[str, str, str, str]]) -> None:
    """Create ``data_batch`` and insert rows for tests.

    Args:
        rows: Sequence of ``(batch_id, path, received_time, status)`` tuples.
    """
    from ovid2.scheduler.batch_tracker import _default_sqlite_path

    path = _default_sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS data_batch ("
        "batch_id TEXT PRIMARY KEY, path TEXT, received_time TEXT, "
        "status TEXT, priority TEXT, processing_start TEXT, processing_end TEXT)"
    )
    conn.executemany(
        "INSERT INTO data_batch (batch_id, path, received_time, status) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.mark.unit
def test_list_returns_empty_on_fresh_install(isolated_xdg: Path) -> None:
    """No scheduler DB + no web_jobs = empty result."""
    result = runs_svc.list_runs()
    assert result.total == 0
    assert result.runs == []


@pytest.mark.unit
def test_list_reads_data_batch_rows(isolated_xdg: Path) -> None:
    """Rows in ``data_batch`` are returned in descending received_time order."""
    _seed_data_batch(
        [
            ("B1", "/data/B1", "2026-01-01T10:00:00Z", "PROCESSED"),
            ("B2", "/data/B2", "2026-01-02T10:00:00Z", "NEW"),
        ]
    )
    result = runs_svc.list_runs()
    assert result.total == 2
    assert [r.batch_id for r in result.runs] == ["B2", "B1"]
    assert result.runs[0].status == "NEW"


@pytest.mark.unit
def test_list_filters_by_status(isolated_xdg: Path) -> None:
    """Status filter narrows the tracker query."""
    _seed_data_batch(
        [
            ("B1", "/x", "2026-01-01T10:00:00Z", "PROCESSED"),
            ("B2", "/y", "2026-01-02T10:00:00Z", "NEW"),
            ("B3", "/z", "2026-01-03T10:00:00Z", "ERROR"),
        ]
    )
    result = runs_svc.list_runs(status="ERROR")
    assert [r.batch_id for r in result.runs] == ["B3"]


@pytest.mark.unit
def test_empty_string_filters_do_not_hide_rows(isolated_xdg: Path) -> None:
    """Empty status/pipeline (the GET form's default) must not filter anything.

    Regression: the runs page submits ``?status=&pipeline=`` when the
    dropdown is "any status" and the pipeline box is blank. An empty
    string is not ``None``, so a naive ``is not None`` guard compared
    every row against ``""`` and dropped them all.
    """
    _seed_data_batch(
        [
            ("B1", "/x", "2026-01-01T10:00:00Z", "PROCESSED"),
            ("B2", "/y", "2026-01-02T10:00:00Z", "NEW"),
        ]
    )
    result = runs_svc.list_runs(status="", pipeline="")
    assert result.total == 2
    assert {r.batch_id for r in result.runs} == {"B1", "B2"}
    # Whitespace-only values are equally "no filter".
    result = runs_svc.list_runs(status="   ", pipeline="  ")
    assert result.total == 2


@pytest.mark.unit
def test_pipeline_filter_is_case_insensitive_substring(isolated_xdg: Path) -> None:
    """A partial, differently-cased pipeline query still matches its run."""
    _seed_data_batch([("B1", "/x", "2026-01-01T10:00:00Z", "PROCESSING")])
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs (job_id, submitted_at, submitted_by, pipeline, "
            "batch_id, input_address, output_address, tables_json, "
            "transformations_json, status, instance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-1",
                "2026-01-01T09:59:00Z",
                "web",
                "demo_pcornet",
                "B1",
                "disc_input",
                "disc_output",
                '["all"]',
                '["all"]',
                "running",
                "20260101-100000-abcdef",
            ),
        )
        conn.commit()
    # Partial + upper-cased query still finds the run.
    result = runs_svc.list_runs(pipeline="DEMO")
    assert [r.batch_id for r in result.runs] == ["B1"]
    # A non-matching substring returns nothing.
    result = runs_svc.list_runs(pipeline="nope")
    assert result.runs == []


@pytest.mark.unit
def test_list_merges_web_job_metadata(isolated_xdg: Path) -> None:
    """When a batch was submitted via the web tier, extra fields are joined."""
    _seed_data_batch([("B1", "/x", "2026-01-01T10:00:00Z", "PROCESSING")])
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs (job_id, submitted_at, submitted_by, pipeline, "
            "batch_id, input_address, output_address, tables_json, "
            "transformations_json, status, instance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "job-1",
                "2026-01-01T09:59:00Z",
                "web",
                "demo",
                "B1",
                "disc_input",
                "disc_output",
                '["all"]',
                '["all"]',
                "running",
                "20260101-100000-abcdef",
            ),
        )
    result = runs_svc.list_runs()
    row = result.runs[0]
    assert row.job_id == "job-1"
    assert row.pipeline == "demo"
    assert row.instance_id == "20260101-100000-abcdef"
    assert row.submitted_by == "web"


@pytest.mark.unit
def test_get_returns_none_for_unknown(isolated_xdg: Path) -> None:
    """Unknown batch id yields ``None``."""
    assert runs_svc.get_run("nope") is None


@pytest.mark.unit
def test_pagination_offset_and_limit(isolated_xdg: Path) -> None:
    """Offset + limit clamp to the requested slice."""
    _seed_data_batch(
        [
            (f"B{i:02d}", f"/data/B{i:02d}", f"2026-01-{i:02d}T00:00:00Z", "NEW")
            for i in range(1, 11)
        ]
    )
    page = runs_svc.list_runs(limit=3, offset=2)
    assert page.total == 10
    assert page.limit == 3
    assert page.offset == 2
    assert len(page.runs) == 3


@pytest.mark.unit
@pytest.mark.parametrize(
    ("web_status", "expected"),
    [
        ("queued", "NEW"),
        ("running", "PROCESSING"),
        ("done", "PROCESSED"),
        ("failed", "ERROR"),
        ("cancelled", "CANCELLED"),
    ],
)
def test_status_from_web_status_maps_cancelled_distinctly(
    web_status: str, expected: str
) -> None:
    """A cancelled job maps to its own label, not ``ERROR``.

    Regression guard: cancelling a run used to collapse into ``ERROR``,
    which rendered the runs list badge bright red instead of the muted
    ``status-cancelled`` styling.
    """
    assert runs_svc._status_from_web_status(web_status) == expected


def _seed_web_job(
    job_id: str,
    *,
    batch_id: str,
    submitted_at: str,
    instance_id: str,
    status: str = "done",
    pipeline: str = "demo",
) -> None:
    """Insert one web_jobs row for the per-job list tests."""
    web_db.ensure_schema()
    with web_db.connect() as conn:
        conn.execute(
            "INSERT INTO web_jobs (job_id, submitted_at, submitted_by, pipeline, "
            "batch_id, input_address, output_address, tables_json, "
            "transformations_json, status, instance_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                submitted_at,
                "web",
                pipeline,
                batch_id,
                "disc_input",
                "disc_output",
                '["all"]',
                '["all"]',
                status,
                instance_id,
            ),
        )


@pytest.mark.unit
def test_list_is_one_row_per_job_not_per_batch(isolated_xdg: Path) -> None:
    """Multiple jobs sharing a batch_id each get their own row.

    Regression guard for the merged workspace: the old Jobs page showed
    one row per submission / rerun. Collapsing by batch_id hid reruns
    (e.g. batch '20260610' with 49 submissions showed as a single row).
    """
    _seed_data_batch([("B1", "/x", "2026-01-01T10:00:00Z", "PROCESSED")])
    _seed_web_job(
        "job-a",
        batch_id="B1",
        submitted_at="2026-01-01T09:00:00Z",
        instance_id="20260101-090000-aaaaaa",
    )
    _seed_web_job(
        "job-b",
        batch_id="B1",
        submitted_at="2026-01-01T10:00:00Z",
        instance_id="20260101-100000-bbbbbb",
    )
    result = runs_svc.list_runs()
    # Two jobs on the same batch -> two rows (newest submission first),
    # not one collapsed batch row.
    assert result.total == 2
    assert [r.job_id for r in result.runs] == ["job-b", "job-a"]
    # Both enriched with the shared batch's path.
    assert all(r.path == "/x" for r in result.runs)


@pytest.mark.unit
def test_list_surfaces_web_jobs_without_a_data_batch_row(isolated_xdg: Path) -> None:
    """Web jobs whose batch never entered the scheduler still appear.

    Regression guard for the empty-workspace bug: with a data_batch
    table present but no matching row, the merged list must still surface
    the web job (previously it returned zero rows).
    """
    _seed_data_batch([("OTHER", "/o", "2026-01-01T00:00:00Z", "NEW")])
    _seed_web_job(
        "job-orphan",
        batch_id="NO_BATCH_ROW",
        submitted_at="2026-02-01T12:00:00Z",
        instance_id="20260201-120000-cccccc",
    )
    result = runs_svc.list_runs()
    ids = {r.job_id for r in result.runs if r.job_id}
    assert "job-orphan" in ids
    # And the scheduler-only batch (no web job) is present too.
    assert any(r.batch_id == "OTHER" and r.job_id is None for r in result.runs)


@pytest.mark.unit
def test_get_run_resolves_by_instance_id(isolated_xdg: Path) -> None:
    """A run is resolvable by instance_id (the list-row identifier)."""
    _seed_data_batch([("B9", "/x", "2026-01-01T10:00:00Z", "PROCESSED")])
    _seed_web_job(
        "job-9",
        batch_id="B9",
        submitted_at="2026-01-01T09:00:00Z",
        instance_id="20260101-090000-999999",
    )
    run = runs_svc.get_run("20260101-090000-999999")
    assert run is not None
    assert run.job_id == "job-9"
