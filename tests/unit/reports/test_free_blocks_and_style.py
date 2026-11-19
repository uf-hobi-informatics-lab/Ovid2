"""Unit tests: free-form blocks, per-component style, export mode, templates.

Covers the Report Studio additions in :mod:`ovid2.reports.layout` and
:mod:`ovid2.reports.compose`: ``note`` / ``info`` / ``checklist`` /
``divider`` / ``spacer`` blocks, ``notes`` sections, ``style`` overrides
(``merge_style``, reset, theme-role resolution, chart config layering),
the ``export: false`` flag, render modes and template scrubbing.
"""

from __future__ import annotations

import pytest

from ovid2.reports import catalogue, layout
from ovid2.reports.compose import render_model, resolve_color, resolve_style
from ovid2.reports.samples import sample_sources


@pytest.fixture(scope="module")
def health() -> dict:
    return sample_sources()["health"]


@pytest.fixture()
def base(health: dict) -> dict:
    tmpl = catalogue.get_template("health.edc_summary")
    assert tmpl is not None
    return layout.normalise(tmpl.layout)


def _notes_section(base: dict, health: dict, *blocks: dict) -> dict:
    ops = [{"op": "add_section", "title": "Reviewer notes", "kind": "notes", "section_id": "nts"}]
    ops += [{"op": "add_block", "section_id": "nts", "block": b} for b in blocks]
    new, _ = layout.apply_ops(base, ops, health, actor="alice", now="2026-11-17T10:00:00+00:00")
    return new


def test_free_blocks_validate_and_get_defaults(base: dict, health: dict) -> None:
    new = _notes_section(
        base,
        health,
        {"type": "note", "text": "**check** labs"},
        {"type": "info"},
        {"type": "checklist"},
        {"type": "divider"},
        {"type": "spacer"},
    )
    sec = next(s for s in new["sections"] if s["id"] == "nts")
    assert sec["kind"] == "notes"
    note, info, check, div, spacer = sec["blocks"]
    assert note["author"] == "alice" and note["updated_at"].startswith("2026-11-17")
    assert note["tone"] == "note"
    assert info["rows"] == [{"key": "Item", "value": ""}]
    assert check["items"][0]["done"] is False
    assert spacer["size"] == "md" and div["type"] == "divider"
    assert layout.validate_layout(new, health) == []


def test_client_cannot_forge_note_author(base: dict, health: dict) -> None:
    new = _notes_section(base, health, {"type": "note", "text": "x", "author": "mallory"})
    note = next(s for s in new["sections"] if s["id"] == "nts")["blocks"][0]
    assert note["author"] == "alice"
    edited, _ = layout.apply_ops(
        new,
        [{"op": "update_block", "block_id": note["id"], "set": {"author": "mallory", "text": "y"}}],
        health,
        actor="bob",
    )
    sec = next(s for s in edited["sections"] if s["id"] == "nts")
    assert sec["blocks"][0]["author"] == "bob"


@pytest.mark.parametrize(
    ("block", "needle"),
    [
        ({"type": "info", "items": [{"text": "x"}]}, "only checklist blocks have items"),
        ({"type": "note", "text": "x", "dataset": "checks"}, "cannot have ['dataset']"),
        ({"type": "spacer", "rows": [{"key": "a"}]}, "only info blocks have rows"),
        ({"type": "checklist", "items": [{"done": True}]}, "'text' is a required property"),
        ({"type": "text", "text": "x", "style": {"accent": "pink"}}, "does not match"),
        ({"type": "text", "text": "x", "style": {"glow": True}}, "Additional properties"),
    ],
)
def test_bad_free_blocks_rejected(base: dict, health: dict, block: dict, needle: str) -> None:
    sid = base["sections"][0]["id"]
    with pytest.raises(layout.LayoutError) as exc:
        layout.apply_ops(base, [{"op": "add_block", "section_id": sid, "block": block}], health)
    assert any(needle in e for e in exc.value.errors), exc.value.errors


def test_merge_style_sets_resets_and_drops_empty(base: dict, health: dict) -> None:
    bid = base["sections"][0]["blocks"][0]["id"]
    new, _ = layout.apply_ops(
        base,
        [
            {
                "op": "update_block",
                "block_id": bid,
                "set": {"style": {"accent": "palette:2"}},
                "merge_style": True,
            },
            {
                "op": "update_block",
                "block_id": bid,
                "set": {"style": {"align": "center"}},
                "merge_style": True,
            },
        ],
        health,
    )
    blk = layout.normalise(new)["sections"][0]["blocks"][0]
    assert blk["style"] == {"accent": "palette:2", "align": "center"}
    reset, _ = layout.apply_ops(
        new,
        [
            {
                "op": "update_block",
                "block_id": bid,
                "set": {"style": {"accent": None, "align": None}},
                "merge_style": True,
            }
        ],
        health,
    )
    assert "style" not in reset["sections"][0]["blocks"][0]
    # Section styles use the same merge.
    sid = base["sections"][0]["id"]
    sec, _ = layout.apply_ops(
        base,
        [
            {
                "op": "update_section",
                "section_id": sid,
                "set": {"style": {"background": "surface"}},
                "merge_style": True,
            }
        ],
        health,
    )
    assert sec["sections"][0]["style"] == {"background": "surface"}


