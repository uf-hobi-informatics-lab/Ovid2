/*
 * Ovid Atlas — card renderer.
 *
 * Every node is a fixed-size card in the run-console style (dark pane,
 * mono text, status dots, orange clocks). Cards never expand in place — the
 * lane height (layout.fitLanes) picks a density and the dock shows the
 * rest. A run config is drawn as one *pipeline component*
 * (``pipelineHtml``): its name, shape (run kind · package), material
 * (engine type) and input → output, each with its logo (``static/logos.js``);
 * every part carries ``data-part-id`` so clicking it opens that node. The
 * component carries its transport (▶ ‖ ■) and live clock; at the roomy
 * density also the running table and its stage chain.
 */

import { KINDS, STAGES, STAGE_SHORT, LAZY_STAGES } from "/static/atlas/kinds.js";
import { state } from "/static/atlas/state.js";
import { esc, fmtDur, itemElapsed, statusClass } from "/static/atlas/util.js";
import { logoSvg, logoForType, logoForEngine, logoForKind } from "/static/logos.js";

function badge(text, cls) {
  return '<span class="atl-badge' + (cls ? " " + cls : "") + '">' + esc(text) + "</span>";
}

/* The most relevant job of a config: live first, else the latest. */
export function jobFor(configId) {
  const jobs = state.liveByConfig.get(configId) || [];
  return jobs.find((j) => j.status === "running") || jobs.find((j) => j.status === "queued") || jobs[0] || null;
}

export function jobStatus(job) {
  if (!job) return "idle";
  if (job.status === "running" && (job.paused || job.pause_requested)) return "paused";
  return job.status;
}

function drift(n) {
  if (!state.overlays.has("drift") || !state.drift) return "";
  if (n.kind === "config") {
    const d = state.drift.configs[n.name];
    if (!d) return "";
    if (d.never_succeeded) return badge("never ok", "is-warn");
    return d.changed.length ? badge("changed ×" + d.changed.length, "is-drift") : "";
  }
  for (const d of Object.values(state.drift.configs)) {
    if ((d.changed || []).some((c) => c.node === n.id)) return badge("changed", "is-drift");
  }
  return "";
}

function healthBadge(n) {
  if (!state.overlays.has("health") || !state.health || n.kind !== "address") return "";
  const h = state.health.addresses[n.name];
  if (!h) return "";
  const n2 = Object.values(h.tables).reduce((a, l) => a + l.length, 0);
  return n2 ? badge("✚ " + n2 + " checks", "is-health") : badge("✚ healthy", "is-ok");
}

function presenceDots(id) {
  if (!state.overlays.has("presence")) return "";
  const here = state.presence.filter((p) => p.node === id);
  if (!here.length) return "";
  return '<span class="atl-presence">' + here.slice(0, 4).map((p) =>
    '<i class="atl-avatar' + (p.mode === "edit" ? " is-edit" : "") + '" title="' + esc(p.display_name + (p.mode === "edit" ? " is editing" : " is viewing")) + '">' +
    esc((p.display_name || "?").slice(0, 1).toUpperCase()) + "</i>").join("") + "</span>";
}

function header(n, extra) {
  const k = KINDS[n.kind];
  return '<div class="atl-card-head">' +
    '<span class="atl-glyph is-' + n.kind + '">' + k.glyph + "</span>" +
    '<span class="atl-name mono" title="' + esc(n.name) + '">' + esc(n.label) + "</span>" +
    (extra || "") + presenceDots(n.id) + "</div>";
}

function liveClock(job) {
  if (!job || !job.started_at) return "";
  const running = job.status === "running" || job.status === "queued";
  return '<span class="atl-clock mono' + (running ? " is-live" : "") + '" data-clock-start="' + esc(job.started_at) +
    '" data-clock-end="' + esc(job.ended_at || "") + '" data-clock-paused="' + (jobStatus(job) === "paused" ? "1" : "") + '"></span>';
}

function transport(n, job) {
  if (!state.caps.run) return "";
  const st = jobStatus(job);
  const live = st === "running" || st === "queued" || st === "paused";
  const pausable = job && job.pausable;
  let html = '<span class="atl-transport">';
  if (!live) html += '<button type="button" class="atl-tbtn" data-act="run" title="Run now">▶</button>';
  if (live && pausable && st !== "paused") html += '<button type="button" class="atl-tbtn" data-act="pause" title="Pause after the current table">‖</button>';
  if (st === "paused") html += '<button type="button" class="atl-tbtn is-resume" data-act="resume" title="Resume">▶</button>';
  if (live) html += '<button type="button" class="atl-tbtn is-stop" data-act="cancel" title="Cancel the run">■</button>';
  return html + "</span>";
}

