/*
 * Ovid Atlas — the dock: everything about the selected node, in place.
 *
 * Tabs depend on the node kind (kinds.js ``tabs``):
 *
 *   overview  facts, links to the classic pages, live / history of a config
 *   run       the live run console of the config's job: per-table rows with
 *             server-synced clocks, the transformation chain of the running
 *             table (lazy plan steps vs the executing write), the event log,
 *             and ▶ / ‖ / ■ / replay
 *   lanes     field lanes of a package table (lineage.js) + the spec editor
 *   data      Data Space: browse folders / schemas, a table's schema and
 *             sample rows (agents need a human approval — the API gates it)
 *   edit      in-place form for the saved config / address / connection /
 *             engine (catalogue-driven; selects for closed sets, UI rule U2)
 *   steward   ask Agent Ovid about the selection; its proposals show as a
 *             diff on the canvas and run only when you click them
 */

import { KINDS, STAGES, LAZY_STAGES } from "/static/atlas/kinds.js";
import { state, emit, configsUsing } from "/static/atlas/state.js";
import { jobFor, jobStatus } from "/static/atlas/cards.js";
import { drawLanes, openStepDoc, parkEditor, editorDirty, pkgSummary, invalidatePackage } from "/static/atlas/lineage.js";
import { $, $$, api, enc, esc, fmtClock, fmtDur, itemElapsed, parseTs, statusClass, tip } from "/static/atlas/util.js";
import { createEventReader } from "/static/display/terminal/reader.js";
import { formatEvent } from "/static/display/terminal/formatter.js";

const TAB_LABEL = { overview: "overview", run: "run console", lanes: "lanes", data: "data space", edit: "edit", steward: "ask steward" };

let root, bodyEl, tabsEl;
let current = { id: null, tab: "overview", extra: {} };
let reader = null;
let logLines = [];

export function initDock(el) {
  root = el;
  bodyEl = $("[data-atlas-dock-body]", el);
  tabsEl = $("[data-atlas-tabs]", el);
  tabsEl.addEventListener("click", (e) => {
    const b = e.target.closest("[data-tab]");
    if (b) openTab(b.dataset.tab);
  });
  bodyEl.addEventListener("click", onBodyClick);
  document.addEventListener("ovid2:spec-saved", () => {
    const n = state.byId.get(current.id);
    if (n && n.kind === "package") { invalidatePackage(n.name); emit("refresh-node", n.id); }
  });
}

function closeReader() {
  if (reader) { try { reader.close(); } catch (_e) { /* ignore */ } }
  reader = null;
}

function tabsFor(n) {
  const base = [...((KINDS[n.kind] || {}).tabs || ["overview"])];
  if (n.kind === "address" && !state.caps.data) base.splice(base.indexOf("data"), 1);
  if (!state.caps.write) { const i = base.indexOf("edit"); if (i >= 0 && n.kind !== "package") base.splice(i, 1); }
  if (document.getElementById("llm-panel")) base.push("steward");
  return base;
}

function renderTabs(n) {
  const tabs = tabsFor(n);
  tabsEl.innerHTML = tabs.map((t) => '<button type="button" role="tab" class="atlas-tab' + (t === current.tab ? " is-on" : "") +
    '" data-tab="' + t + '" aria-selected="' + (t === current.tab) + '">' + TAB_LABEL[t] + "</button>").join("") +
    '<span class="atlas-dock-title mono"><span class="atl-glyph is-' + n.kind + '">' + KINDS[n.kind].glyph + "</span> " + esc(n.name) + "</span>";
}

/* Select a node; ``tab`` optional; ``extra`` carries e.g. {table, path, step}. */
export function show(id, tab, extra) {
  const n = state.byId.get(id);
  if (!n) { bodyEl.innerHTML = '<p class="empty">Select a card.</p>'; tabsEl.innerHTML = ""; return; }
  if (editorDirty() && current.id !== id && !window.confirm("Discard unsaved changes in the editor?")) return;
  const tabs = tabsFor(n);
  const want = tab && tabs.includes(tab) ? tab : current.id === id && tabs.includes(current.tab) ? current.tab : defaultTab(n, tabs);
  current = { id, tab: want, extra: extra || (current.id === id ? current.extra : {}) };
  renderTabs(n);
  render();
}

function defaultTab(n, tabs) {
  if (n.kind === "config") { const j = jobFor(n.id); if (j && ["running", "queued"].includes(j.status)) return "run"; }
  return tabs[0];
}

export function openTab(tab, extra) {
  if (!current.id) return;
  if (extra) current.extra = Object.assign({}, current.extra, extra);
  show(current.id, tab, current.extra);
}

export function currentNode() { return current.id; }
export function currentTab() { return current.tab; }

function render() {
  closeReader();
  parkEditor(root);
  const n = state.byId.get(current.id);
  if (!n) return;
  emit("presence-mode", current.tab === "edit" || current.tab === "lanes" ? "edit" : "view");
  const fn = TABS[current.tab] || TABS.overview;
  Promise.resolve(fn(n, current.extra)).catch((err) => { bodyEl.innerHTML = '<p class="flash error">' + esc(err.message) + "</p>"; });
}

