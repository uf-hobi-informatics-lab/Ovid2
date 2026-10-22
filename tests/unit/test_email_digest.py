"""Unit tests for the email-digest renderer."""

from __future__ import annotations

import json
from email.message import EmailMessage
from pathlib import Path

import pytest

from ovid2.core import events as ev
from ovid2.display.renderers.email_digest import (
    BatchSummary,
    RecordingMailer,
    build_message,
    dispatch_digest,
    render_html,
    render_text,
    summarise,
)

_INSTANCE = "20260707-101112-abc123"


def _write_events(path: Path, events: list[ev.Event]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event.to_json_dict(), separators=(",", ":")) + "\n")


@pytest.mark.unit
def test_summarise_aggregates_per_batch() -> None:
    """Multiple events for one (pipeline, batch_id) collapse into one summary."""
    events = [
        ev.instance_started(instance_id=_INSTANCE, pipeline="site"),
        ev.table_started(
            instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="demographic"
        ),
        ev.table_completed(
            instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="demographic"
        ),
        ev.table_started(
            instance_id=_INSTANCE, pipeline="site", batch_id="20260101", table="encounter"
        ),
        ev.table_failed(
            instance_id=_INSTANCE,
            pipeline="site",
            batch_id="20260101",
            table="encounter",
            error="boom",
        ),
        ev.fix_applied(
            instance_id=_INSTANCE,
            pipeline="site",
            batch_id="20260101",
            table="demographic",
            fix="patid_orphans",
        ),
        ev.instance_finished(instance_id=_INSTANCE, success=False),
    ]
    records = [e.to_json_dict() for e in events]
    summaries = summarise(records)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.pipeline == "site"
    assert summary.tables_ok == ["demographic"]
    assert summary.tables_failed == [("encounter", "boom")]
    assert summary.fixes_applied == 1
    assert summary.success is False


@pytest.mark.unit
def test_render_text_lists_failures() -> None:
    """Plain-text renderer names the failing tables."""
    summary = BatchSummary(
        pipeline="site",
        batch_id="20260101",
        instance_id=_INSTANCE,
        tables_ok=["demographic"],
        tables_failed=[("encounter", "join error")],
        fixes_applied=5,
        fixes_failed=1,
        success=False,
    )
    text = render_text(summary)
    assert "site" in text
    assert "FAILURE" in text
    assert "encounter" in text
    assert "join error" in text


@pytest.mark.unit
def test_render_html_flags_status_color() -> None:
    """HTML renderer picks red for failure, green for success."""
    ok = BatchSummary(pipeline="site", batch_id="1", success=True)
    fail = BatchSummary(pipeline="site", batch_id="2", success=False)
    assert "#137333" in render_html(ok)
    assert "#c5221f" in render_html(fail)


@pytest.mark.unit
def test_build_message_has_both_bodies() -> None:
    """Every email carries a plain-text and HTML body."""
    summary = BatchSummary(pipeline="site", batch_id="20260101", success=True)
    msg = build_message(summary, sender="noreply@example.org", recipients=["ops@example.org"])
    assert isinstance(msg, EmailMessage)
    assert "site/20260101" in msg["Subject"]
    payloads = {part.get_content_type() for part in msg.walk() if not part.is_multipart()}
    assert "text/plain" in payloads
    assert "text/html" in payloads


@pytest.mark.unit
def test_dispatch_digest_sends_one_message_per_batch(tmp_path: Path) -> None:
    """A log with two distinct batches produces two messages."""
    log = tmp_path / "events.jsonl"
    events = []
    for batch in ("20260101", "20260102"):
        events.extend(
            [
                ev.instance_started(instance_id=_INSTANCE, pipeline="site"),
                ev.table_started(
                    instance_id=_INSTANCE, pipeline="site", batch_id=batch, table="demographic"
                ),
                ev.table_completed(
                    instance_id=_INSTANCE, pipeline="site", batch_id=batch, table="demographic"
                ),
                ev.instance_finished(instance_id=_INSTANCE, success=True),
            ]
        )
    _write_events(log, events)

    mailer = RecordingMailer()
    n = dispatch_digest(
        log,
        sender="noreply@example.org",
        recipients=["ops@example.org"],
        mailer=mailer,
    )
    assert n == 2
    assert len(mailer.sent) == 2


@pytest.mark.unit
def test_summarise_aggregates_health_run() -> None:
    """Health events (no batch_id) still produce a digest keyed on instance."""
    events = [
        ev.instance_started(instance_id=_INSTANCE),
        ev.check_started(instance_id=_INSTANCE, check_id="dc_1_01", name="Sex", part="1"),
        ev.check_completed(
            instance_id=_INSTANCE, check_id="dc_1_01", name="Sex", part="1", status="passed"
        ),
        ev.check_started(instance_id=_INSTANCE, check_id="dc_1_02", name="Race", part="1"),
        ev.check_completed(
            instance_id=_INSTANCE,
            check_id="dc_1_02",
            name="Race",
            part="1",
            status="exception",
            exception_count=3,
        ),
        ev.check_failed(
            instance_id=_INSTANCE, check_id="dc_2_01", name="Enc", part="2", error="crash"
        ),
        ev.instance_finished(instance_id=_INSTANCE, success=False),
    ]
    records = [e.to_json_dict() for e in events]
    summaries = summarise(records)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.is_health is True
    assert summary.checks_ok == ["dc_1_01"]
    assert ("dc_1_02", "exception") in summary.checks_failed
    assert ("dc_2_01", "crash") in summary.checks_failed
    assert summary.success is False


@pytest.mark.unit
def test_render_text_health_shows_checks_not_fixes() -> None:
    """Health summaries show a Checks line and omit the Fixes line."""
    summary = BatchSummary(
        instance_id=_INSTANCE,
        checks_ok=["dc_1_01"],
        checks_failed=[("dc_1_02", "exception")],
        success=False,
    )
    text = render_text(summary)
    assert "Checks:" in text
    assert "Fixes:" not in text
    assert "dc_1_02" in text
    html = render_html(summary)
    assert "Checks" in html
    assert "Fixes" not in html
    assert "#c5221f" in html


@pytest.mark.unit
def test_render_text_etl_omits_fixes_when_none() -> None:
    """An ETL run with no fixes does not print a Fixes line."""
    summary = BatchSummary(
        pipeline="site", batch_id="20260101", tables_ok=["demographic"], success=True
    )
    text = render_text(summary)
    assert "Tables:" in text
    assert "Fixes:" not in text
