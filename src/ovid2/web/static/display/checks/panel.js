/*
 * Ovid2 per-check panel — thin config wrapper over the shared run panel
 * (see display/run/panel.js). Kept as its own module + export
 * (``mountCheckPanel``) for back-compat with live.js; all behaviour
 * (status dots, follow-run toggle, detail list, live MM:SS.s timer,
 * roster pre-seed) lives in the shared panel so the health/dq_checks
 * console renders identically to the ETL console.
 *
 * Checks render as a *flat* roster (no CDC-part grouping) — one row per
 * check, exactly like the ETL table console. The roster is pre-seeded
 * from the run's selection (``data-check-parts`` / ``data-check-packages``
 * / ``data-check-checks`` on the console pane) resolved against the
 * check catalogue (``GET /api/v1/health/checks`` +
 * ``GET /api/v1/health/packages``) so every check the run will touch
 * appears immediately as ``queued`` before any event arrives. Each row
 * shows a colour-coded status dot (running / passed / exception / error)
 * — the same dot vocabulary as the table console.
 *
 * The right pane shows each check's detail: a live/frozen MM:SS.s clock
 * (the check's wall-clock duration, from the server-measured
 * ``elapsed_ms`` on ``check.completed`` / ``check.failed``, or timed
 * client-side from ``check.started``), plus a
 * ``N metric(s), N exception(s)`` summary once complete.
 *
 * Event shapes consumed (see ovid2.core.events):
 *   check.started    { check_id, check_name, part }
 *   check.completed  { check_id, check_name, part, check_status,
 *                      exception_count, metric_count, elapsed_ms }
 *   check.failed     { check_id, check_name, part, error, elapsed_ms }
 */

import { mountRunPanel, escapeHtml } from "/static/display/run/panel.js";

/**
 * Mount the per-check panel.
 *
 * @param {object} [opts]
 * @param {HTMLElement} [opts.summaryEl] - defaults to #check-summary.
 * @param {HTMLElement} [opts.detailEl]  - defaults to #check-detail.
 * @param {HTMLElement} [opts.detailNameEl] - defaults to
 *   #check-detail-name.
 * @returns {{ handleEvents:(events:Array<object>)=>void, close:()=>void }|null}
 */
export function mountCheckPanel(opts) {
  const o = opts || {};
  const summaryEl = o.summaryEl || document.getElementById("check-summary");
  const detailEl = o.detailEl || document.getElementById("check-detail");
  const detailNameEl =
    o.detailNameEl || document.getElementById("check-detail-name");
  if (!summaryEl || !detailEl) return null;
  const pane = summaryEl.closest(".terminal-pane");

  return mountRunPanel({
    summaryEl,
    detailEl,
    detailNameEl,
    paneEl: pane,
    tz: o.tz,
    dom: { followToggle: "check-follow-toggle" },
    rowClass: "check-row",
    dotClass: "check-row-dot",
    // Checks now carry a wall-clock duration (elapsed_ms), so the shared
    // live timer is enabled — the same MM:SS.s clock as the ETL console.
    timed: true,
    statusLabel: (s) => s || "",
    // Only ``check.*`` events belong to this panel; they key off
    // ``check_id`` (with the human name preferred for display).
    itemKey: (ev) => {
      if (!ev || typeof ev.kind !== "string") return undefined;
      if (ev.kind.indexOf("check.") !== 0) return undefined;
      return ev.check_id || (ev.extra && ev.extra.check_id);
    },
    // Flat roster, pre-seeded from the run's check selection.
    roster: () => resolveCheckRoster(pane),
    // Fold check events into the shared aggregate. We stash the human
    // name + metric/exception counts on the record for the detail body,
    // and let the shared panel drive the timer from started/elapsed_ms.
    apply: (ev, rec) => {
      const kind = ev.kind || "";
      const name = ev.check_name || (ev.extra && ev.extra.check_name);
      if (name) rec.displayName = name;
      if (kind === "check.started") {
        rec.status = "running";
        rec.startedMs = performance.now();
        rec.startedTsMs = parseTsMs(ev.ts);
      } else if (kind === "check.completed") {
        rec.status = ev.check_status || "passed";
        const exc = num(ev.exception_count);
        const met = num(ev.metric_count);
        if (exc != null) rec.exc = exc;
        if (met != null) rec.metrics = met;
        const el = resolveElapsed(rec, ev);
        if (el != null) rec.elapsedMs = el;
        rec.startedMs = null;
      } else if (kind === "check.failed") {
        rec.status = "error";
        rec.error = String(ev.error || (ev.extra && ev.extra.error) || "");
        const el = resolveElapsed(rec, ev);
        if (el != null) rec.elapsedMs = el;
        rec.startedMs = null;
      }
    },
    // Right-pane detail body for a check. The section header (rendered by
    // the shared panel) already carries the check name, its clock, and a
    // status dot; the body adds the outcome summary line.
    detailBody: (rec) => {
      if (rec.status === "error") {
        return (
          '<p class="small check-detail-error">' +
          escapeHtml(rec.error || "") +
          "</p>"
        );
      }
      if (rec.status === "running") {
        return '<p class="small mono">running…</p>';
      }
      return (
        '<p class="small mono">' +
        (rec.metrics || 0) +
        " metric(s), " +
        (rec.exc || 0) +
        " exception(s)</p>"
      );
    },
  });
}