/* Live updates re-render only the tabs that show live data. */
let wasLive = false;
export function onLive() {
  const n = state.byId.get(current.id);
  if (!n) return;
  if (n.kind === "config") {
    const j = jobFor(n.id);
    const live = !!j && ["running", "queued"].includes(j.status);
    // A run that starts while its pipeline is selected opens its console.
    if (live && !wasLive && current.tab === "overview") { wasLive = live; openTab("run"); return; }
    wasLive = live;
  }
  if (current.tab === "run") updateRun(n);
  else if (current.tab === "overview" && n.kind === "config") TABS.overview(n);
}

/* ------------------------------------------------------------ overview */

function lockNote(n) {
  const locks = state.presence.filter((p) => p.node === n.id && p.mode === "edit");
  return locks.length ? '<p class="flash warn small">' + esc(locks.map((p) => p.display_name).join(", ")) + " is editing this right now.</p>" : "";
}

function factsDl(pairs) {
  return '<dl class="atl-dl">' + pairs.filter(([, v]) => v != null && v !== "").map(([k, v]) => "<dt>" + esc(k) + "</dt><dd>" + v + "</dd>").join("") + "</dl>";
}

const link = (href, label) => '<a href="' + esc(href) + '">' + esc(label) + "</a>";
const nodeLink = (id, label) => '<button type="button" class="atl-link mono" data-goto="' + esc(id) + '">' + esc(label || id.split(":").slice(1).join(":")) + "</button>";