/* Stage chain of a config's current table. */
export function chainHtml(job, table) {
  const stages = (job && job.stages) || [];
  const mine = stages.filter((s) => s.item.startsWith(table + "/"));
  const byStage = Object.fromEntries(mine.map((s) => [s.item.slice(table.length + 1), s]));
  return '<ol class="atl-chain" aria-label="Transformation chain of ' + esc(table) + '">' + STAGES.map((st) => {
    const r = byStage[st];
    const status = r ? r.status : "pending";
    const lazy = LAZY_STAGES.has(st);
    const t = r ? itemElapsed(r) : 0;
    return '<li class="atl-step ' + statusClass(status) + (lazy ? " is-lazy" : " is-exec") + '" title="' + esc(st + (lazy ? " (builds the lazy plan)" : " (the engine executes the plan)") + (r ? " · " + fmtDur(t) : "")) + '">' +
      '<span class="atl-step-dot"></span><span class="atl-step-name">' + STAGE_SHORT[st] + "</span>" +
      (r && status === "running" ? '<span class="atl-step-clock mono" data-item-clock=\'' + esc(JSON.stringify(r)) + "'></span>" : "") + "</li>";
  }).join('<li class="atl-step-sep" aria-hidden="true">›</li>') + "</ol>";
}

/* One-line summary shown on a collapsed card. */
function summary(n, job) {
  const m = n.meta;
  switch (n.kind) {
    case "config": {
      const st = jobStatus(job);
      const heatC = state.overlays.has("heat") && state.heat && state.heat.configs[n.name];
      return '<span class="atl-sub small mono">' + esc(m.kind_label) + " · " + esc(m.engine) + "</span>" +
        (st !== "idle" ? '<span class="atl-status ' + statusClass(st) + '">' + esc(st) + "</span>" : "") +
        (heatC ? '<span class="atl-heat mono" title="median of ' + heatC.runs + ' runs, ' + heatC.failed + ' failed">' + fmtDur(heatC.median_ms) + (heatC.failed ? " ✗" + heatC.failed : "") + "</span>" : "");
    }
    case "package": return '<span class="atl-sub small mono">' + esc(m.cdm || "") + (m.editable ? "" : " · ro") + " · " + (m.formatted_tables || 0) + "t</span>";
    case "engine": return '<span class="atl-sub small mono">' + esc(m.type) + "</span>";
    case "account": return '<span class="atl-sub small mono">' + esc(m.type) + (m.read_only ? " · ro" : "") + "</span>";
    case "address": return '<span class="atl-sub small mono">' + esc(m.connection || m.type || "") + (m.missing ? " · missing" : "") + "</span>" + healthBadge(n);
    case "workflow": return '<span class="atl-sub small mono">' + (m.steps || []).length + " steps · " + esc(m.trigger) + "</span>" +
      (m.last_run_status ? '<span class="atl-status ' + statusClass(m.last_run_status) + '">' + esc(m.last_run_status) + "</span>" : "");
    default: return "";
  }
}

/* Live progress line of a config card: running table, n/m, pause note. */
function progressHtml(job) {
  if (!job) return "";
  const st = jobStatus(job);
  if (!["running", "queued", "paused"].includes(st)) return "";
  const items = job.items || [];
  const running = items.find((i) => i.status === "running");
  const done = items.filter((i) => ["completed", "passed", "exception"].includes(i.status)).length;
  const failed = items.filter((i) => ["failed", "error"].includes(i.status)).length;
  const total = (job.tables || []).includes("all") ? items.length || "?" : (job.tables || []).length || items.length;
  return '<div class="atl-progress small mono"><span>' + done + "/" + total + " tables</span>" +
    (failed ? '<span class="is-err">' + failed + " failed</span>" : "") +
    (running ? '<span class="is-run" title="' + esc(running.item) + '">▸ ' + esc(running.item) + "</span>" : "") +
    (st === "paused" ? '<span class="is-paused">paused</span>' : job.pause_requested ? '<span class="is-paused">pausing…</span>' : "") + "</div>";
}

/*
 * A card's inner HTML at a lane density: ``compact`` is one line (name +
 * status / clock), ``normal`` adds the summary line, ``roomy`` adds the
 * live progress and the transformation chain of a running config. Cards
 * never grow: everything else about a node lives in the dock.
 */
export function cardHtml(n, density = "normal") {
  const job = n.kind === "config" ? jobFor(n.id) : null;
  const extra = (n.kind === "config" ? liveClock(job) + transport(n, job) : "") + (density === "compact" ? "" : drift(n));
  let html = header(n, extra);
  if (density !== "compact") html += '<div class="atl-card-sum">' + summary(n, job) + "</div>";
  if (n.kind === "config" && density === "roomy") {
    html += progressHtml(job);
    const running = job && (job.items || []).find((i) => i.status === "running");
    if (running) html += chainHtml(job, running.item);
  }
  return html;
}

