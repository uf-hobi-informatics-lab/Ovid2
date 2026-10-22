/*
 * Ovid2 runs-workspace console bootstrap.
 *
 * The runs workspace (runs.html) is a master-detail layout: a scrollable
 * run-history list on the left and the live run console ("canvas") on the
 * right. This module boots that console and wires:
 *
 *   1. the "job-status nudge" — when the terminal reader observes a
 *      terminal-state event (instance.finished / batch.failed / …), it
 *      fires ``ovid2:job-status-nudge`` and we immediately re-fetch the
 *      ``/partials/runs/{id}/status`` shim so the hidden status contract
 *      (and the visible status pill it drives) update within one
 *      round-trip instead of within one HTMX poll interval;
 *
 *   2. left-list navigation — clicking a run row (``[data-run-nav]``)
 *      swaps ONLY the right detail region in place (no full reload) and
 *      moves the ``.is-selected`` marker, so the operator glides between
 *      runs while the history list stays put;
 *
 *   3. the ▶ Rerun smooth handoff (``ovid2:rerun-navigate``), which uses
 *      the same in-place detail swap.
 *
 * D3 is expected on window.d3 (self-hosted vendor bundle loaded from
 * /static/vendor/d3.v7.min.js earlier in <head>) — the terminal mount
 * uses it for its line buffer.
 *
 * Notes:
 *  - Loaded as a native ES module (type="module") so it can ``import``
 *    the terminal mount module. Older browsers without module support
 *    get the server-rendered <noscript> fallback and plain-navigation
 *    row links.
 *  - The live D3 metrics canvas + widget set is no longer mounted on
 *    the run console; the terminal console is the whole surface now. The
 *    canvas / widget modules still ship (other pages / the JSON API use
 *    them) and their contract seams stay intact.
 */

import { mountTerminal } from "/static/display/terminal/mount.js";
import { mountTablePanel } from "/static/display/tables/panel.js";
import { mountCheckPanel } from "/static/display/checks/panel.js";

// The live terminal mount handle for the run currently rendered in the
// detail region. Kept at module scope so a smooth in-place swap can tear
// the old reader down (``.close()``) before booting the new one.
let terminalHandle = null;

function bootTerminal() {
  const pane = document.querySelector(".terminal-pane");
  if (!pane) return null;
  // Mount the split panel first so we can hand its ``handleEvents`` to
  // the terminal as the shared event consumer — one poll feeds both the
  // console lines and the summary/detail split. Health runs render a
  // per-check split (#check-summary); everything else renders the
  // per-table split (#table-summary). We mount whichever is present.
  const panel = document.getElementById("check-summary")
    ? mountCheckPanel({})
    : mountTablePanel({});
  terminalHandle = mountTerminal(pane, {
    d3: window.d3,
    onEvents: panel ? panel.handleEvents : null,
  });
  return terminalHandle;
}

/**
 * Broadcast a job-status nudge and, when HTMX is available, trigger
 * an immediate refresh of the ``#run-job-status`` container.
 */
function nudgeJobStatus(reason) {
  try {
    window.dispatchEvent(
      new CustomEvent("ovid2:job-status-nudge", { detail: { reason } }),
    );
  } catch (e) {
    /* ignore */
  }
}

/**
 * Listen for status-nudge events (fired by the terminal reader on
 * ``instance.finished`` / ``batch.*``, or by the metrics bus on a
 * terminal state change) and ask HTMX to re-fetch the status
 * partial right away.
 *
 * We prefer HTMX's imperative API so the refresh goes through the
 * same code path as the periodic poll (identical swap semantics,
 * identical error handling). Falls back to a manual ``fetch`` +
 * innerHTML swap when HTMX isn't loaded on the page.
 */