async function overview(n) {
  const m = n.meta;
  let html = lockNote(n);
  if (n.kind === "config") {
    const job = jobFor(n.id);
    const heat = state.heat && state.heat.configs[n.name];
    const drift = state.drift && state.drift.configs[n.name];
    html += factsDl([
      ["kind", esc(m.kind_label)],
      ["package", m.pipeline ? nodeLink("package:" + m.pipeline, m.pipeline) : ""],
      ["input", m.input_address ? nodeLink("address:" + m.input_address, m.input_address) : '<span class="nw-muted">chosen per run</span>'],
      ["engine", nodeLink("engine:" + m.engine, m.engine)],
      ["output", m.output_address ? nodeLink("address:" + m.output_address, m.output_address) : '<span class="nw-muted">chosen per run</span>'],
      ["tables", esc((m.tables || []).join(", "))],
      ["trigger", esc(m.trigger) + (m.enabled ? "" : " (disabled)")],
      ["last run", job ? '<span class="atl-status ' + statusClass(jobStatus(job)) + '">' + esc(jobStatus(job)) + "</span> " + esc((job.submitted_at || "").slice(0, 19).replace("T", " ")) : "—"],
      ["typical", heat ? fmtDur(heat.median_ms) + " over " + heat.runs + " runs" + (heat.failed ? ", " + heat.failed + " failed" : "") : ""],
      ["changed", drift && drift.changed && drift.changed.length ? drift.changed.map((c) => nodeLink(c.node) + " " + esc(c.at.slice(0, 16).replace("T", " "))).join("<br>") : ""],
    ]);
    html += '<p class="atl-actions">' + (state.caps.run ? '<button type="button" class="button small primary" data-do="run">▶ run now</button> ' : "") +
      '<button type="button" class="button small" data-do="impact">blast radius</button> ' +
      link("/pipelines/" + enc(n.name), "pipeline page") + " · " + link("/runs", "jobs") + "</p>";
    try {
      const d = await api("GET", "/atlas/nodes/" + enc(n.id));
      const jobs = d.recent_jobs || [];
      if (jobs.length) {
        html += '<h3 class="atl-h">recent runs ' + tip("Replay any finished run on the canvas: the beam and the table rows animate along the run's saved timeline.") + '</h3><ul class="atl-rows">' + jobs.map((j) =>
          '<li class="atl-row table-row ' + statusClass(j.status) + '"><span class="table-row-dot ' + statusClass(j.status) + '"></span>' +
          '<span class="atl-row-name mono">' + esc((j.submitted_at || "").slice(0, 19).replace("T", " ")) + "</span>" +
          '<span class="atl-sub mono small">' + esc(j.status) + (j.started_at && j.ended_at ? " · " + fmtDur(parseTs(j.ended_at) - parseTs(j.started_at)) : "") + "</span>" +
          (j.instance_id ? '<a class="atl-link" href="/runs/' + esc(j.instance_id) + '">console</a>' : "") +
          (j.instance_id && !["running", "queued"].includes(j.status) ? '<button type="button" class="atl-link" data-replay="' + esc(j.instance_id) + '" data-replay-job=\'' + esc(JSON.stringify(j)) + "'>replay</button>" : "") +
          "</li>").join("") + "</ul>";
      }
    } catch (_e) { /* the facts are enough */ }
  } else if (n.kind === "package") {
    html += factsDl([["cdm", esc(m.cdm)], ["profile", esc(m.profile)], ["template", esc(m.template)], ["tables", String(m.formatted_tables)],
      ["fixers", esc(m.fixer_source || "—")], ["editing", m.editable ? (state.caps.write ? "editable" : "read-only (needs manage-config)") : "generated port — read-only"],
      ["used by", configsUsing(n.id).map((c) => nodeLink(c)).join(", ")]]);
    html += '<p class="atl-actions">' + link("/pipeline-packages/" + enc(n.name) + "/builder", "builder") + " · " + link("/pipeline-packages/" + enc(n.name), "package hub") + "</p>";
    try {
      const L = await pkgSummary(n.name);
      html += '<h3 class="atl-h">tables</h3><ul class="atl-rows">' + L.tables.filter((t) => t.configured).map((t) =>
        '<li class="atl-row" data-lanes="' + esc(t.table) + '"><span class="atl-row-name mono">' + esc(t.table) + '</span><span class="atl-sub small">' + t.fields + " fields</span></li>").join("") + "</ul>";
    } catch (err) { html += '<p class="flash error">' + esc(err.message) + "</p>"; }
  } else if (n.kind === "engine") {
    html += factsDl([["type", esc(m.type)], ["family", esc(m.family)], ["config", m.configured ? "saved engine config" : "built-in type"],
      ["capabilities", '<span class="small mono">' + esc((m.capabilities || []).join(" · ")) + "</span>"],
      ["used by", configsUsing(n.id).map((c) => nodeLink(c)).join(", ")]]);
    if (m.configured) html += '<p class="atl-actions">' + link("/engines/" + enc(n.name) + "/edit", "engine page") + "</p>";
  } else if (n.kind === "account") {
    html += factsDl([["type", esc(m.type)], ["read-only", m.read_only ? "yes (never an output)" : "no"], ["data space", esc(m.dataspace_path)]]);
    html += '<p class="atl-actions">' + (state.caps.data ? '<button type="button" class="button small" data-tab-go="data">browse data</button> ' : "") +
      link("/connections/" + enc(n.name) + "/edit", "storage account page") + "</p>";
  } else if (n.kind === "address") {
    html += factsDl([["account", m.connection ? nodeLink("account:" + m.connection, m.connection) : "legacy"], ["location", '<code>' + esc(JSON.stringify(m.location || {})) + "</code>"],
      ["data space", esc(m.dataspace_path || "")], ["used by", configsUsing(n.id).map((c) => nodeLink(c)).join(", ")]]);
    const h = state.health && state.health.addresses[n.name];
    if (h) {
      html += '<h3 class="atl-h">data health ' + tip("The latest health run on this location. Each failed check is pinned to the tables its metrics point at, with the fixers / transforms the check catalogue recommends.") + "</h3>" +
        '<p class="small mono">' + esc((h.generated_at || "").slice(0, 16).replace("T", " ")) + " · " + esc(JSON.stringify(h.summary)) + "</p>" +
        '<ul class="atl-rows">' + Object.entries(h.tables).map(([t, list]) => list.map((c) =>
          '<li class="atl-row"><span class="atl-badge is-health sev-' + esc(c.severity) + '">' + esc(c.severity) + '</span><span class="atl-row-name mono">' + esc(t) + " · " + esc(c.check_id) + " " + esc(c.name || "") + "</span>" +
          '<span class="atl-sub small">' + c.exceptions + " exc.</span>" + ((c.remediation.fixers || []).length ? '<span class="atl-sub small">fix: ' + esc(c.remediation.fixers.join(", ")) + "</span>" : "") + "</li>").join("")).join("") + "</ul>";
    }
    html += '<p class="atl-actions">' + (state.caps.data ? '<button type="button" class="button small" data-tab-go="data">browse data</button> ' : "") +
      '<button type="button" class="button small" data-do="impact">blast radius</button></p>';
  } else if (n.kind === "workflow") {
    html += factsDl([["trigger", esc(m.trigger)], ["enabled", m.enabled ? "yes" : "no"], ["last run", esc(m.last_run_status || "—")],
      ["steps", (m.steps || []).map((s) => nodeLink("config:" + s.pipeline, s.label || s.id)).join(" → ")]]);
    html += '<p class="atl-actions">' + (state.caps.run ? '<button type="button" class="button small primary" data-do="run-workflow">▶ run workflow</button> ' : "") +
      link("/workflows/" + enc(n.name), "workflow canvas") + "</p>";
  }
  bodyEl.innerHTML = '<div class="atl-pane">' + html + "</div>";
}

/* ----------------------------------------------------------------- run */

function runSkeleton(n, job) {
  const st = jobStatus(job);
  return '<div class="atl-run" data-run>' +
    '<div class="atl-run-head term-summary">' +
    '<span class="term-prompt-label" data-status="' + esc(st) + '">ovid2</span>' +
    '<span class="mono">' + esc(n.name) + "</span>" +
    '<span class="atl-status ' + statusClass(st) + '" data-run-status>' + esc(st) + "</span>" +
    '<span class="pl-flex"></span>' +
    '<span class="term-total mono" data-run-total></span>' +
    '<span class="atl-transport" data-run-transport></span>' +
    "</div>" +
    '<div class="atl-run-chain" data-run-chain></div>' +
    '<div class="term-split atl-run-split">' +
    '<div><div class="term-split-title">tables</div><ul class="atl-rows" data-run-tables></ul></div>' +
    '<div><div class="term-split-title">events ' + tip("The run's event log, live. Steps before write only build the lazy plan (instant); the engine does the work during write.") + '</div><div class="run-detail-log atl-log mono" data-run-log></div></div>' +
    "</div></div>";
}

