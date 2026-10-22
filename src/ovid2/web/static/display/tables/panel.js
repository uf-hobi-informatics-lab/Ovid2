/*
 * Ovid2 per-table panel — thin config wrapper over the shared run panel
 * (see display/run/panel.js). Kept as its own module + export
 * (``mountTablePanel``) for back-compat with live.js and the run-page
 * tests; all behaviour (status dots, follow-run toggle, live MM:SS.s
 * timer, roster pre-seeding, detail event log) lives in the shared panel
 * so every pipeline type renders identically.
 *
 * Tables are a *flat* roster (no grouping). The roster is pre-seeded
 * from the pane's ``data-planned-tables`` JSON array (an explicit subset,
 * e.g. a copy/move job) or, when that is the ``["all"]`` sentinel /
 * absent, from ``GET /api/v1/pipeline-packages/{pipeline}/plan``.
 *
 * Event shapes consumed (see ovid2.core.events):
 *   table.started / table.completed / table.failed / stage.* /
 *   fix.applied / fix.skipped / fix.failed / error — anything carrying a
 *   ``table`` field is attached to that table's detail log.
 */

import { mountRunPanel, fetchPlanTables } from "/static/display/run/panel.js";

//: Human-facing label per internal status key (drives dot aria-label).
const STATUS_LABEL = {
  pending: "queued",
  running: "running",
  completed: "done",
  failed: "failed",
  cancelled: "cancelled",
};

/**
 * Mount the per-table panel.
 *
 * @param {object} [opts]
 * @param {HTMLElement} [opts.summaryEl] - defaults to #table-summary.
 * @param {HTMLElement} [opts.detailEl]  - defaults to #table-detail.
 * @param {HTMLElement} [opts.detailNameEl] - defaults to
 *   #table-detail-name.
 * @param {string} [opts.tz] - IANA display timezone.
 * @returns {{ handleEvents:(events:Array<object>)=>void, close:()=>void }|null}
 */
export function mountTablePanel(opts) {
  const o = opts || {};
  const summaryEl = o.summaryEl || document.getElementById("table-summary");
  const detailEl = o.detailEl || document.getElementById("table-detail");
  const detailNameEl =
    o.detailNameEl || document.getElementById("table-detail-name");
  if (!summaryEl || !detailEl) return null;
  const pane = summaryEl.closest(".terminal-pane");

  return mountRunPanel({
    summaryEl,
    detailEl,
    detailNameEl,
    paneEl: pane,
    tz: o.tz,
    dom: { followToggle: "table-follow-toggle" },
    rowClass: "table-row",
    dotClass: "table-row-dot",
    timed: true,
    statusLabel: (s) => STATUS_LABEL[s] || s || "",
    // Tables key off any event carrying a ``table`` field.
    itemKey: (ev) => ev && (ev.table || (ev.extra && ev.extra.table)),
    // Flat roster: no grouping.
    roster: () => resolveRoster(pane),
  });
}

/**
 * Resolve the table roster for pre-seeding. An explicit
 * ``data-planned-tables`` subset wins (no network); otherwise fall back
 * to the pipeline's ``--tables all`` plan endpoint.
 *
 * @param {HTMLElement|null} pane
 * @returns {string[]|Promise<string[]>}
 */
function resolveRoster(pane) {
  const explicit = parsePlannedTables(pane);
  if (explicit && explicit.length) return explicit;
  const pipeline = pane && pane.dataset ? pane.dataset.pipeline : "";
  if (!pipeline) return [];
  return fetchPlanTables(pipeline);
}

/**
 * Parse the pane's ``data-planned-tables`` JSON array. Returns null when
 * absent / the ``["all"]`` sentinel / malformed (roster then resolves via
 * the plan endpoint).
 *
 * @param {HTMLElement|null} pane
 * @returns {string[]|null}
 */
function parsePlannedTables(pane) {
  const raw = pane && pane.dataset ? pane.dataset.plannedTables : "";
  if (!raw) return null;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    return null;
  }
  if (!Array.isArray(parsed) || !parsed.length) return null;
  const names = parsed.map((t) => String(t || "")).filter(Boolean);
  if (!names.length) return null;
  if (names.length === 1 && names[0].toLowerCase() === "all") return null;
  return names;
}

export default { id: "table-panel", mount: mountTablePanel };