def test_style_resolves_against_theme_and_follows_theme_changes() -> None:
    pub = catalogue.get_theme("publication")
    dark = catalogue.get_theme("presentation_dark")
    assert resolve_color("accent", pub) == pub.colors["accent"]
    assert resolve_color("accent", dark) == dark.colors["accent"] != pub.colors["accent"]
    assert resolve_color("palette:1", pub) == pub.palette[1]
    assert resolve_color("status:passed", pub) == pub.status["passed"]
    assert resolve_color("#ABCDEF", dark) == "#abcdef"
    assert resolve_color("nope", pub) is None
    assert resolve_style({"accent": "text", "emphasis": "card"}, pub) == {
        "accent": pub.colors["text"],
        "emphasis": "card",
    }


def test_chart_config_takes_block_colours(base: dict, health: dict) -> None:
    chart_block = next(
        b
        for _s, b in layout.iter_blocks(base)
        if b["type"] == "chart" and b.get("chart") not in ("kpi", "table")
    )
    new, _ = layout.apply_ops(
        base,
        [
            {
                "op": "update_block",
                "block_id": chart_block["id"],
                "set": {"style": {"accent": "#123456", "text_color": "#654321"}},
                "merge_style": True,
            }
        ],
        health,
    )
    model = render_model(new, health)
    b = next(x for s in model["sections"] for x in s["blocks"] if x["id"] == chart_block["id"])
    if b["renderer"] != "vega":
        pytest.skip("template's first chart is not a vega chart")
    cfg = b["spec"]["config"]
    assert cfg["bar"]["color"] == "#123456" and cfg["mark"]["color"] == "#123456"
    assert cfg["title"]["color"] == "#654321"
    assert b["style_resolved"]["accent"] == "#123456"


def test_export_flag_and_render_modes(base: dict, health: dict) -> None:
    new = _notes_section(
        base,
        health,
        {"type": "text", "text": "WORKING-ONLY", "export": False},
        {"type": "note", "text": "SHARED-NOTE"},
    )
    hidden_id = base["sections"][0]["blocks"][0]["id"]
    new, _ = layout.apply_ops(
        new, [{"op": "set_visibility", "target_id": hidden_id, "visible": False}], health
    )

    def texts(model: dict) -> set[str]:
        return {b.get("markdown") for s in model["sections"] for b in s["blocks"]}

    def ids(model: dict) -> set[str]:
        return {b["id"] for s in model["sections"] for b in s["blocks"]}

    view = render_model(new, health)
    assert "WORKING-ONLY" in texts(view) and hidden_id not in ids(view)
    edit = render_model(new, health, mode="edit")
    assert hidden_id in ids(edit)
    assert (
        next(b for s in edit["sections"] for b in s["blocks"] if b["id"] == hidden_id)["visible"]
        is False
    )
    exp = render_model(new, health, mode="export")
    assert "WORKING-ONLY" not in texts(exp) and "SHARED-NOTE" in texts(exp)
    assert hidden_id not in ids(exp)
    with pytest.raises(ValueError):
        render_model(new, health, mode="bogus")


def test_free_block_render_fields(base: dict, health: dict) -> None:
    new = _notes_section(
        base,
        health,
        {"type": "info", "rows": [{"key": "Reviewer", "value": "Jane"}]},
        {"type": "checklist", "items": [{"text": "Re-run", "done": True}]},
        {"type": "spacer", "size": "lg"},
    )
    sec = next(s for s in render_model(new, health)["sections"] if s["id"] == "nts")
    assert sec["kind"] == "notes"
    info, check, spacer = sec["blocks"]
    assert info["renderer"] == "info" and info["rows"][0] == {"key": "Reviewer", "value": "Jane"}
    assert check["renderer"] == "checklist" and check["items"][0]["done"] is True
    assert spacer["renderer"] == "spacer" and spacer["size"] == "lg"


def test_as_template_scrubs_values_and_authorship(base: dict, health: dict) -> None:
    new = _notes_section(
        base,
        health,
        {"type": "info", "rows": [{"key": "Reviewer", "value": "Jane"}]},
        {"type": "checklist", "items": [{"text": "Send", "done": True}]},
        {"type": "note", "text": "keep me"},
    )
    tmpl = layout.as_template(new)
    sec = next(s for s in tmpl["sections"] if s["id"] == "nts")
    info, check, note = sec["blocks"]
    assert info["rows"] == [{"key": "Reviewer", "value": ""}]
    assert check["items"] == [{"text": "Send", "done": False}]
    assert "author" not in note and note["text"] == "keep me"
    assert layout.validate_layout(tmpl, health) == []


def test_merge_blocks_anchors_and_orphans(base: dict) -> None:
    sid = base["sections"][0]["id"]
    first = base["sections"][0]["blocks"][0]["id"]
    merged = layout.merge_blocks(
        base,
        [
            (sid, None, {"id": "pn_aaaaaaaaaaaa", "type": "note", "text": "top"}),
            (sid, first, {"id": "pn_bbbbbbbbbbbb", "type": "note", "text": "after"}),
            ("gone", None, {"id": "pn_cccccccccccc", "type": "note", "text": "orphan"}),
        ],
    )
    ids = [b["id"] for b in merged["sections"][0]["blocks"]]
    assert ids[0] == "pn_aaaaaaaaaaaa" and ids[2] == "pn_bbbbbbbbbbbb"
    tray = merged["sections"][-1]
    assert tray["kind"] == "notes" and tray["blocks"][0]["id"] == "pn_cccccccccccc"
    assert base["sections"][0]["blocks"][0]["id"] == first  # not mutated


def test_validate_free_block() -> None:
    assert layout.validate_free_block({"id": "pn_1", "type": "note", "text": "x"}) == []
    assert layout.validate_free_block({"id": "pn_1", "type": "chart"})
    assert layout.validate_free_block({"id": "pn_1", "type": "info", "rows": [{"value": "v"}]})