function wireJobStatusNudge() {
  window.addEventListener("ovid2:job-status-nudge", () => {
    const el = document.getElementById("run-job-status");
    if (!el) return;
    // The container drops its ``hx-get`` attr once the job reaches a
    // terminal state (see _run_job_status.html); we re-derive the
    // URL from the current path so a nudge still refreshes the
    // badge in that case.
    let url = el.getAttribute("hx-get");
    if (!url) {
      // /runs/<id> or /runs/<batch_id> -> we use ``instance_id`` from
      // the terminal pane when it's available, otherwise the URL's
      // last segment.
      const pane = document.querySelector(".terminal-pane");
      const instanceId =
        (pane && pane.dataset.instanceId) ||
        window.location.pathname.split("/").filter(Boolean).pop();
      if (!instanceId) return;
      url = "/partials/runs/" + encodeURIComponent(instanceId) + "/status";
    }
    if (window.htmx && typeof window.htmx.ajax === "function") {
      try {
        window.htmx.ajax("GET", url, {
          target: "#run-job-status",
          swap: "innerHTML",
        });
        return;
      } catch (e) {
        /* fall through to plain fetch */
      }
    }
    fetch(url, { credentials: "same-origin" })
      .then((r) => (r.ok ? r.text() : ""))
      .then((html) => {
        if (html) {
          el.innerHTML = html;
          syncVisibleStatus();
        }
      })
      .catch(() => {
        /* ignore */
      });
  });
}

/**
 * Update the ``.is-selected`` marker in the run-history list so the
 * active row matches the run currently shown in the detail region.
 * ``runId`` is the row's ``data-run-id`` (instance_id or batch_id).
 */
function markSelected(runId) {
  if (!runId) return;
  document.querySelectorAll(".run-list-item.is-selected").forEach((el) => {
    el.classList.remove("is-selected");
  });
  const link = document.querySelector(
    '.run-list-item .run-list-link[data-run-id="' + cssEscape(runId) + '"]',
  );
  if (link) {
    const item = link.closest(".run-list-item");
    if (item) item.classList.add("is-selected");
  }
}

