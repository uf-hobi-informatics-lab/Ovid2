"""Contract: every ``ovid2.core.events`` constructor emits a schema-valid event.

Runs every typed constructor with representative arguments, converts the
result to its JSON-serialisable form, and validates it against the
bundled ``events.v1.json`` schema. Any drift between the schema and the
Python constructors trips this test.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

pytest.importorskip("jsonschema")

from jsonschema import Draft202012Validator

from ovid2.core import events as ev


def _load_schema() -> dict:
    """Load the bundled event schema."""
    schema_ref = resources.files("ovid2.resources.schemas") / "events.v1.json"
    with schema_ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    """Return a validator built from the current event schema."""
    return Draft202012Validator(_load_schema())


_INSTANCE_ID = "20260707-101112-abc123"


def _pairs() -> list[tuple[str, ev.Event]]:
    """Return one (name, event) pair per typed constructor."""
    return [
        ("instance_started", ev.instance_started(instance_id=_INSTANCE_ID, pipeline="site")),
        ("instance_finished", ev.instance_finished(instance_id=_INSTANCE_ID, success=True)),
        (
            "table_started",
            ev.table_started(
                instance_id=_INSTANCE_ID, pipeline="site", batch_id="20260101", table="encounter"
            ),
        ),
        (
            "table_completed",
            ev.table_completed(
                instance_id=_INSTANCE_ID,
                pipeline="site",
                batch_id="20260101",
                table="encounter",
                elapsed_ms=1234.5,
            ),
        ),
        (
            "table_failed",
            ev.table_failed(
                instance_id=_INSTANCE_ID,
                pipeline="site",
                batch_id="20260101",
                table="encounter",
                error="boom",
            ),
        ),
        (
            "fix_applied",
            ev.fix_applied(
                instance_id=_INSTANCE_ID,
                pipeline="site",
                batch_id="20260101",
                table="encounter",
                fix="patid_orphans",
                current_fix=1,
                total_fixes=3,
                src1="demographic",
            ),
        ),
        (
            "fix_failed",
            ev.fix_failed(
                instance_id=_INSTANCE_ID,
                pipeline="site",
                batch_id="20260101",
                table="encounter",
                fix="patid_orphans",
                error="reference missing",
                current_fix=1,
                total_fixes=3,
                src1="demographic",
            ),
        ),
        (
            "batch_completed",
            ev.batch_completed(instance_id=_INSTANCE_ID, pipeline="site", batch_id="20260101"),
        ),
        (
            "check_started",
            ev.check_started(
                instance_id=_INSTANCE_ID, check_id="1.01", name="DC_1_01", part="part1"
            ),
        ),
        (
            "check_completed",
            ev.check_completed(
                instance_id=_INSTANCE_ID,
                check_id="1.01",
                name="DC_1_01",
                part="part1",
                status="exception",
                exception_count=2,
                metric_count=5,
                elapsed_ms=1234.5,
            ),
        ),
        (
            "check_failed",
            ev.check_failed(
                instance_id=_INSTANCE_ID,
                check_id="1.01",
                name="DC_1_01",
                part="part1",
                error="boom",
                elapsed_ms=42.0,
            ),
        ),
        (
            "batch_failed",
            ev.batch_failed(
                instance_id=_INSTANCE_ID, pipeline="site", batch_id="20260101", error="oops"
            ),
        ),
        (
            "log_event",
            ev.log_event(instance_id=_INSTANCE_ID, level="info", message="hello"),
        ),
        (
            "warning_event",
            ev.warning_event(instance_id=_INSTANCE_ID, message="watch it"),
        ),
        (
            "error_event",
            ev.error_event(instance_id=_INSTANCE_ID, message="broken"),
        ),
    ]


@pytest.mark.contract
@pytest.mark.parametrize(("name", "event"), _pairs(), ids=[p[0] for p in _pairs()])
def test_constructor_output_validates(
    name: str, event: ev.Event, validator: Draft202012Validator
) -> None:
    """Each constructor produces a schema-valid record."""
    record = event.to_json_dict()
    errors = sorted(validator.iter_errors(record), key=lambda e: e.path)
    assert not errors, f"{name} produced invalid record: {record}\n" + "\n".join(
        str(e.message) for e in errors
    )


@pytest.mark.contract
def test_event_kinds_match_schema() -> None:
    """Python ``EVENT_KINDS`` and the schema enum must be identical."""
    schema = _load_schema()
    schema_kinds = set(schema["properties"]["kind"]["enum"])
    assert set(ev.EVENT_KINDS) == schema_kinds
