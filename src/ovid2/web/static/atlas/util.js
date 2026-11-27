/*
 * Ovid Atlas — shared helpers (HTTP, escaping, time, CSRF).
 */

export const esc = (s) => String(s == null ? "" : s).replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[c]);
export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
export const enc = encodeURIComponent;
export const tip = (text) => (window.Ovid2HelpTip ? window.Ovid2HelpTip.html(text) : "");

/* JSON call to /api/v1 (the API's CSRF defence is the JSON content type). */
export async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch("/api/v1" + path, opts);
  let data = null;
  try { data = r.status === 204 ? {} : await r.json(); } catch (_e) { data = null; }
  if (!r.ok || r.status === 202) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.status === "awaiting_approval"
      ? "Awaiting human approval (" + d.approval_id + ") — approve it on the Approvals page, then retry."
      : d && d.error === "overwrite_required" ? "overwrite_required"
      : JSON.stringify(d || data || r.statusText);
    const err = new Error(msg);
    err.status = r.status;
    err.detail = d;
    throw err;
  }
  return data;
}

/* Server-synced clock: the page carries the server's "now" at render. */
let skew = 0;
export function setServerNow(ms) {
  const n = Number(ms);
  if (Number.isFinite(n) && n > 0) skew = Date.now() - n;
}
export const serverNow = () => Date.now() - skew;

export function parseTs(ts) {
  if (!ts) return null;
  const n = Date.parse(String(ts));
  return Number.isFinite(n) ? n : null;
}

export function fmtClock(ms) {
  const total = Math.max(0, Math.floor(ms || 0));
  const s = Math.floor(total / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  const tenths = Math.floor((total % 1000) / 100);
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(ss).padStart(2, "0") + "." + tenths;
}

export function fmtDur(ms) {
  const s = Math.round((ms || 0) / 1000);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m" + String(s % 60).padStart(2, "0") + "s";
  return Math.floor(m / 60) + "h" + String(m % 60).padStart(2, "0") + "m";
}

/* Elapsed of a saved run item (``run_items_svc`` row) at server-now. */
export function itemElapsed(row, frozenAtMs) {
  const base = Number(row.elapsed_ms) || 0;
  if (row.status === "running") {
    const since = parseTs(row.active_since) || parseTs(row.started_at);
    const now = frozenAtMs || serverNow();
    return since != null ? base + Math.max(0, now - since) : base;
  }
  return base;
}

export function debounce(fn, ms) {
  let t = 0;
  return (...a) => { window.clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
}

export const statusClass = (s) => "status-" + String(s || "pending").replace(/[^a-z_]/g, "");