async function runTab(n) {
  const job = jobFor(n.id);
  if (!job) {
    bodyEl.innerHTML = '<div class="atl-pane"><p class="empty">No run of this config yet.</p>' +
      (state.caps.run ? '<p><button type="button" class="button small primary" data-do="run">▶ run now</button></p>' : "") + "</div>";
    return;
  }
  bodyEl.innerHTML = runSkeleton(n, job);
  logLines = [];
  updateRun(n);
  if (job.instance_id) {
    reader = createEventReader({
      instanceId: job.instance_id,
      pollMs: 1500,
      onEvents(events) {
        for (const ev of events) {
          const l = formatEvent(ev);
          if (l) logLines.push(l);
        }
        if (logLines.length > 400) logLines = logLines.slice(-400);
        const host = $("[data-run-log]", bodyEl);
        if (!host) return;
        const stick = host.scrollTop + host.clientHeight >= host.scrollHeight - 8;
        host.innerHTML = logLines.map((l) => '<div class="run-detail-line ' + esc(l.className) + '"><span class="run-detail-ts">' + esc(l.ts) + "</span><span>" + esc(l.text) + "</span></div>").join("");
        if (stick) host.scrollTop = host.scrollHeight;
      },
    });
  }
}

function updateRun(n) {
  const job = jobFor(n.id);
  const box = $("[data-run]", bodyEl);
  if (!box || !job) return;
  const st = jobStatus(job);
  $("[data-run-status]", box).className = "atl-status " + statusClass(st);
  $("[data-run-status]", box).textContent = st;
  $(".term-prompt-label", box).dataset.status = st;
  const tr = $("[data-run-transport]", box);
  const live = ["running", "queued", "paused"].includes(st);
  tr.innerHTML = state.caps.run ? (
    (!live ? '<button type="button" class="atl-tbtn" data-do="run" title="Run again">▶</button>' : "") +
    (live && job.pausable && st !== "paused" ? '<button type="button" class="atl-tbtn" data-do="pause" title="Pause after the current table">‖</button>' : "") +
    (st === "paused" ? '<button type="button" class="atl-tbtn is-resume" data-do="resume" title="Resume">▶</button>' : "") +
    (live ? '<button type="button" class="atl-tbtn is-stop" data-do="cancel" title="Cancel">■</button>' : "")) : "";
  const total = $("[data-run-total]", box);
  total.dataset.clockStart = job.started_at || "";
  total.dataset.clockEnd = job.ended_at || "";
  total.dataset.clockPaused = st === "paused" ? "1" : "";
  total.classList.toggle("is-stopped", !live);
  const items = job.items || [];
  const runningItem = items.find((i) => i.status === "running");
  const tables = (job.tables || []).filter((t) => t !== "all");
  const names = [...new Set([...items.map((i) => i.item), ...tables])];
  const byName = new Map(items.map((i) => [i.item, i]));
  $("[data-run-tables]", box).innerHTML = names.map((t) => {
    const it = byName.get(t);
    const s = it ? it.status : live ? "pending" : "idle";
    return '<li class="atl-row table-row ' + statusClass(s) + '" data-run-table="' + esc(t) + '"><span class="table-row-dot ' + statusClass(s) + '"></span>' +
      '<span class="atl-row-name mono">' + esc(it && it.display_name && it.kind === "check" ? it.display_name : t) + "</span>" +
      '<span class="atl-sub small mono">' + esc(s) + "</span>" +
      (it ? '<span class="atl-row-clock mono' + (s === "running" ? " is-live" : "") + '" data-item-clock=\'' + esc(JSON.stringify(it)) + "'>" + fmtClock(itemElapsed(it)) + "</span>" : "") + "</li>";
  }).join("") || '<li class="atl-row small">waiting for the first table…</li>';
  const chainTable = current.extra.table || (runningItem && runningItem.item) || (items.length ? items[items.length - 1].item : "");
  $("[data-run-chain]", box).innerHTML = chainTable ? chainBlock(job, chainTable) : "";
}

function chainBlock(job, table) {
  const stages = (job.stages || []).filter((s) => s.item.startsWith(table + "/"));
  const by = Object.fromEntries(stages.map((s) => [s.item.slice(table.length + 1), s]));
  const seen = STAGES.filter((s) => by[s]);
  const shown = seen.length ? STAGES.filter((s) => by[s] || (s !== "deduplicate" && s !== "transform")) : STAGES;
  return '<div class="atl-chain-wide"><span class="term-split-title">chain · <span class="mono">' + esc(table) + "</span> " +
    tip("Every table passes read → format → … → fix → write. Steps before write only build Spark's / DuckDB's lazy plan, so they finish almost instantly; the engine executes the whole chain during write. Pending steps are hollow, finished ones filled, the running one pulses.") + "</span>" +
    '<ol class="atl-chain is-wide">' + shown.map((st) => {
      const r = by[st];
      const status = r ? r.status : "pending";
      return '<li class="atl-step ' + statusClass(status) + (LAZY_STAGES.has(st) ? " is-lazy" : " is-exec") + '"><span class="atl-step-dot"></span><span class="atl-step-name">' + esc(st) + "</span>" +
        (r ? '<span class="atl-step-clock mono" data-item-clock=\'' + esc(JSON.stringify(r)) + "'>" + fmtClock(itemElapsed(r)) + "</span>" : "") + "</li>";
    }).join('<li class="atl-step-sep" aria-hidden="true">›</li>') + "</ol></div>";
}

