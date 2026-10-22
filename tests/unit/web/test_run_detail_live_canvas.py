"""Unit tests for the full-width run console on the run-detail page.

Verifies:

* The run-detail page renders the console pane (``.terminal-pane`` with
  ``.console-fill``) filling the main area, carrying the run's
  ``data-instance-id`` so ``/static/live.js`` can find it and start the
  event reader. The scrolling ``<pre>`` terminal body and the live meta
  strip were removed — the reader now drives the status pill, the
  total-elapsed clock and the per-table summary/detail panel without
  rendering terminal lines.
* The ovid-style job-details header block (pipeline / folder / instance)
  and the status pill are present.
* The total-elapsed footer is present.
* The action toolbar (music-player-style Cancel/Rerun icon buttons)
  lives in the top summary block.
* The ``<noscript>`` fallback links the raw JSONL log so JS-off users
  still have a way to read the events.
* The base template loads the self-hosted D3 bundle before ``live.js``
  and marks ``live.js`` as an ES module.
* The display modules (bus / canvas / widgets / terminal) are still
  reachable at their declared static URLs — they still ship for the
  JSON API and the agent-driveable widget-contract seam even though
  the run page no longer mounts the metrics canvas.
* ``live.js`` boots the terminal reader and wires the ``job-status
  nudge`` so the hidden status shim (and the visible status pill it
  drives) update on terminal-state events within one round-trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")


class TestRunConsoleRendering:
    def test_run_detail_renders_console_pane(self, web_client) -> None:
        response = web_client.get("/runs/example-instance-id")
        assert response.status_code == 200
        html = response.text
        assert "terminal-pane" in html
        # The pane must carry the run's instance id so the reader
        # polls the correct events endpoint.
        assert 'data-instance-id="example-instance-id"' in html

    def test_run_detail_has_no_terminal_body(self, web_client) -> None:
        """The scrolling <pre> terminal body was removed."""
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert 'id="ovid2-terminal"' not in html

    def test_run_detail_console_fills_main(self, web_client) -> None:
        """The console page opts into the full-width fill layout."""
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert "console-page" in html
        assert "console-fill" in html, (
            "run-detail page must mark the terminal pane console-fill "
            "so the console fills the whole main content area."
        )

    def test_run_detail_has_summary_and_status_indicator(self, web_client) -> None:
        """The ovid-style header block is rendered, and the ``ovid2``
        wordmark carries the live run-status via ``data-status`` (the
        old ``term-status-pill`` was removed — the prompt label *is* the
        status indicator now)."""
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert "term-summary" in html
        assert "term-status-pill" not in html
        assert 'class="term-prompt-label"' in html
        assert "data-status=" in html

    def test_run_detail_has_total_elapsed_footer(self, web_client) -> None:
        """Total-elapsed footer present (the meta-strip timer is gone)."""
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert "term-footer" in html and "term-total" in html, (
            "missing total-elapsed footer"
        )
        assert "Total Elapsed Time" in html
        # The removed live meta strip must not linger.
        assert "term-header" not in html
        assert "term-current" not in html

    def test_run_detail_has_noscript_fallback(self, web_client) -> None:
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert "<noscript>" in html
        assert "term-noscript" in html
        assert "/api/v1/runs/example-instance-id/events.log" in html

    def test_base_template_loads_d3_and_module_live(self, web_client) -> None:
        response = web_client.get("/health")
        html = response.text
        assert "/static/vendor/d3.v7.min.js" in html
        # live.js must be an ES module so it can `import` the terminal
        # mount module.
        assert 'src="/static/live.js" type="module"' in html or (
            'type="module"' in html and 'src="/static/live.js"' in html
        )


class TestVendorBundleServed:
    def test_static_route_serves_d3(self, web_client) -> None:
        """The self-hosted D3 bundle is reachable at its declared URL."""
        response = web_client.get("/static/vendor/d3.v7.min.js")
        assert response.status_code == 200
        # Sanity — it's a real JS payload, not a 404 HTML page.
        body = response.text
        assert "d3" in body[:400].lower()
        # Bundle is expected to be at least ~50 KB — smaller means
        # something has gone wrong (a stub, an empty file, …).
        assert len(body) > 50_000, (
            f"D3 bundle looks truncated ({len(body)} bytes)"
        )

    def test_static_route_serves_display_bus(self, web_client) -> None:
        response = web_client.get("/static/display/bus.js")
        assert response.status_code == 200
        assert "createBus" in response.text

    def test_static_route_serves_canvas_module(self, web_client) -> None:
        response = web_client.get("/static/display/canvas.js")
        assert response.status_code == 200
        assert "mountCanvas" in response.text


class TestWidgetFilesShipped:
    """The six default widgets must be present on disk (live.js imports
    them by static URL, so a missing file surfaces as a 404 at boot)."""

    def test_six_widgets_present(self) -> None:
        root = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ovid2"
            / "web"
            / "static"
            / "display"
            / "widgets"
        )
        expected = {
            "pulse_bar.js",
            "executors_ring.js",
            "throughput.js",
            "stage_gantt.js",
            "elapsed_tile.js",
            "banner.js",
        }
        present = {p.name for p in root.glob("*.js")}
        missing = expected - present
        assert not missing, f"missing widget files: {sorted(missing)}"


class TestTerminalPaneRendering:
    """The console pane is rendered on the run-detail page and its
    reader / formatter modules are reachable at their declared static
    URLs. (The D3 line-buffer body was removed; the mount module still
    ships and still drives the status pill / clock / table panel.)"""

    def test_terminal_pane_container_present(self, web_client) -> None:
        response = web_client.get("/runs/example-instance-id")
        assert response.status_code == 200
        html = response.text
        assert "terminal-pane" in html, (
            "run-detail page must include the console pane"
        )
        # data-instance-id wired through so the reader hits the right
        # events endpoint.
        assert 'data-instance-id="example-instance-id"' in html

    def test_terminal_pane_action_toolbar_in_summary(self, web_client) -> None:
        """The transport control (the prompt-cursor play/stop button)
        lives in the top summary block, not in the split's right-pane
        head. (This example run has no backing job, so the interactive
        prompt may be absent — the placement contract is exercised
        end-to-end in test_run_page_rework.py.)"""
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert "term-summary" in html
        assert "term-split" in html
        # The old standalone toolbar is gone for good.
        assert "term-menu" not in html
        if "term-prompt-btn" in html:
            summary_part = html.split("term-split", 1)[0]
            assert "term-prompt-btn" in summary_part, (
                "prompt transport control must live in the top summary block"
            )

    def test_terminal_pane_noscript_links_raw_log(self, web_client) -> None:
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        assert "term-noscript" in html
        assert "/api/v1/runs/example-instance-id/events.log" in html

    def test_terminal_mount_module_served(self, web_client) -> None:
        response = web_client.get("/static/display/terminal/mount.js")
        assert response.status_code == 200
        text = response.text
        assert "mountTerminal" in text
        assert len(text) > 800

    def test_terminal_reader_module_served(self, web_client) -> None:
        response = web_client.get("/static/display/terminal/reader.js")
        assert response.status_code == 200
        text = response.text
        assert "createEventReader" in text
        assert len(text) > 400

    def test_terminal_formatter_module_served(self, web_client) -> None:
        response = web_client.get("/static/display/terminal/formatter.js")
        assert response.status_code == 200
        text = response.text
        assert "formatEvent" in text
        # ``engine.metrics`` must be suppressed by the formatter so
        # the terminal doesn't drown in samples that are already
        # visualised in the metrics canvas above.
        assert "engine.metrics" in text
        assert len(text) > 400


class TestLiveJsWiring:
    """live.js imports the terminal module and the job-status nudge glue."""

    def _live_js(self) -> str:
        p = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ovid2"
            / "web"
            / "static"
            / "live.js"
        )
        return p.read_text(encoding="utf-8")

    def test_live_js_imports_terminal_mount(self) -> None:
        text = self._live_js()
        assert "display/terminal/mount.js" in text, (
            "live.js must import the terminal mount module"
        )

    def test_live_js_wires_job_status_nudge(self) -> None:
        text = self._live_js()
        assert "ovid2:job-status-nudge" in text, (
            "live.js must listen for the terminal / bus job-status "
            "nudge so the /partials/runs/{id}/status swap fires on "
            "terminal-state events without waiting for the next poll."
        )
        assert "run-job-status" in text, (
            "the nudge handler must target #run-job-status"
        )

    def test_live_js_boots_terminal(self) -> None:
        text = self._live_js()
        assert "bootTerminal" in text, (
            "live.js must boot the terminal console"
        )


class TestDisplayTimezone:
    """The run console must hand the operator's display timezone to the
    browser so the live per-table detail times render in the configured
    zone (not raw UTC)."""

    def test_run_detail_pane_carries_display_tz(self, web_client) -> None:
        response = web_client.get("/runs/example-instance-id")
        assert response.status_code == 200
        html = response.text
        # Default settings have display_timezone == America/New_York.
        assert 'data-display-tz="America/New_York"' in html, (
            "the console pane must carry data-display-tz so panel.js can "
            "format event timestamps in the operator's timezone"
        )

    def test_tz_helper_module_served(self, web_client) -> None:
        response = web_client.get("/static/display/tz.js")
        assert response.status_code == 200
        text = response.text
        # The two exports panel.js / mount.js rely on.
        assert "formatClockTs" in text
        assert "readDisplayTz" in text
        assert "parseTsMs" in text

    def test_panel_module_uses_tz_helper(self) -> None:
        # The universal behaviour (tz formatting, follow, timer) lives in
        # the shared run panel; the tables wrapper delegates to it.
        p = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ovid2"
            / "web"
            / "static"
            / "display"
            / "run"
            / "panel.js"
        )
        text = p.read_text(encoding="utf-8")
        assert "display/tz.js" in text, (
            "panel.js must import the tz helper"
        )
        assert "formatClockTs" in text, (
            "panel.js must format detail-log timestamps via the tz helper"
        )

    def test_mount_module_backfills_start_anchor(self) -> None:
        """mount.js must back-fill the elapsed-clock anchors from event
        timestamps so the total-elapsed counter starts without a page
        refresh when the page is opened while the job is still queued."""
        p = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ovid2"
            / "web"
            / "static"
            / "display"
            / "terminal"
            / "mount.js"
        )
        text = p.read_text(encoding="utf-8")
        assert "backfillStartFromEvent" in text
        assert "backfillEndFromEvent" in text
        # The anchors must be mutable (let) for the back-fill to work.
        assert "let startedAtEpochMs" in text
        assert "let endedAtEpochMs" in text


class TestPerTableDetailFreeList:
    """The right pane is a free list of every table's detail, controlled
    (scrolled/highlighted) by the left list — not gated behind a click."""

    def test_detail_pane_placeholder_is_waiting(self, web_client) -> None:
        response = web_client.get("/runs/example-instance-id")
        html = response.text
        # The old "Select a table on the left…" gating copy is gone.
        assert "Select a table on the left" not in html
        assert "term-split-right" in html

    def test_panel_renders_all_table_sections(self) -> None:
        # The universal split-panel implementation lives in run/panel.js;
        # the tables wrapper is a thin config over it.
        p = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ovid2"
            / "web"
            / "static"
            / "display"
            / "run"
            / "panel.js"
        )
        text = p.read_text(encoding="utf-8")
        # A section per item + scroll-to-section controller behaviour.
        assert "run-detail-section" in text
        assert "scrollIntoView" in text
        # Durations derive from event ts, not only performance.now().
        assert "startedTsMs" in text
        assert "resolveElapsed" in text

    def test_panel_autoscroll_advances_and_rearms(self) -> None:
        """The live panel must keep advancing as jobs exceed the window.

        Regression guard for "the run page doesn't advance down when the
        jobs exceed the window": the summary list must deterministically
        scroll the *running frontier* into view (not rely on the browser's
        minimal ``scrollIntoView``), and the detail pane's follow must
        re-arm when the operator scrolls back to the bottom (so a single
        inspection click can't freeze the pane for the rest of the run).
        """
        p = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ovid2"
            / "web"
            / "static"
            / "display"
            / "run"
            / "panel.js"
        )
        text = p.read_text(encoding="utf-8")
        # Deterministic scroll helper (computes target scrollTop) rather
        # than only the minimal-move scrollIntoView.
        assert "scrollRowIntoView" in text
        assert "status-running" in text
        # Detail follow re-arms on scroll-to-bottom (userPinned reset).
        assert "userPinned = false" in text