/* Status class a card carries (drives the beam animation). */
export function cardStatus(n) {
  if (n.kind === "config") return jobStatus(jobFor(n.id));
  return "";
}

/* ------------------------------------------------- pipeline component */

function part(role, id, logo, text, title, { dim = false, empty = false } = {}) {
  const drag = id && state.caps.write && (role === "material" || role === "input" || role === "output") && !empty ? ' draggable="true"' : "";
  return '<span class="atl-part is-' + role + (dim ? " is-dim" : "") + (empty ? " is-empty" : "") + '"' +
    (id ? ' data-part-id="' + esc(id) + '"' : "") + drag + ' title="' + esc(title) + '">' +
    (logo ? logoSvg(logo, 14, "atl-part-logo") : "") + '<span class="atl-part-text">' + esc(text) + "</span></span>";
}

function addressPart(role, ids) {
  const id = ids.find((x) => state.byId.has(x));
  if (!id) return part(role, null, null, "per run", role + " chosen per run", { empty: true });
  const a = state.byId.get(id), m = a.meta || {};
  const logo = m.missing ? "unknown" : logoForType(m.type);
  return part(role, id, logo, a.label || a.name, role + " · " + a.name + (m.connection ? " on " + m.connection : "") + (m.type ? " (" + m.type + ")" : "") + (m.missing ? " — missing" : ""), { dim: !!m.missing });
}

/*
 * The four parts of a run config: the *shape* (run kind · package), the
 * *material* (engine type) and the input → output addresses, each with
 * its logo. ``beam`` is the config's (or a workflow step's) beam; parts
 * whose layer is hidden are left out.
 */
function pipelineParts(n, beam, compact) {
  const m = n.meta || {};
  const out = [];
  if (state.layers.has("algorithm")) {
    const pkgId = (beam.package || []).find((x) => state.byId.has(x));
    const shape = m.package_required === false || !m.pipeline ? m.kind_label : m.kind_label + " · " + m.pipeline;
    out.push(part("shape", pkgId || null, logoForKind(m.kind), compact ? m.kind_label : shape, "shape · " + m.kind_label + (m.package_required === false ? "" : " · package " + (m.pipeline || "—"))));
  }
  if (state.layers.has("compute")) {
    const eid = (beam.engine || []).find((x) => state.byId.has(x));
    const e = eid ? state.byId.get(eid) : null;
    const type = (e && e.meta.type) || m.engine_type || m.engine || "";
    out.push(part("material", eid || null, logoForEngine(type, e && e.meta.family), type, "material · engine " + (e ? e.name : m.engine) + (e && e.name !== type ? " (" + type + ")" : "") + (e && !e.meta.configured ? " — bare type" : "")));
  }
  if (state.layers.has("storage")) {
    out.push('<span class="atl-io">' + addressPart("input", beam.input || []) + '<span class="atl-io-arrow" aria-hidden="true">→</span>' + addressPart("output", beam.output || []) + "</span>");
  }
  return out.join("");
}

/*
 * One pipeline component's inner HTML: name (+ clock, transport, status),
 * then shape · material · input → output. ``opts.label`` replaces the
 * name (a workflow step label), ``opts.badges`` adds header badges.
 */
export function pipelineHtml(n, beam, density = "normal", opts = {}) {
  const job = jobFor(n.id);
  const st = jobStatus(job);
  const heatC = state.overlays.has("heat") && state.heat && state.heat.configs[n.name];
  const extra = (opts.badges || "") +
    (st !== "idle" && density !== "compact" ? '<span class="atl-status ' + statusClass(st) + '">' + esc(st) + "</span>" : "") +
    (heatC && density !== "compact" ? '<span class="atl-heat mono" title="median of ' + heatC.runs + " runs, " + heatC.failed + ' failed">' + fmtDur(heatC.median_ms) + (heatC.failed ? " ✗" + heatC.failed : "") + "</span>" : "") +
    (density === "compact" ? "" : drift(n)) + liveClock(job) + transport(n, job);
  const name = opts.label || n.label;
  let html = '<div class="atl-card-head">' +
    '<span class="atl-glyph is-config">' + KINDS.config.glyph + "</span>" +
    '<span class="atl-name mono" title="' + esc(n.name + (opts.label ? " · step " + opts.label : "") + (n.meta.description ? " — " + n.meta.description : "")) + '">' + esc(name) + "</span>" +
    (density === "compact" ? '<span class="atl-pipe-parts is-mini">' + pipelineParts(n, beam, !opts.wide) + "</span>" : "") +
    extra + presenceDots(n.id) + "</div>";
  if (density !== "compact") html += '<div class="atl-pipe-parts">' + pipelineParts(n, beam, false) + "</div>";
  if (density === "roomy") {
    html += progressHtml(job);
    const running = job && (job.items || []).find((i) => i.status === "running");
    if (running) html += chainHtml(job, running.item);
  }
  return html;
}