/* --------------------------------------------------------------- lanes */

async function lanesTab(n, extra) {
  if (n.kind !== "package") { bodyEl.innerHTML = '<p class="empty">Lanes are per package table.</p>'; return; }
  const L = await pkgSummary(n.name);
  const tables = L.tables.filter((t) => t.configured).map((t) => t.table);
  const table = extra.table && tables.includes(extra.table) ? extra.table : tables[0];
  if (!table) { bodyEl.innerHTML = '<p class="empty">No configured tables.</p>'; return; }
  current.extra.table = table;
  const health = [];
  if (state.health) for (const cid of configsUsing(n.id)) {
    const cfg = state.byId.get(cid);
    for (const a of [cfg && cfg.meta.input_address, cfg && cfg.meta.output_address]) {
      const h = a && state.health.addresses[a];
      if (h) for (const c of h.tables[table] || []) health.push(c);
    }
  }
  bodyEl.innerHTML = '<div class="atl-lanes-shell"><div class="atl-lanes-bar"><label class="atl-tool"><span class="atl-tool-label">table</span><select data-lanes-table aria-label="Table">' +
    tables.map((t) => '<option' + (t === table ? " selected" : "") + ">" + esc(t) + "</option>").join("") + "</select></label></div>" +
    '<div class="atl-lanes-split"><div class="atl-lanes-host" data-lanes-host></div><div class="atl-editor-host" data-editor-host><p class="empty small">Click a chip to open the spec that defines it.</p></div></div></div>';
  $("[data-lanes-table]", bodyEl).addEventListener("change", (e) => openTab("lanes", { table: e.target.value }));
  await drawLanes($("[data-lanes-host]", bodyEl), n.name, table, {
    health,
    canWrite: state.caps.write,
    onOpenStep: (Lt, s) => openStepDoc($("[data-editor-host]", bodyEl), s, n.name, { canWrite: state.caps.write, editable: Lt.editable }),
  });
}

/* ---------------------------------------------------------------- data */

async function dataTab(n, extra) {
  const base = n.kind === "account" ? "/" + n.name : n.meta.dataspace_path;
  const path = extra.path || base;
  if (!path) { bodyEl.innerHTML = '<p class="empty">This location has no Data Space path.</p>'; return; }
  current.extra.path = path;
  const crumbs = path.split("/").filter(Boolean);
  const crumbHtml = '<nav class="atl-crumbs mono small">' + crumbs.map((c, i) =>
    '<button type="button" class="atl-link" data-ds-path="/' + esc(crumbs.slice(0, i + 1).join("/")) + '">' + esc(c) + "</button>").join(" / ") + "</nav>";
  if (extra.table) {
    bodyEl.innerHTML = '<div class="atl-pane">' + crumbHtml + '<p class="atl-loading small">describing ' + esc(extra.table) + "…</p></div>";
    let d, sample;
    try { d = await api("GET", "/dataspace/table?path=" + enc(extra.table)); } catch (err) { bodyEl.innerHTML = '<div class="atl-pane">' + crumbHtml + '<p class="flash error">' + esc(err.message) + "</p></div>"; return; }
    const cols = (d.columns || []).map((c) => "<tr><td class=mono>" + esc(c.name) + "</td><td class=mono>" + esc(c.type) + "</td><td>" + (c.nullable === false ? "not null" : "") + "</td></tr>").join("");
    let sampleHtml = '<p><button type="button" class="button small" data-ds-sample="' + esc(extra.table) + '">load sample rows</button> ' + tip("Up to 50 rows of the table. Row values may be PHI: an agent asking for them needs a human approval; you see them because you hold the view-pipeline-data capability.") + "</p>";
    if (extra.sample) {
      try {
        sample = await api("GET", "/dataspace/table/sample?limit=50&path=" + enc(extra.table));
        sampleHtml = '<div class="atl-sample"><table class="atl-table mono small"><thead><tr>' + sample.columns.map((c) => "<th>" + esc(c) + "</th>").join("") + "</tr></thead><tbody>" +
          sample.rows.map((r) => "<tr>" + r.map((v) => "<td>" + esc(v == null ? "∅" : v) + "</td>").join("") + "</tr>").join("") + "</tbody></table></div>" +
          (sample.message ? '<p class="small">' + esc(sample.message) + "</p>" : "");
      } catch (err) { sampleHtml = '<p class="flash error">' + esc(err.message) + "</p>"; }
    }
    bodyEl.innerHTML = '<div class="atl-pane">' + crumbHtml + '<h3 class="atl-h mono">▦ ' + esc(d.name || extra.table) + "</h3>" +
      factsDl([["format", esc(d.format || d.kind || "")], ["rows", d.row_count != null ? Number(d.row_count).toLocaleString() + (d.row_count_exact ? "" : " (est.)") : ""],
        ["size", d.size_bytes != null ? (d.size_bytes / 1048576).toFixed(1) + " MiB" : ""], ["modified", esc((d.modified_at || "").slice(0, 19).replace("T", " "))]]) +
      (d.message ? '<p class="small">' + esc(d.message) + "</p>" : "") +
      '<details open><summary class="small">columns (' + (d.columns || []).length + ')</summary><table class="atl-table small"><tbody>' + cols + "</tbody></table></details>" +
      sampleHtml + "</div>";
    return;
  }
  bodyEl.innerHTML = '<div class="atl-pane">' + crumbHtml + '<p class="atl-loading small">listing…</p></div>';
  let L;
  try { L = await api("GET", "/dataspace?path=" + enc(path)); } catch (err) { bodyEl.innerHTML = '<div class="atl-pane">' + crumbHtml + '<p class="flash error">' + esc(err.message) + "</p></div>"; return; }
  bodyEl.innerHTML = '<div class="atl-pane">' + crumbHtml +
    (L.message ? '<p class="small">' + esc(L.message) + "</p>" : "") +
    '<ul class="atl-rows">' + (L.entries || []).map((e) => {
      const leaf = e.kind === "table" || e.kind === "file";
      return '<li class="atl-row" ' + (leaf ? 'data-ds-table="' + esc(e.path) + '"' : 'data-ds-path="' + esc(e.path) + '"') + '><span class="atl-glyph">' + (leaf ? "▦" : "▸") + "</span>" +
        '<span class="atl-row-name mono">' + esc(e.name) + "</span>" +
        (e.row_count != null ? '<span class="atl-sub small mono">' + Number(e.row_count).toLocaleString() + " rows</span>" : "") +
        (e.size_bytes != null ? '<span class="atl-sub small mono">' + (e.size_bytes / 1048576).toFixed(1) + " MiB</span>" : "") + "</li>";
    }).join("") + ((L.entries || []).length ? "" : '<li class="atl-row small">empty</li>') + "</ul></div>";
}