/**
 * Resolve the pre-seed roster of check ids for the console pane.
 *
 * Reads the run's selection (``data-check-checks`` / ``-packages`` /
 * ``-parts``) and reproduces the server-side ``select_specs`` precedence
 * (explicit checks → union of packages → parts → whole catalogue) using
 * the web-safe catalogue endpoints. Returns roster records
 * ``{ name, displayName }`` keyed on ``check_id`` (what ``itemKey``
 * emits), so the roster and the live events collapse onto the same rows.
 *
 * @param {HTMLElement|null} pane
 * @returns {Promise<Array<{name:string, displayName:string}>>}
 */
function resolveCheckRoster(pane) {
  const ds = pane && pane.dataset ? pane.dataset : {};
  const checks = parseList(ds.checkChecks);
  const packages = parseList(ds.checkPackages);
  const parts = parseList(ds.checkParts);

  return Promise.all([fetchChecks(), fetchPackages()])
    .then(([catalogue, pkgs]) => {
      const enabled = catalogue.filter((c) => c.enabled !== false);
      const byId = new Map();
      for (const c of enabled) {
        if (c.id) byId.set(c.id, c);
        // Explicit ``--checks`` may name a check by its machine name too.
        if (c.name) byId.set(c.name, c);
      }
      let selected;
      if (checks && checks.length) {
        // Explicit ids/names win (precedence rule 1).
        selected = dedupe(checks.map((k) => byId.get(k)).filter(Boolean));
      } else if (packages && packages.length) {
        // Union of the selected packages' member checks (rule 2).
        const want = new Set();
        for (const p of pkgs) {
          if (packages.indexOf(p.id) === -1) continue;
          for (const m of p.checks || []) want.add(m);
        }
        selected = enabled.filter((c) => want.has(c.id) || want.has(c.name));
      } else if (parts && parts.length) {
        // Report-bucket parts (rule 3).
        const want = new Set(parts);
        selected = enabled.filter((c) => want.has(c.part));
      } else {
        // No selection ⇒ the whole enabled catalogue.
        selected = enabled;
      }
      return selected.map((c) => ({
        name: c.id,
        displayName: c.name || c.id,
      }));
    })
    .catch(() => []); // endpoint unavailable / forbidden — stay reactive
}

function fetchChecks() {
  return getJson("/api/v1/health/checks");
}

function fetchPackages() {
  return getJson("/api/v1/health/packages");
}

function getJson(url) {
  return fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then((r) => {
      if (!r.ok) throw new Error(url + " " + r.status);
      return r.json();
    })
    .then((rows) => (Array.isArray(rows) ? rows : []))
    .catch(() => []);
}

function parseList(raw) {
  if (!raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return null;
  }
  if (!Array.isArray(parsed) || !parsed.length) return null;
  const out = parsed.map((v) => String(v || "")).filter(Boolean);
  return out.length ? out : null;
}

function dedupe(list) {
  const seen = new Set();
  const out = [];
  for (const c of list) {
    if (!c || seen.has(c.id)) continue;
    seen.add(c.id);
    out.push(c);
  }
  return out;
}

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// Local copies of the timing helpers the shared panel uses internally
// (not exported). ``parseTsMs`` mirrors tz.js; ``resolveElapsed`` mirrors
// run/panel.js so the checks folder can freeze the clock at completion.
function parseTsMs(ts) {
  if (!ts) return null;
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? ms : null;
}

function resolveElapsed(rec, ev) {
  const server = Number(
    (ev && ev.elapsed_ms) != null
      ? ev.elapsed_ms
      : ev && ev.extra && ev.extra.elapsed_ms,
  );
  if (Number.isFinite(server) && server >= 0) return server;
  const endTsMs = parseTsMs(ev && ev.ts);
  if (rec.startedTsMs != null && endTsMs != null && endTsMs >= rec.startedTsMs) {
    return endTsMs - rec.startedTsMs;
  }
  if (rec.startedMs != null) return performance.now() - rec.startedMs;
  return null;
}

export default { id: "check-panel", mount: mountCheckPanel };
