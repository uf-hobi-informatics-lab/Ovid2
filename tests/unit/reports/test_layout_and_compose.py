"""Unit tests for the tier-neutral Report Studio core (layout ops, compose, sources)."""

from __future__ import annotations

import pytest

from ovid2.reports import catalogue, layout
from ovid2.reports.compose import render_model
from ovid2.reports.samples import sample_sources
from ovid2.reports.sources import describe_source


@pytest.fixture(scope="module")
def health() -> dict:
    return sample_sources()["health"]


@pytest.fixture()
def base(health: dict) -> dict:
    tmpl = catalogue.get_template("health.edc_summary")
    assert tmpl is not None
    return layout.normalise(tmpl.layout)


def test_ops_reorder_hide_and_restyle(base: dict, health: dict) -> None:
    new, notes = layout.apply_ops(
        base,
        [
            {"op": "move_section", "section_id": "action", "index": 0},
            {"op": "set_visibility", "target_id": "kpi_exc", "visible": False},
            {
                "op": "set_report",
                "set": {"theme": "grayscale_print", "page": {"orientation": "landscape"}},
            },
        ],
        health,
    )
    assert new["sections"][0]["id"] == "action"
    assert new["theme"] == "grayscale_print" and new["page"]["orientation"] == "landscape"
    assert new["page"]["size"] == "letter"  # merged, not replaced
    assert len(notes) == 3
    # The input is never mutated.
    assert base["sections"][0]["id"] == "overview"
    model = render_model(new, health)
    rendered = {b["id"] for s in model["sections"] for b in s["blocks"]}
    assert "kpi_exc" not in rendered and "kpi_total" in rendered
    assert model["sections"][0]["id"] == "action"


def test_hidden_section_is_not_rendered(base: dict, health: dict) -> None:
    new, _ = layout.apply_ops(
        base, [{"op": "set_visibility", "target_id": "status", "visible": False}], health
    )
    assert "status" not in {s["id"] for s in render_model(new, health)["sections"]}


def test_add_move_and_remove_block(base: dict, health: dict) -> None:
    new, _ = layout.apply_ops(
        base,
        [
            {
                "op": "add_block",
                "section_id": "overview",
                "index": 0,
                "block": {
                    "type": "chart",
                    "chart": "bar",
                    "dataset": "parts",
                    "params": {"category": "part", "value": "checks"},
                },
            }
        ],
        health,
    )
    added = new["sections"][0]["blocks"][0]
    assert added["chart"] == "bar" and added["id"] == "bar"
    moved, _ = layout.apply_ops(
        new, [{"op": "move_block", "block_id": "bar", "section_id": "status", "index": -1}], health
    )
    assert moved["sections"][1]["blocks"][-1]["id"] == "bar"
    gone, _ = layout.apply_ops(moved, [{"op": "remove_block", "block_id": "bar"}], health)
    assert "bar" not in {b["id"] for _s, b in layout.iter_blocks(gone)}


def test_update_block_none_removes_key(base: dict, health: dict) -> None:
    new, _ = layout.apply_ops(
        base,
        [{"op": "update_block", "block_id": "intro", "set": {"text": "Hello", "text_ref": None}}],
        health,
    )
    intro = next(b for _s, b in layout.iter_blocks(new) if b["id"] == "intro")
    assert intro["text"] == "Hello" and "text_ref" not in intro


@pytest.mark.parametrize(
    ("ops", "needle"),
    [
        ([{"op": "explode"}], "unknown op"),
        ([{"op": "remove_section", "section_id": "nope"}], "no section"),
        ([{"op": "set_report", "set": {"theme": "neon"}}], "unknown report theme"),
        (
            [
                {
                    "op": "update_block",
                    "block_id": "status_donut",
                    "set": {"params": {"category": "nope", "value": "checks"}},
                }
            ],
            "no column",
        ),
        (
            [
                {
                    "op": "update_block",
                    "block_id": "status_donut",
                    "set": {"params": {"category": "status", "value": "status"}},
                }
            ],
            "needs one of",
        ),
        ([{"op": "update_block", "block_id": "intro", "set": {"script": "x"}}], "unknown field"),
    ],
)
def test_bad_ops_are_rejected_atomically(base: dict, health: dict, ops: list, needle: str) -> None:
    with pytest.raises(layout.LayoutError) as exc:
        layout.apply_ops(
            base, [{"op": "move_section", "section_id": "action", "index": 0}, *ops], health
        )
    assert needle in str(exc.value)


def test_ops_batch_limit(base: dict) -> None:
    with pytest.raises(layout.LayoutError):
        layout.apply_ops(base, [{"op": "set_report", "set": {}}] * (layout.MAX_OPS + 1))


def test_small_cell_suppression() -> None:
    """Record counts (is_count) in (0, threshold) are suppressed everywhere."""
    analysis = sample_sources()["analysis"]
    lay = layout.normalise(
        {
            "v": 1,
            "title": "T",
            "options": {"small_cell_threshold": 11},
            "sections": [
                {
                    "id": "s",
                    "title": "S",
                    "blocks": [
                        {
                            "id": "t",
                            "type": "chart",
                            "chart": "table",
                            "dataset": "top_values",
                            "params": {},
                        }
                    ],
                }
            ],
        }
    )
    assert not layout.validate_layout(lay, analysis)
    model = render_model(lay, analysis)
    assert model["provenance"]["small_cell_threshold"] == 11
    assert model["provenance"]["suppressed_cells"] == 1  # the IP row has 5 records
    table = model["sections"][0]["blocks"][0]["table"]
    assert "5" not in [c for row in table["rows"] for c in row]


def test_describe_source_redacts_row_values() -> None:
    analysis = sample_sources()["analysis"]
    open_ = describe_source(analysis)
    shut = describe_source(analysis, redact_row_values=True)
    col = next(c for c in open_["datasets"]["top_values"]["columns"] if c["name"] == "value")
    assert col["sensitivity"] == "row_values" and "AV" in col["examples"]
    col2 = next(c for c in shut["datasets"]["top_values"]["columns"] if c["name"] == "value")
    assert "examples" not in col2
    assert "rows" not in shut["datasets"]["top_values"]


def test_outline_has_ids(base: dict) -> None:
    out = layout.outline(base)
    assert out["sections"][0]["blocks"][0]["id"] == "intro"