/* ---------------------------------------------------------------- edit */

function fieldInput(f, value, prefix) {
  const id = prefix + f.name;
  const w = f.multiline ? "multiline" : f.widget && f.widget !== "str" ? f.widget : f.type || "str";
  const v = value == null ? "" : value;
  let input;
  if (w === "choices") {
    input = '<select name="' + esc(id) + '" class="mono">' + (f.required ? "" : '<option value="">(default' + (f.default != null && f.default !== "" ? " — " + esc(f.default) : "") + ")</option>") +
      (f.choices || []).map((c) => '<option value="' + esc(c) + '"' + (String(v) === String(c) ? " selected" : "") + ">" + esc(c) + "</option>").join("") + "</select>";
  } else if (w === "bool") {
    input = '<select name="' + esc(id) + '" class="mono"><option value="">(default' + (f.default != null ? " — " + esc(f.default) : "") + ")</option>" +
      ["true", "false"].map((c) => '<option value="' + c + '"' + (String(v).toLowerCase() === c ? " selected" : "") + ">" + c + "</option>").join("") + "</select>";
  } else if (w === "multiline") {
    input = '<textarea name="' + esc(id) + '" class="mono wide" rows="3">' + esc(v) + "</textarea>";
  } else {
    input = '<input type="' + (prefix === "sec_" ? "password" : w === "int" ? "number" : "text") + '" name="' + esc(id) + '" value="' + esc(v) + '" class="mono wide"' + (f.required ? " required" : "") + ">";
  }
  return "<tr><th><label>" + esc(f.name) + (f.required ? " *" : "") + "</label> " + (f.help ? tip(f.help) : "") + "</th><td>" + input + "</td></tr>";
}

function selectOf(name, options, value, { allowEmpty = "" } = {}) {
  return '<select name="' + esc(name) + '" class="mono">' + (allowEmpty ? '<option value="">' + esc(allowEmpty) + "</option>" : "") +
    options.map((o) => '<option value="' + esc(o) + '"' + (o === value ? " selected" : "") + ">" + esc(o) + "</option>").join("") +
    (value && !options.includes(value) ? '<option value="' + esc(value) + '" selected>' + esc(value) + " (current)</option>" : "") + "</select>";
}

