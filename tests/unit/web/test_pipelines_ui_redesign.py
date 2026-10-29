"""Pipelines UI redesign tests: card grid, stepper wizard, "?" help tips.

Locks the 2026-10 refresh of ``/pipelines`` (stat strip + card grid),
``/pipelines/new`` (five-step wizard with kind tiles driving a hidden,
still-posted ``<select name="kind">``), the reusable ``_help.html`` "?"
tooltip, and the removal of the spaceship theme's numbered nav items.
"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static"


def _write_disc_connection(xdg: Path, name: str, host_path: str) -> None:
    conndir = xdg / "config" / "ovid2" / "connections"
    conndir.mkdir(parents=True, exist_ok=True)
    (conndir / f"{name}.json").write_text(
        json.dumps(
            {
                "type": "disc",
                "args": {"host_path": host_path, "container_path": "/data", "mount_mode": "rw"},
            }
        )
    )


# --------------------------------------------------------------------------- #
# "?" help tips
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_help_tip_is_accessible_pair(web_client: object) -> None:
    """Every rendered tip pairs a button with a role=tooltip bubble by id."""
    body = web_client.get("/pipelines/new").text  # type: ignore[attr-defined]
    buttons = re.findall(r'class="help-tip-btn"[^>]*aria-describedby="([^"]+)"', body)
    assert len(buttons) >= 6
    for tip_id in buttons:
        assert f'role="tooltip" id="{tip_id}"' in body
    assert "/static/help_tip.js" in body


@pytest.mark.unit
def test_builder_descriptions_moved_into_tips(web_client: object) -> None:
    """The bulky ``note small`` paragraphs are gone from the builder."""
    body = web_client.get("/pipelines/new").text  # type: ignore[attr-defined]
    assert '<p class="note small">' not in body
    # The guidance text survives — inside the tooltip bubbles.
    assert "immediately runnable" in body
    assert "help-tip-bubble" in body


@pytest.mark.unit
def test_help_tip_assets_served(web_client: object) -> None:
    r = web_client.get("/static/help_tip.js")  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert "help-tip" in r.text
    css = (_STATIC / "ovid2.css").read_text(encoding="utf-8")
    assert ".help-tip:focus-within .help-tip-bubble" in css


@pytest.mark.unit
def test_dashboard_and_scaffold_use_help_tips(web_client: object) -> None:
    for url in ("/", "/pipeline-packages/new"):
        body = web_client.get(url).text  # type: ignore[attr-defined]
        assert 'class="help-tip-btn"' in body, url
    scaffold = web_client.get("/pipeline-packages/new").text  # type: ignore[attr-defined]
    assert '<p class="note">' not in scaffold


# --------------------------------------------------------------------------- #
# spaceship nav numbering removed
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_spaceship_nav_items_are_not_numbered() -> None:
    css = (_STATIC / "ovid2.css").read_text(encoding="utf-8")
    assert "counter(sx-nav" not in css
    assert "counter-increment: sx-nav" not in css


# --------------------------------------------------------------------------- #
# pipelines card grid
# --------------------------------------------------------------------------- #


@pytest.fixture
def temp_pipelines_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Scaffold into a throwaway dir that is on the ``ovid2_pipelines`` path."""
    root = tmp_path / "pipelines_out"
    root.mkdir()
    monkeypatch.setattr("ovid2.tools.new_pipeline._pipelines_root", lambda: root)
    pkg = importlib.import_module("ovid2_pipelines")
    original = list(pkg.__path__)
    pkg.__path__.append(str(root))
    yield root
    pkg.__path__[:] = original


@pytest.fixture
def one_pipeline(
    web_client: object, isolated_xdg: Path, tmp_path: Path, temp_pipelines_root: Path
) -> str:
    root = tmp_path / "disc_cards"
    root.mkdir()
    _write_disc_connection(isolated_xdg, "cards", str(root))
    r = web_client.post(  # type: ignore[attr-defined]
        "/pipelines",
        data={
            "name": "cardme",
            "kind": "etl",
            "template": "pcornet_etl",
            "input_open": "1",
            "output_connection": "cards",
            "output_location": "out",
            "trigger_type": "manual",
            "enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    return "cardme"


@pytest.mark.unit
def test_pipelines_page_renders_cards_and_stats(web_client: object, one_pipeline: str) -> None:
    body = web_client.get("/pipelines").text  # type: ignore[attr-defined]
    assert 'class="pl-stats"' in body
    assert 'data-kind-tile="etl"' in body
    card = re.search(r'<article class="pl-card pipeline-row".*?</article>', body, re.DOTALL)
    assert card is not None
    html = card.group(0)
    assert 'data-name="cardme"' in html
    assert 'class="pl-flow"' in html
    # open input renders as a dashed "open" node
    assert "pl-node is-open" in html
    assert 'data-run-pipeline="cardme"' in html
    # the old table and intro paragraph are gone
    assert "<th>Next fire</th>" not in body
    assert "persisted, named job template" in body  # moved into the "?" tip
    assert '<p class="note">\n    A <strong>pipeline</strong>' not in body


@pytest.mark.unit
def test_pipelines_empty_state_has_cta(web_client: object, isolated_xdg: Path) -> None:
    body = web_client.get("/pipelines").text  # type: ignore[attr-defined]
    assert 'class="pl-empty"' in body
    assert "Create your first pipeline" in body


# --------------------------------------------------------------------------- #
# new-pipeline stepper wizard
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_new_pipeline_wizard_structure(web_client: object) -> None:
    body = web_client.get("/pipelines/new").text  # type: ignore[attr-defined]
    steps = re.findall(r'class="wz-step builder-card" id="wz-step-(\w+)"', body)
    assert steps == ["type", "source", "engine", "destination", "automation"]
    for key in steps:
        assert f'data-rail="{key}"' in body
    # kind tiles drive the still-posted native select
    for kind in ("etl", "analysis", "dq_checks", "copy", "move"):
        assert f'data-kind-tile="{kind}"' in body
    assert '<select id="kind" name="kind"' in body
    assert "data-wz-next" in body and "data-wz-prev" in body
    # the canvas checklist + save chip survive
    assert 'id="builder-req-list"' in body
    assert 'id="builder-save"' in body


@pytest.mark.unit
def test_wizard_js_tags_requirements_by_step() -> None:
    js = (_STATIC / "pipeline_builder.js").read_text(encoding="utf-8")
    for step in ("type", "source", "engine", "destination", "automation"):
        assert f'add("{step}"' in js
    assert "setupWizard" in js
    assert 'classList.add("wz-js")' in js
