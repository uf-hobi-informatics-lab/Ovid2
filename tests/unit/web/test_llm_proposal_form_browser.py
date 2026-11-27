"""Browser test for the assistant's editable proposal forms (llm_proposal_form.js).

Loads the real ``help_tip.js`` + ``llm_proposal_form.js`` into headless
Chromium with stubbed catalogue GETs and checks:

- the model's arguments prefill the form; closed sets are ``<select>``s;
- Run stays disabled until every required field (incl. a secret) is filled;
- the request carries the secret, the redacted body never does;
- renaming a proposed connection rewrites the sibling address proposal.

Skipped when Playwright / Chromium is not installed (``OVID2_TEST_CHROMIUM``
points at a specific chromium / headless-shell binary).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_STATIC = Path(__file__).resolve().parents[3] / "src" / "ovid2" / "web" / "static"

_TYPES = [
    {
        "type": "disc",
        "args": [
            {"name": "host_path", "required": True},
            {"name": "mount_mode", "widget": "choices", "choices": ["rw", "ro"], "default": "rw"},
        ],
        "secrets": [],
    },
    {
        "type": "postgres",
        "args": [{"name": "host", "required": True}],
        "secrets": [{"name": "password", "required": True}],
    },
]

_STUB = """
window.__fetches = [];
window.fetch = function (url, opts) {
  window.__fetches.push([url, opts || {}]);
  var body = null;
  if (url === "/api/v1/connections/types")
    body = (%(types)s).map(function (x) { return {"type": x.type, "args": [], "secrets": []}; });
  else if (url === "/api/v1/connections") body = [{"name": "sf", "type": "snowflake"}];
  else if (url.indexOf("/api/v1/atlas/forms/connection/") === 0) {
    var t = url.split("/").pop();
    (%(types)s).forEach(function (x) { if (x.type === t) body = {"args": x.args, "secrets": x.secrets}; });
  }
  else if (url.indexOf("/api/v1/atlas/forms/location/") === 0)
    body = {"args": [{"name": "path"}, {"name": "format", "widget": "choices", "choices": ["parquet", "csv"]}]};
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(body); } });
};
"""


@pytest.fixture(scope="module")
def page():  # type: ignore[no-untyped-def]
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        exe = os.environ.get("OVID2_TEST_CHROMIUM")
        browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
    except Exception as exc:
        # Stop Playwright before skipping: a started-but-unstopped sync
        # Playwright leaves its asyncio loop running on this thread and
        # breaks every later test that calls ``asyncio.run``.
        pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    pg = browser.new_page()
    pg.set_content("<html><body><div id='host'></div></body></html>")
    pg.add_script_tag(content=_STUB % {"types": json.dumps(_TYPES)})
    pg.add_script_tag(content=(_STATIC / "help_tip.js").read_text(encoding="utf-8"))
    pg.add_script_tag(content=(_STATIC / "llm_proposal_form.js").read_text(encoding="utf-8"))
    yield pg
    browser.close()
    pw.stop()


_RENDER = """
(args) => {
  const host = document.getElementById('host');
  host.innerHTML = '';
  const m = { meta: { proposals: args.proposals } };
  window.__m = m; window.__runs = [];
  m.meta.proposals.forEach((p) => {
    const card = document.createElement('div');
    card.className = 'card-' + p.name;
    host.appendChild(card);
    Ovid2ProposalForm.render(card, p, { m, busy: () => false,
      onChange: () => {}, onDismiss: () => {}, onRun: (r) => window.__runs.push(r) });
  });
}
"""


def _settle(page) -> None:  # type: ignore[no-untyped-def]
    page.wait_for_timeout(50)


def test_disc_connection_prefill_and_run(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        _RENDER,
        {
            "proposals": [
                {
                    "name": "create_connection",
                    "method": "POST",
                    "path": "/api/v1/connections/inboxarchive_nu",
                    "body": {"type": "disc", "args": {"mount_mode": "ro"}},
                }
            ]
        },
    )
    _settle(page)
    card = page.locator(".card-create_connection")
    run = card.get_by_role("button", name="Run")
    assert run.is_disabled()  # host_path missing
    assert card.locator(".llm-missing").count() == 1
    assert card.locator("select").count() == 2  # type + mount_mode
    assert card.locator("select").nth(1).input_value() == "ro"
    name_input = card.locator("input[type=text]").first
    assert name_input.input_value() == "inboxarchive_nu"
    card.locator("input[type=text]").nth(1).fill("/data/inboxarchive/NU")
    assert run.is_enabled()
    run.click()
    runs = page.evaluate("window.__runs")
    assert runs == [
        {
            "path": "/api/v1/connections/inboxarchive_nu",
            "body": {
                "type": "disc",
                "args": {"host_path": "/data/inboxarchive/NU", "mount_mode": "ro"},
            },
            "method": "POST",
        }
    ]


def test_secret_is_sent_but_never_redacted_body(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        _RENDER,
        {
            "proposals": [
                {
                    "name": "create_connection",
                    "method": "POST",
                    "path": "/api/v1/connections/pg",
                    "body": {"type": "postgres", "args": {"host": "db.example.org"}},
                }
            ]
        },
    )
    _settle(page)
    card = page.locator(".card-create_connection")
    run = card.get_by_role("button", name="Run")
    assert run.is_disabled()
    card.locator("input[type=password]").fill("s3cret")
    assert run.is_enabled()
    run.click()
    runs = page.evaluate("window.__runs")
    assert runs[0]["body"]["secrets"] == {"password": "s3cret"}
    redacted = page.evaluate("Ovid2ProposalForm.redactedBody(window.__m.meta.proposals[0])")
    assert "s3cret" not in json.dumps(redacted)
    persisted = page.evaluate("JSON.stringify(window.__m.meta.proposals[0])")
    assert "s3cret" not in persisted


def test_rename_connection_updates_address_proposal(page) -> None:  # type: ignore[no-untyped-def]
    page.evaluate(
        _RENDER,
        {
            "proposals": [
                {
                    "name": "create_connection",
                    "method": "POST",
                    "path": "/api/v1/connections/inbox_a",
                    "body": {"type": "disc", "args": {"host_path": "/data/a"}},
                },
                {
                    "name": "create_address",
                    "method": "POST",
                    "path": "/api/v1/addresses/inbox_a_addr",
                    "body": {"connection": "inbox_a", "location": {}},
                },
            ]
        },
    )
    _settle(page)
    addr = page.locator(".card-create_address")
    # The proposed (not yet created) account is offered in the select.
    assert "inbox_a" in addr.locator("select").first.input_value()
    name = page.locator(".card-create_connection input[type=text]").first
    name.fill("inbox_b")
    name.dispatch_event("change")
    conn_of_addr = page.evaluate("window.__m.meta.proposals[1].form.connection")
    assert conn_of_addr == "inbox_b"