async function editTab(n) {
  if (!state.caps.write) { bodyEl.innerHTML = '<p class="empty">Editing needs the manage-config capability.</p>'; return; }
  const lock = lockNote(n);
  if (n.kind === "package") { openTab("lanes"); return; }
  if (n.kind === "workflow") { bodyEl.innerHTML = '<div class="atl-pane">' + lock + '<p>Workflows are edited on their DAG canvas: ' + link("/workflows/" + enc(n.name), "open " + n.name) + ".</p></div>"; return; }
  if (n.kind === "config") {
    const d = (await api("GET", "/pipelines/" + enc(n.name)));
    const addrs = state.graph.nodes.filter((x) => x.kind === "address" && !x.meta.missing).map((x) => x.name).sort();
    const engines = state.graph.nodes.filter((x) => x.kind === "engine").map((x) => x.name).sort();
    const triggers = ["manual", "cron", "interval", "watch"];
    bodyEl.innerHTML = '<form class="atl-form" data-edit="config" data-visibility-root>' + lock +
      '<table class="kv-table edit"><tbody>' +
      "<tr><th>input " + tip("The exact location this pipeline reads. Leave open to choose it per run.") + "</th><td>" + selectOf("input_address", addrs, d.input_address, { allowEmpty: "(chosen per run)" }) + "</td></tr>" +
      "<tr><th>engine " + tip("Where the algorithms run. Changing it is checked against the capabilities every step of the package needs.") + "</th><td>" + selectOf("engine", engines, d.engine) + "</td></tr>" +
      "<tr><th>output " + tip("The exact location this pipeline writes. A read-only storage type is refused.") + "</th><td>" + selectOf("output_address", addrs, d.output_address, { allowEmpty: "(chosen per run)" }) + "</td></tr>" +
      "<tr><th>trigger</th><td>" + selectOf("trigger_type", triggers, d.trigger.type) + "</td></tr>" +
      '<tr data-show-when="trigger_type=cron"><th>cron</th><td><input type="text" name="trigger_cron" class="mono wide" value="' + esc(d.trigger.cron || "") + '"></td></tr>' +
      '<tr data-show-when="trigger_type=interval"><th>every (min)</th><td><input type="number" name="trigger_interval" class="mono" value="' + esc(d.trigger.interval_minutes || "") + '"></td></tr>' +
      '<tr><th>enabled</th><td>' + selectOf("enabled", ["true", "false"], String(d.enabled)) + "</td></tr>" +
      '<tr><th>description</th><td><input type="text" name="description" class="wide" maxlength="500" value="' + esc(d.description || "") + '"></td></tr>' +
      "</tbody></table>" +
      '<p class="atl-actions"><span class="pb-status small" data-edit-status></span><button type="submit" class="button primary small">save</button> ' + link("/pipelines/" + enc(n.name) + "/edit", "full editor") + "</p></form>";
    if (window.Ovid2Visibility) window.Ovid2Visibility.attach($("[data-visibility-root]", bodyEl));
    $("form", bodyEl).addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const status = $("[data-edit-status]", bodyEl);
      for (const role of ["engine", "input", "output"]) {
        const key = role === "engine" ? "engine" : role + "_address";
        const val = String(fd.get(key) || "");
        if (val && val !== d[key]) {
          const c = await api("POST", "/atlas/rebind:check", { config: n.name, role, target: val });
          if (!c.ok) { status.textContent = c.reason; status.className = "pb-status small is-err"; return; }
        }
      }
      const trigger = { type: String(fd.get("trigger_type")) };
      if (trigger.type === "cron") trigger.cron = String(fd.get("trigger_cron") || "");
      if (trigger.type === "interval") trigger.interval_minutes = Number(fd.get("trigger_interval") || 0) || null;
      if (trigger.type === "watch") trigger.watch_min_wait_minutes = d.trigger.watch_min_wait_minutes || null;
      const body = Object.assign(pipelineWrite(d), {
        input_address: String(fd.get("input_address") || ""), engine: String(fd.get("engine") || d.engine),
        output_address: String(fd.get("output_address") || ""), enabled: fd.get("enabled") === "true",
        description: String(fd.get("description") || ""), trigger,
      });
      try {
        await api("PUT", "/pipelines/" + enc(n.name), body);
        status.textContent = "saved"; status.className = "pb-status small is-ok";
        emit("refresh-graph");
      } catch (err) { status.textContent = err.message; status.className = "pb-status small is-err"; }
    });
    return;
  }
  if (n.kind === "address") {
    if (!n.meta.connection) { bodyEl.innerHTML = '<div class="atl-pane">' + lock + '<p>Legacy address: ' + link("/addresses/" + enc(n.name) + "/edit", "edit on its page") + ".</p></div>"; return; }
    const [d, spec] = await Promise.all([api("GET", "/addresses/" + enc(n.name)), api("GET", "/atlas/forms/location/" + enc(n.meta.type || ""))]);
    const accounts = state.graph.nodes.filter((x) => x.kind === "account").map((x) => x.name);
    bodyEl.innerHTML = '<form class="atl-form" data-edit="address">' + lock + '<table class="kv-table edit"><tbody>' +
      "<tr><th>storage account</th><td>" + selectOf("connection", accounts, d.connection) + "</td></tr>" +
      spec.args.map((f) => fieldInput(f, (d.location || {})[f.name], "loc_")).join("") + "</tbody></table>" +
      '<p class="atl-actions"><span class="pb-status small" data-edit-status></span><button type="submit" class="button primary small">save</button></p></form>';
    $("form", bodyEl).addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const location = {};
      for (const f of spec.args) { const v = fd.get("loc_" + f.name); if (v != null && String(v) !== "") location[f.name] = (f.type === "bool" || f.widget === "bool") ? String(v) === "true" : String(v); }
      const status = $("[data-edit-status]", bodyEl);
      try {
        await api("PUT", "/addresses/" + enc(n.name), { connection: String(fd.get("connection")), location });
        status.textContent = "saved"; status.className = "pb-status small is-ok";
        emit("refresh-graph");
      } catch (err) { status.textContent = err.message; status.className = "pb-status small is-err"; }
    });
    return;
  }
  if (n.kind === "account" || n.kind === "engine") {
    const isEng = n.kind === "engine";
    if (isEng && !n.meta.configured) { bodyEl.innerHTML = '<div class="atl-pane"><p>Built-in engine type — nothing to edit. ' + link("/engines/new", "create an engine config") + ".</p></div>"; return; }
    const [d, spec] = await Promise.all([api("GET", (isEng ? "/engines/" : "/connections/") + enc(n.name)), api("GET", "/atlas/forms/" + (isEng ? "engine" : "connection") + "/" + enc(n.meta.type))]);
    bodyEl.innerHTML = '<form class="atl-form">' + lock + '<table class="kv-table edit"><tbody>' +
      spec.args.map((f) => fieldInput(f, (d.args || {})[f.name], "arg_")).join("") +
      spec.secrets.map((f) => fieldInput(f, (d.secrets || {})[f.name] ? "***" : "", "sec_")).join("") + "</tbody></table>" +
      '<p class="atl-actions"><span class="pb-status small" data-edit-status></span><button type="submit" class="button primary small">save</button> ' +
      tip("Secrets are never shown: a stored secret reads ***; leave it to keep the stored value.") + "</p></form>";
    $("form", bodyEl).addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const args = {}, secrets = {};
      for (const f of spec.args) { const v = fd.get("arg_" + f.name); if (v != null && String(v) !== "") args[f.name] = String(v); }
      for (const f of spec.secrets) { const v = fd.get("sec_" + f.name); if (v != null && String(v) !== "") secrets[f.name] = String(v); }
      const status = $("[data-edit-status]", bodyEl);
      try {
        await api("PUT", (isEng ? "/engines/" : "/connections/") + enc(n.name), { type: n.meta.type, args, secrets, secret_ref: d.secret_ref || null });
        status.textContent = "saved"; status.className = "pb-status small is-ok";
        emit("refresh-graph");
      } catch (err) { status.textContent = err.message; status.className = "pb-status small is-err"; }
    });
  }
}