/** Minimal attribute-selector escaping (CSS.escape shim). */
function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === "function") {
    return window.CSS.escape(value);
  }
  return String(value).replace(/["\\]/g, "\\$&");
}

/**
 * Swap the right-hand detail region with a freshly-fetched run console
 * and re-boot the terminal, without a full-document reload. Used by the
 * left-list row clicks and the ▶ Rerun transition so the operator glides
 * between runs instead of getting a white-flash navigation. Returns a
 * promise that resolves when the swap is done.
 *
 * We fetch the target URL with ``X-Requested-With: fetch`` so the server
 * returns ONLY the run-console fragment (run_detail.html), extract its
 * ``#run-console`` node, and replace the current one inside
 * ``#run-detail-region``. HTMX's swap would not re-run our ES-module
 * boot, so we do the DOM replace ourselves and then call
 * :func:`bootTerminal` explicitly. When HTMX is present we still let it
 * process the swapped subtree so any ``hx-*`` attributes inside it (the
 * job-status poll shim) re-arm.
 */
function navigateRun(url, opts) {
  const push = !opts || opts.push !== false;
  const runId = opts && opts.runId;
  return fetch(url, {
    credentials: "same-origin",
    headers: { "X-Requested-With": "fetch" },
  })
    .then((r) => (r.ok ? r.text() : Promise.reject(new Error("nav:" + r.status))))
    .then((html) => {
      const doc = new DOMParser().parseFromString(html, "text/html");
      const fresh = doc.querySelector("#run-console");
      const region = document.getElementById("run-detail-region");
      if (!fresh || !region) {
        // Could not locate the console fragment / region — fall back to
        // a real navigation so the operator still lands on the run.
        window.location.assign(url);
        return;
      }
      // Tear down the outgoing run's reader so its poll timer stops.
      if (terminalHandle && typeof terminalHandle.close === "function") {
        try {
          terminalHandle.close();
        } catch (_e) {
          /* ignore */
        }
      }
      terminalHandle = null;
      region.replaceChildren(fresh);
      if (doc.title) document.title = doc.title;
      markSelected(runId || fresh.getAttribute("data-instance-id"));
      if (push) {
        try {
          window.history.pushState({ ovid2Run: url }, "", url);
        } catch (_e) {
          /* history unavailable — the content still swapped */
        }
      }
      // Let HTMX arm any hx-* attributes inside the swapped subtree
      // (the #run-job-status poll shim) before we boot the terminal.
      if (window.htmx && typeof window.htmx.process === "function") {
        try {
          window.htmx.process(fresh);
        } catch (_e) {
          /* ignore */
        }
      }
      bootTerminal();
      // A ▶ launch / rerun lands here: reflect the new server status on
      // the visible label and the left-list row straight away.
      syncVisibleStatus();
      refreshListBadges();
    })
    .catch(() => {
      // Any failure -> plain navigation so the click is never a dead end.
      window.location.assign(url);
    });
}

/**
 * Wire left-list row navigation. Clicking a run row swaps the detail
 * region in place instead of reloading the whole page. Falls back to the
 * anchor's ordinary navigation when JS-modified clicks (new tab, etc.)
 * are detected, and does nothing without a resolvable target.
 */
function wireListNavigation() {
  document.addEventListener("click", (ev) => {
    const link =
      ev.target && typeof ev.target.closest === "function"
        ? ev.target.closest("[data-run-nav]")
        : null;
    if (!link) return;
    // Respect modifier clicks (open in new tab/window) and non-primary
    // buttons — let the browser handle those as real navigations.
    if (
      ev.defaultPrevented ||
      ev.button !== 0 ||
      ev.metaKey ||
      ev.ctrlKey ||
      ev.shiftKey ||
      ev.altKey
    ) {
      return;
    }
    // Only intercept when the workspace detail region is present.
    if (!document.getElementById("run-detail-region")) return;
    const url = link.getAttribute("href");
    if (!url) return;
    ev.preventDefault();
    navigateRun(url, { push: true, runId: link.getAttribute("data-run-id") });
  });
}

/**
 * Wire the ▶ Rerun smooth-navigation handoff. The terminal mount posts
 * the rerun in the background and, on success, dispatches
 * ``ovid2:rerun-navigate`` with the new run's URL. We claim the event
 * (``preventDefault`` tells the mount not to hard-navigate) and swap
 * the new run page into ``#main``. A ``popstate`` listener makes the
 * browser back/forward buttons re-swap instead of reloading.
 */
function wireRerunNavigate() {
  window.addEventListener("ovid2:rerun-navigate", (ev) => {
    const url = ev && ev.detail && ev.detail.url;
    if (!url) return;
    // Claim the navigation so the terminal mount does not hard-navigate.
    if (typeof ev.preventDefault === "function") ev.preventDefault();
    navigateRun(url, { push: true });
  });
  // Back/forward between runs we swapped in: re-swap in place. Only
  // handle paths under /runs/ so we don't hijack navigation away from
  // the workspace (a real reload is correct when leaving it).
  window.addEventListener("popstate", () => {
    const path = window.location.pathname || "";
    if (!path.startsWith("/runs/")) return;
    // No detail region means we already left the workspace DOM; let the
    // browser handle it.
    if (!document.getElementById("run-detail-region")) return;
    const runId = path.split("/").filter(Boolean).pop();
    navigateRun(window.location.pathname + window.location.search, {
      push: false,
      runId,
    });
  });
}

// ---- visible status sync ------------------------------------------
//
// The ``#run-job-status`` poll swaps into a *hidden* shim, and the
// console's "ovid2" label used to change only on ``instance.started`` /
// ``instance.finished`` events. A launched draft therefore looked like a
// draft for as long as it sat queued behind another run. After every
// status swap we read the hidden ``.job-status-marker`` and push its
// status onto the visible label + queue note. We only ever move the
// label *forward* (draft -> queued -> running -> terminal) so we never
// fight the event-driven terminal state set by mount.js.

const STATUS_RANK = { draft: 0, unknown: 0, queued: 1, running: 2, stopping: 3,
                      done: 4, failed: 4, cancelled: 4 };

function queueNoteText(running, ahead) {
  const parts = [];
  if (running > 0) parts.push(running + " running job" + (running === 1 ? "" : "s"));
  if (ahead > 0) parts.push(ahead + " queued ahead");
  return parts.length ? "queued — waiting for " + parts.join(" + ") : "queued — starting…";
}

function syncVisibleStatus() {
  const shim = document.getElementById("run-job-status");
  const marker = shim ? shim.querySelector(".job-status-marker") : null;
  if (!marker) return;
  const status = (marker.getAttribute("data-job-status") || "").toLowerCase();
  const pane = document.querySelector(".terminal-pane");
  if (!pane || !status) return;
  const label = pane.querySelector(".term-prompt-label");
  if (label) {
    const cur = (label.dataset.status || "unknown").toLowerCase();
    if ((STATUS_RANK[status] ?? 0) > (STATUS_RANK[cur] ?? 0)) {
      label.dataset.status = status;
    }
  }
  pane.setAttribute("data-status", status);
  const note = pane.querySelector(".term-queue-note");
  if (note) {
    if (status === "queued") {
      const r = parseInt(marker.getAttribute("data-queue-running") || "0", 10) || 0;
      const a = parseInt(marker.getAttribute("data-queue-ahead") || "0", 10) || 0;
      note.textContent = queueNoteText(r, a);
      note.hidden = false;
    } else {
      note.hidden = true;
    }
  }
  // Keep the matching left-list row in step with the detail pane.
  refreshListBadges();
}

function wireStatusSync() {
  document.addEventListener("htmx:afterSwap", (ev) => {
    const t = ev && ev.detail && ev.detail.target;
    if (t && t.id === "run-job-status") syncVisibleStatus();
  });
}

// ---- left-list badge refresh ---------------------------------------
//
// The Jobs list is rendered once server-side; nothing re-fetched existing
// rows, so a launched draft kept its DRAFT badge until a full reload.
// Every few seconds we ask /partials/runs/list-status for the rows that
// are still non-terminal and patch their badge in place. No request is
// made when every visible row is already terminal.

const LIST_PENDING = { DRAFT: 1, NEW: 1, PROCESSING: 1 };
const LIST_POLL_MS = 4000;
let listInFlight = false;

function pendingListRows() {
  const out = {};
  document.querySelectorAll("#run-list-items .run-list-link[data-instance-id]").forEach((a) => {
    const badge = a.querySelector(".run-list-sub .badge");
    const txt = badge ? (badge.textContent || "").trim() : "";
    if (LIST_PENDING[txt] === 1) out[a.getAttribute("data-instance-id")] = a;
  });
  return out;
}

function applyListBadge(link, status) {
  const badge = link.querySelector(".run-list-sub .badge");
  if (!badge || (badge.textContent || "").trim() === status) return;
  const cls = "status-" + status.toLowerCase();
  badge.className = "badge " + cls;
  badge.textContent = status;
  const item = link.closest(".run-list-item");
  if (item) {
    Array.from(item.classList)
      .filter((c) => c.indexOf("status-") === 0)
      .forEach((c) => item.classList.remove(c));
    item.classList.add(cls);
  }
}

function refreshListBadges() {
  if (listInFlight) return;
  const rows = pendingListRows();
  const ids = Object.keys(rows);
  if (!ids.length) return;
  listInFlight = true;
  fetch("/partials/runs/list-status?ids=" + encodeURIComponent(ids.join(",")), {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then((r) => (r.ok ? r.json() : {}))
    .then((map) => {
      Object.keys(map || {}).forEach((iid) => {
        if (rows[iid]) applyListBadge(rows[iid], String(map[iid]));
      });
    })
    .catch(() => {
      /* ignore — next tick retries */
    })
    .then(() => {
      listInFlight = false;
    });
}

function wireListBadgeRefresh() {
  if (!document.getElementById("run-list-items")) return;
  window.setInterval(() => {
    if (document.visibilityState === "hidden") return;
    refreshListBadges();
  }, LIST_POLL_MS);
}

function boot() {
  wireStatusSync();
  wireListBadgeRefresh();
  wireJobStatusNudge();
  wireRerunNavigate();
  wireListNavigation();
  bootTerminal();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