export function pipelineWrite(d) {
  return {
    kind: d.kind, cdm: d.cdm, input_cdm: d.input_cdm, pipeline: d.pipeline, input_address: d.input_address,
    output_address: d.output_address, tables: d.tables, transformations: d.transformations, packages: d.packages,
    engine: d.engine, description: d.description, workers: d.workers, sf_account: d.sf_account,
    stderr_log: d.stderr_log, allow_overwrite: d.allow_overwrite, enabled: d.enabled, trigger: d.trigger,
  };
}

/* ------------------------------------------------------------- steward */

async function stewardTab(n) {
  const sel = [...new Set([n.id, ...state.pinned])].slice(0, 12);
  bodyEl.innerHTML = '<div class="atl-pane">' +
    '<p class="small">context: ' + sel.map((i) => nodeLink(i)).join(" ") + " " + tip("Pin nodes (p) to add them. Agent Ovid reads only their metadata and links (no row values, no secrets) through the atlas_subgraph tool. Anything it proposes to change appears highlighted on the canvas and in the assistant panel; nothing runs until you click Run there.") + "</p>" +
    '<textarea class="wide mono" rows="3" data-steward-q placeholder="e.g. why does this run take longer than last week? / move this pipeline to DuckDB / what would break if I change this location?"></textarea>' +
    '<p class="atl-actions"><button type="button" class="button primary small" data-steward-ask>ask Agent Ovid</button></p></div>';
}

function askSteward(q) {
  const n = state.byId.get(current.id);
  const sel = [...new Set([n.id, ...state.pinned])].slice(0, 12);
  const prompt = "[Atlas selection: " + sel.join(", ") + " in workspace " + (state.graph.workspace || "default") +
    ". Call the atlas_subgraph tool with ids=\"" + sel.join(",") + "\" to read it.]\n" + q;
  const input = document.getElementById("llm-input");
  const form = document.getElementById("llm-form");
  if (!input || !form || !window.Ovid2Llm) return;
  window.Ovid2Llm.open();
  input.value = prompt;
  if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event("submit"));
}

/* ------------------------------------------------------------- actions */

const TABS = { overview, run: runTab, lanes: lanesTab, data: dataTab, edit: editTab, steward: stewardTab };

async function onBodyClick(e) {
  const g = e.target.closest("[data-goto]");
  if (g) { emit("goto", g.dataset.goto); return; }
  const tg = e.target.closest("[data-tab-go]");
  if (tg) { openTab(tg.dataset.tabGo); return; }
  const ln = e.target.closest("[data-lanes]");
  if (ln) { openTab("lanes", { table: ln.dataset.lanes }); return; }
  const ds = e.target.closest("[data-ds-path]");
  if (ds) { current.extra = { path: ds.dataset.dsPath }; render(); return; }
  const dt = e.target.closest("[data-ds-table]");
  if (dt) { current.extra = { path: current.extra.path, table: dt.dataset.dsTable }; render(); return; }
  const smp = e.target.closest("[data-ds-sample]");
  if (smp) { current.extra = Object.assign({}, current.extra, { sample: true }); render(); return; }
  const rt = e.target.closest("[data-run-table]");
  if (rt) { current.extra.table = rt.dataset.runTable; updateRun(state.byId.get(current.id)); return; }
  const rp = e.target.closest("[data-replay]");
  if (rp) { let j = null; try { j = JSON.parse(rp.dataset.replayJob); } catch (_e) { /* ignore */ } emit("replay", { configId: current.id, job: j }); return; }
  const ask = e.target.closest("[data-steward-ask]");
  if (ask) { const q = $("[data-steward-q]", bodyEl).value.trim(); if (q) askSteward(q); return; }
  const d = e.target.closest("[data-do]");
  if (d) emit("action", { id: current.id, act: d.dataset.do });
}

export function rerender() { render(); }

export { TAB_LABEL, $$ };
