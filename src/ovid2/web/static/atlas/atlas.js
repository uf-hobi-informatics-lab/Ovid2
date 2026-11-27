/*
 * Ovid Atlas (/navigator) — the lane board.
 *
 *   lanes      one line per workflow and per pipeline (layout.js) in groups
 *              (Workflows · Pipelines · Storage accounts · Not used) with a
 *              wide gap between groups, sized to fill the canvas exactly and
 *              re-fitted whenever the canvas changes size (window, dock, side
 *              panel). Lanes shrink to a one-line height before the list
 *              scrolls — inside the canvas only; the page never grows. A run
 *              config is one pipeline component (name · shape · material ·
 *              input → output); a workflow lane lays its components out as
 *              the workflow's DAG with its links drawn in SVG. Cards never
 *              expand in place: selecting one (or one of its parts) opens
 *              everything about it in the dock.
 *   running    "running on top" (default on) floats every lane with a live
 *              job to the top; lanes move with a short FLIP animation.
 *   live       one poll of /api/v1/atlas/live; running beams glow, cards
 *              show clocks synced to server time; ▶ ‖ ■ act through
 *              /api/v1/jobs
 *   dock       everything about the selection (dock.js)
 *   overlays   heat · data health · changed-since-last-success · presence
 *   magic      blast radius (shift-click), drag an engine / location onto a
 *              run config to re-bind it (checked first), replay a past run,
 *              find, ask the Steward about a selection
 *
 * Keyboard: / find · r running · t toggle running-on-top · c clear ·
 * p pin · 1/2/3 toggle layers · Esc clear.
 */

import { LAYERS } from "/static/atlas/kinds.js";
import { state, on, persist, viewState, applyView, setGraph, configsUsing } from "/static/atlas/state.js";
import { buildLanes, orderLanes, laneConfigs, fitLanes, groupLanes, LANE_GAP, LANE_MIN, GROUP_GAP, GROUP_HEAD } from "/static/atlas/layout.js";
import { cardHtml, pipelineHtml, cardStatus, jobFor, jobStatus } from "/static/atlas/cards.js";
import { initDock, show, onLive, currentNode, currentTab, openTab } from "/static/atlas/dock.js";
import { $, $$, api, debounce, enc, esc, fmtClock, itemElapsed, parseTs, serverNow, setServerNow } from "/static/atlas/util.js";

const root = document.getElementById("atlas");
const stage = $("[data-atlas-stage]", root);
const lanesEl = $("[data-atlas-lanes]", root);
const hud = $("[data-atlas-hud]", root);
const dockEl = $("[data-atlas-dock]", root);
const shell = $("[data-atlas-shell]", root);
const POLL_MS = Math.max(1500, Number(root.dataset.pollMs) || 2000);
const CLIENT_ID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()).replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40);
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)");

state.caps = { write: root.dataset.canWrite === "true", run: root.dataset.canRun === "true", data: root.dataset.canViewData === "true" };
setServerNow(root.dataset.serverNowMs);

let presenceMode = "view";
let dragId = null;
let lanes = [];
let fitInfo = { laneH: LANE_MIN, scroll: false, density: "normal" };

/* ------------------------------------------------------------- render */

function workflowLive(name) {
  return (state.live.workflow_runs || []).some((r) => r.workflow === name);
}

const LIVE = new Set(["running", "queued", "paused"]);

function laneLive(lane) {
  if (lane.kind === "workflow" && workflowLive(lane.label)) return true;
  return laneConfigs(lane).some((c) => LIVE.has(cardStatus(state.byId.get(c) || {})));
}

function card(id, density) {
  const n = state.byId.get(id);
  if (!n) return "";
  const st = cardStatus(n);
  const cls = "atl-card is-" + n.kind + " layer-" + n.layer +
    (state.pinned.has(id) ? " is-pinned" : "") + (state.selected === id ? " is-selected" : "") +
    (st ? " st-" + st : "") + (n.meta && (n.meta.missing || n.meta.unused) ? " is-dim" : "");
  const drag = state.caps.write && (n.kind === "engine" || n.kind === "address") ? ' draggable="true"' : "";
  return '<article class="' + cls + '" data-id="' + esc(id) + '" tabindex="0" role="button" aria-label="' + esc(n.kind + " " + n.name) + '"' + drag + ">" + cardHtml(n, density) + "</article>";
}

/*
 * One pipeline component (a run config): name · shape · material ·
 * input → output in one card. ``beam`` is the config's (or the workflow
 * step's) beam; ``opts`` = {step, label, badges, wide}.
 */
function pipeCard(id, beam, density, opts = {}) {
  const n = state.byId.get(id);
  if (!n) return "";
  const st = cardStatus(n);
  const cls = "atl-card atl-pipe is-config layer-algorithm" + (opts.wide ? " is-wide" : "") +
    (state.pinned.has(id) ? " is-pinned" : "") + (state.selected === id ? " is-selected" : "") + (st ? " st-" + st : "");
  return '<article class="' + cls + '" data-id="' + esc(id) + '"' + (opts.step ? ' data-step="' + esc(opts.step) + '"' : "") +
    (opts.style ? ' style="' + opts.style + '"' : "") +
    ' tabindex="0" role="button" aria-label="' + esc("pipeline " + n.name) + '">' + pipelineHtml(n, beam, density, opts) + "</article>";
}

const ON_MARK = { failure: "✗", always: "∀", otherwise: "else" };

/* A workflow lane body: its steps as pipeline components on the DAG grid. */
function workflowBody(lane, density) {
  if (!lane.steps.length) return '<span class="atl-empty-cell mono small">no steps</span>';
  const { pos, cols, rows } = lane.dag;
  const incoming = new Map();
  for (const e of lane.edges) incoming.set(e.to, (incoming.get(e.to) || 0) + 1);
  const cells = lane.steps.map((s) => {
    const p = pos[s.id] || { col: 0, row: 0 };
    const n = state.byId.get(s.config);
    const label = s.label || (n && s.id !== n.name && s.id !== s.config ? s.id : "");
    const badges = s.join === "any" && (incoming.get(s.id) || 0) > 1 ? '<span class="atl-badge is-join" title="runs when any incoming link is taken">any</span>' : "";
    return pipeCard(s.config, lane.beams[s.id], density, { step: s.id, label: label && n ? label : "", badges, style: "grid-column:" + (p.col + 1) + ";grid-row:" + (p.row + 1) });
  }).join("");
  return '<div class="atl-dag" data-dag="' + esc(lane.id) + '" style="--cols:' + cols + ";--rows:" + rows + '">' +
    '<svg class="atl-links" aria-hidden="true"></svg>' + cells + "</div>";
}

function laneHtml(lane, density) {
  const live = laneLive(lane);
  const style = ' style="--w:' + (lane.weight || 1) + '"';
  const open = '<section class="atl-lane is-' + lane.kind + (live ? " is-live" : "") + '" data-lane="' + esc(lane.id) + '"' + style + ">";
  if (lane.kind === "pipeline") {
    return open + '<div class="atl-lane-body">' + pipeCard(lane.head, lane.beam, density, { wide: true }) + "</div></section>";
  }
  const headNode = lane.head && state.byId.get(lane.head);
  const glyph = lane.kind === "workflow" ? "⛓" : lane.kind === "accounts" ? "◫" : "∅";
  const count = lane.kind === "workflow" ? lane.steps.length + " step" + (lane.steps.length === 1 ? "" : "s") : (lane.nodes || []).length + "";
  const head = '<header class="atl-lane-head">' +
    '<span class="atl-lane-glyph">' + glyph + "</span>" +
    (headNode && lane.kind === "workflow" ? card(lane.head, "compact") : '<span class="atl-lane-name mono" title="' + esc(lane.label) + '">' + esc(lane.label) + "</span>") +
    '<span class="atl-lane-count small mono">' + esc(count) + "</span></header>";
  const body = lane.kind === "workflow" ? workflowBody(lane, density)
    : '<div class="atl-pool">' + (lane.nodes || []).map((id) => card(id, "compact")).join("") + "</div>";
  return open + head + '<div class="atl-lane-body">' + body + "</div></section>";
}

function groupHead(g, first) {
  return '<h3 class="atl-group-head mono' + (first ? " is-first" : "") + '" data-group="' + g.key + '">' + esc(g.label) +
    '<span class="atl-group-count">' + g.lanes.length + "</span></h3>";
}

/* Draw each workflow's links between its pipeline components. */
function drawLinks() {
  for (const dag of $$(".atl-dag", lanesEl)) {
    const lane = laneById.get(dag.dataset.dag);
    const svg = $(".atl-links", dag);
    if (!lane || !svg) continue;
    const box = dag.getBoundingClientRect();
    svg.setAttribute("viewBox", "0 0 " + Math.max(1, box.width) + " " + Math.max(1, box.height));
    const rect = new Map();
    for (const el of $$(".atl-pipe[data-step]", dag)) {
      const r = el.getBoundingClientRect();
      rect.set(el.dataset.step, { l: r.left - box.left, r: r.right - box.left, t: r.top - box.top, b: r.bottom - box.top });
    }
    const cfgOf = new Map(lane.steps.map((s) => [s.id, s.config]));
    let html = "";
    const links = lane.edges.length ? lane.edges
      : lane.steps.slice(1).map((s, i) => ({ from: lane.steps[i].id, to: s.id, type: "order", on: "success" }));
    for (const e of links) {
      const a = rect.get(e.from), b = rect.get(e.to);
      if (!a || !b) continue;
      const x1 = a.r, y1 = (a.t + a.b) / 2, x2 = b.l, y2 = (b.t + b.b) / 2;
      const back = x2 < x1 + 4;
      const dx = back ? 40 : Math.max(12, (x2 - x1) / 2);
      const d = "M" + x1 + " " + y1 + " C" + (x1 + dx) + " " + y1 + " " + (x2 - dx) + " " + y2 + " " + (x2 - 3) + " " + y2;
      const st = cardStatus(state.byId.get(cfgOf.get(e.from)) || {});
      const cls = "atl-link is-" + e.type + " on-" + e.on + (st ? " st-" + st : "");
      html += '<path class="' + cls + '" d="' + d + '"><title>' + esc(e.from + " → " + e.to + " · " + e.type + (e.on !== "success" ? " · on " + e.on : "")) + "</title></path>";
      html += '<path class="' + cls + ' is-head" d="M' + (x2 - 7) + " " + (y2 - 4) + " L" + x2 + " " + y2 + " L" + (x2 - 7) + " " + (y2 + 4) + 'z"/>';
      if (ON_MARK[e.on]) {
        const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
        html += '<text class="atl-link-label on-' + e.on + '" x="' + mx + '" y="' + (my - 4) + '" text-anchor="middle">' + esc(ON_MARK[e.on]) + "</text>";
      }
    }
    svg.innerHTML = html;
  }
}

let laneById = new Map();

function render() {
  const first = captureTops();
  const built = buildLanes(state.graph, { layers: state.layers });
  lanes = orderLanes(built, { floatRunning: state.floatRunning, isLive: laneLive });
  laneById = new Map(lanes.map((l) => [l.id, l]));
  const groups = groupLanes(lanes);
  const H = lanesEl.clientHeight || stage.clientHeight || 600;
  fitInfo = fitLanes(lanes.map((l) => l.weight || 1), H, { groups: groups.length });
  root.style.setProperty("--lane-min", LANE_MIN + "px");
  root.style.setProperty("--lane-gap", LANE_GAP + "px");
  root.style.setProperty("--group-gap", GROUP_GAP + "px");
  root.style.setProperty("--group-head", GROUP_HEAD + "px");
  lanesEl.dataset.density = fitInfo.density;
  lanesEl.classList.toggle("is-scrolling", fitInfo.scroll);
  const scrollTop = lanesEl.scrollTop;
  lanesEl.innerHTML = lanes.length
    ? groups.map((g, i) => groupHead(g, i === 0) + g.lanes.map((l) => laneHtml(l, fitInfo.density)).join("")).join("")
    : '<p class="empty">Nothing here yet — save a run config to see it as a lane.</p>';
  lanesEl.scrollTop = scrollTop;
  drawLinks();
  flip(first);
  applyMatches();
  applyImpact();
  applyProposals();
  tickClocks();
  root.dataset.mode = "lanes";
}

/* FLIP: lanes that changed position glide to their new slot. */
function captureTops() {
  const m = new Map();
  for (const el of $$(".atl-lane", lanesEl)) m.set(el.dataset.lane, el.getBoundingClientRect().top);
  return m;
}

function flip(first) {
  if (REDUCED.matches || !first.size) return;
  for (const el of $$(".atl-lane", lanesEl)) {
    const was = first.get(el.dataset.lane);
    if (was == null) continue;
    const dy = was - el.getBoundingClientRect().top;
    if (Math.abs(dy) < 2) continue;
    el.animate([{ transform: "translateY(" + dy + "px)" }, { transform: "none" }], { duration: 320, easing: "cubic-bezier(.2,.8,.2,1)" });
  }
}

/* Every element standing for a node: cards and pipeline-component parts. */
function nodeEls() {
  return $$(".atl-card, [data-part-id]", lanesEl);
}
const elId = (el) => el.dataset.partId || el.dataset.id;

function applyProposals() {
  for (const el of nodeEls()) el.classList.toggle("is-proposed", state.proposals.has(elId(el)));
}

/* Scroll the lane holding ``id`` into view (only the lane list scrolls). */
function scrollToNode(id) {
  const el = lanesEl.querySelector('.atl-card[data-id="' + CSS.escape(id) + '"], [data-part-id="' + CSS.escape(id) + '"]');
  if (!el) {
    const n = state.byId.get(id);
    if (n && !state.layers.has(n.layer)) { state.layers.add(n.layer); persist(); syncBar(); render(); return scrollToNode(id); }
    return;
  }
  const lane = el.closest(".atl-lane") || el;
  lane.scrollIntoView({ block: "nearest", behavior: REDUCED.matches ? "auto" : "smooth" });
  el.classList.add("is-flash");
  window.setTimeout(() => el.classList.remove("is-flash"), 900);
}

/* --------------------------------------------------------- selection */

function select(id, { tab, extra, fly = false, push = true } = {}) {
  state.selected = id;
  for (const el of $$(".is-selected", lanesEl)) el.classList.remove("is-selected");
  if (id) for (const el of $$('.atl-card[data-id="' + CSS.escape(id) + '"], [data-part-id="' + CSS.escape(id) + '"]', lanesEl)) el.classList.add("is-selected");
  if (id) show(id, tab, extra);
  if (fly && id) scrollToNode(id);
  setUrl(push);
  beatPresence();
}

function setUrl(push) {
  const qs = new URLSearchParams();
  if (state.selected) qs.set("node", state.selected);
  const t = currentTab();
  if (state.selected && t && t !== "overview") qs.set("tab", t);
  const url = "/navigator" + (qs.toString() ? "?" + qs : "");
  if (url !== location.pathname + location.search) (push ? history.pushState : history.replaceState).call(history, null, "", url);
}

function clearAll() {
  state.impact = null;
  state.search = "";
  const s = $("[data-atlas-search]", root);
  if (s) s.value = "";
  hud.textContent = "";
  render();
  lanesEl.scrollTop = 0;
}

/* --------------------------------------------------------------- live */

let liveTimer = 0;
async function pollLive() {
  window.clearTimeout(liveTimer);
  try {
    const live = await api("GET", "/atlas/live");
    setServerNow(live.server_now_ms);
    state.live = live;
    state.liveByConfig = new Map();
    for (const j of live.jobs) {
      if (!j.config) continue;
      const id = "config:" + j.config;
      if (!state.liveByConfig.has(id)) state.liveByConfig.set(id, []);
      state.liveByConfig.get(id).push(j);
    }
    if (!state.replay && !dragId) { render(); onLive(); }
    renderTransport();
  } catch (_e) { /* keep the last state; retry */ }
  const anyLive = state.live.jobs.some((j) => j.status === "running" || j.status === "queued");
  liveTimer = window.setTimeout(pollLive, document.hidden ? POLL_MS * 4 : anyLive ? POLL_MS : POLL_MS * 2);
}

function renderTransport() {
  const box = $("[data-atlas-transport]", root);
  const live = state.live.jobs.filter((j) => j.status === "running" || j.status === "queued");
  box.hidden = !live.length;
  box.innerHTML = live.map((j) => {
    const st = jobStatus(j);
    const it = (j.items || []).find((i) => i.status === "running");
    return '<button type="button" class="atl-tchip st-' + st + '" data-goto-config="' + esc(j.config ? "config:" + j.config : "") + '" title="' + esc(j.pipeline + " · " + st) + '">' +
      '<span class="table-row-dot status-' + (st === "paused" ? "waiting" : st) + '"></span><span class="mono">' + esc(j.config || j.pipeline) + "</span>" +
      (it ? '<span class="mono small">' + esc(it.item) + "</span>" : "") +
      '<span class="atl-clock mono is-live" data-clock-start="' + esc(j.started_at || "") + '" data-clock-paused="' + (st === "paused" ? "1" : "") + '"></span></button>';
  }).join("");
  $("[data-atlas-focus-running]", root).classList.toggle("is-live", live.length > 0);
  const mark = $(".atlas-wordmark", root);
  if (mark) mark.dataset.status = live.some((j) => j.status === "running") ? "running" : live.length ? "queued" : "idle";
}

/* Clocks tick on requestAnimationFrame from server-synced times. */
const pausedAt = new Map();
function tickClocks() {
  const now = state.replay ? state.replay.t : serverNow();
  for (const el of $$("[data-clock-start]", root)) {
    const s = parseTs(el.dataset.clockStart);
    if (s == null) { el.textContent = ""; continue; }
    const end = parseTs(el.dataset.clockEnd);
    let t = end != null ? end : now;
    if (el.dataset.clockPaused) { if (!pausedAt.has(el.dataset.clockStart)) pausedAt.set(el.dataset.clockStart, now); t = pausedAt.get(el.dataset.clockStart); }
    else pausedAt.delete(el.dataset.clockStart);
    el.textContent = fmtClock(t - s);
  }
  for (const el of $$("[data-item-clock]", root)) {
    let row;
    try { row = JSON.parse(el.dataset.itemClock); } catch (_e) { continue; }
    el.textContent = fmtClock(itemElapsed(row, state.replay ? state.replay.t : null));
  }
}
function loop() { tickClocks(); requestAnimationFrame(loop); }

/* ------------------------------------------------------------ actions */

async function act(id, what) {
  const n = state.byId.get(id);
  if (!n) return;
  const job = n.kind === "config" ? jobFor(id) : null;
  const say = (msg, cls) => { hud.textContent = msg; hud.className = "atlas-hud small mono" + (cls ? " " + cls : ""); window.setTimeout(() => { if (hud.textContent === msg) hud.textContent = ""; }, 6000); };
  try {
    if (what === "run") {
      let body = {};
      try {
        await api("POST", "/pipelines/" + enc(n.name) + "/run", body);
      } catch (err) {
        if (err.message !== "overwrite_required") throw err;
        const tables = (err.detail && err.detail.tables) || [];
        if (!window.confirm("The output already holds " + tables.length + " table(s)" + (tables.length ? " (" + tables.slice(0, 6).join(", ") + (tables.length > 6 ? ", …" : "") + ")" : "") + ".\nOverwrite them?")) return;
        body = { confirm_overwrite: true };
        await api("POST", "/pipelines/" + enc(n.name) + "/run", body);
      }
      say("▶ " + n.name + " queued");
    } else if (what === "pause" && job) { await api("POST", "/jobs/" + enc(job.job_id) + "/pause"); say("‖ " + n.name + " pauses after the current table"); }
    else if (what === "resume" && job) { await api("POST", "/jobs/" + enc(job.job_id) + "/resume"); say("▶ " + n.name + " resumed"); }
    else if (what === "cancel" && job) {
      if (!window.confirm("Cancel the running job of " + n.name + "?")) return;
      await api("POST", "/jobs/" + enc(job.job_id) + "/cancel"); say("■ " + n.name + " cancelling");
    } else if (what === "run-workflow") { await api("POST", "/workflows/" + enc(n.name) + "/run", {}); say("▶ workflow " + n.name + " started"); }
    else if (what === "impact") { await showImpact(id); return; }
    pollLive();
  } catch (err) { say(err.message, "is-err"); }
}

/* -------------------------------------------------------------- impact */

async function showImpact(id) {
  try {
    const r = await api("GET", "/atlas/impact?node=" + enc(id));
    state.impact = { root: id, nodes: new Set(r.nodes), edges: new Set(r.edges) };
  } catch (_e) { state.impact = null; }
  applyImpact();
  hud.textContent = state.impact ? "blast radius of " + id + ": " + (state.impact.nodes.size - 1) + " downstream node(s) — Esc to clear" : "";
}

function applyImpact() {
  root.classList.toggle("has-impact", !!state.impact);
  for (const el of nodeEls()) {
    el.classList.toggle("is-impact", !!state.impact && state.impact.nodes.has(elId(el)));
    el.classList.toggle("is-impact-root", !!state.impact && elId(el) === state.impact.root);
  }
}

/* -------------------------------------------------------------- search */

function applyMatches() {
  const q = state.search.trim().toLowerCase();
  root.classList.toggle("has-search", !!q);
  for (const el of nodeEls()) {
    const n = state.byId.get(elId(el));
    const hit = q && n && (n.name.toLowerCase().includes(q) || (n.label || "").toLowerCase().includes(q) || JSON.stringify(n.meta).toLowerCase().includes(q));
    el.classList.toggle("is-match", !!hit);
  }
  for (const el of $$(".atl-lane", lanesEl)) el.classList.toggle("is-match", !!q && !!el.querySelector(".is-match"));
}

function searchFirst() {
  const q = state.search.trim().toLowerCase();
  if (!q) return;
  const hit = state.graph.nodes.find((n) => n.name.toLowerCase() === q) || state.graph.nodes.find((n) => n.name.toLowerCase().includes(q));
  if (hit) select(hit.id, { fly: true });
}

/* ------------------------------------------------------------ overlays */

async function loadOverlays() {
  const jobs = [];
  if (state.overlays.has("heat") && !state.heat) jobs.push(api("GET", "/atlas/overlays/heat").then((r) => { state.heat = r; }));
  if (state.overlays.has("health") && !state.health) jobs.push(api("GET", "/atlas/overlays/health").then((r) => { state.health = r; }));
  if (state.overlays.has("drift") && !state.drift) jobs.push(api("GET", "/atlas/overlays/drift").then((r) => { state.drift = r; }));
  await Promise.allSettled(jobs);
  render();
}

/* ------------------------------------------------------------ presence */

async function beatPresence() {
  try {
    state.presence = await api("POST", "/atlas/presence", { client_id: CLIENT_ID, node: state.selected, mode: presenceMode });
  } catch (_e) { return; }
  const box = $("[data-atlas-presence]", root);
  const people = new Map();
  for (const p of state.presence) if (!people.has(p.user_id)) people.set(p.user_id, p);
  box.innerHTML = state.overlays.has("presence") ? [...people.values()].map((p) =>
    '<button type="button" class="atl-avatar' + (p.mode === "edit" ? " is-edit" : "") + '" data-goto-node="' + esc(p.node || "") + '" title="' + esc(p.display_name + (p.node ? (p.mode === "edit" ? " is editing " : " is looking at ") + p.node : "")) + '">' +
    esc((p.display_name || "?").slice(0, 1).toUpperCase()) + "</button>").join("") : "";
  render();
}

/* ------------------------------------------------------------- replay */

async function startReplay({ configId, job }) {
  if (!job || !job.instance_id) return;
  let r;
  try { r = await api("GET", "/atlas/replay/" + enc(job.instance_id)); } catch (err) { hud.textContent = err.message; return; }
  const t0 = parseTs(r.job.started_at) || Math.min(...r.items.map((i) => parseTs(i.started_at) || Infinity));
  const t1 = parseTs(r.job.ended_at) || Math.max(...r.items.map((i) => parseTs(i.ended_at) || 0));
  if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) { hud.textContent = "nothing to replay"; return; }
  state.replay = { configId, job: r.job, items: r.items, t0, t1, t: t0, playing: true };
  const sc = $("[data-atlas-scrubber]", root);
  sc.hidden = false;
  scrollToNode(configId);
  replayFrame();
  let last = performance.now();
  const speed = (t1 - t0) / 20000; // a whole run in ~20 s
  const step = (now) => {
    if (!state.replay) return;
    if (state.replay.playing) {
      state.replay.t = Math.min(t1, state.replay.t + (now - last) * speed);
      $("[data-scrub-range]", root).value = String(Math.round(((state.replay.t - t0) / (t1 - t0)) * 1000));
      replayFrame();
      if (state.replay.t >= t1) state.replay.playing = false;
    }
    last = now;
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

/* Rebuild the config's job + items as they were at replay time ``t``. */
function replayFrame() {
  const R = state.replay;
  if (!R) return;
  const t = R.t;
  const at = (row) => {
    const s = parseTs(row.started_at), e = parseTs(row.ended_at);
    if (s == null || s > t) return null;
    if (e != null && e <= t) return { ...row, active_since: null };
    return { ...row, status: "running", ended_at: null, active_since: row.started_at, elapsed_ms: 0 };
  };
  const items = R.items.map(at).filter(Boolean);
  const job = { ...R.job, status: t >= R.t1 ? R.job.status : "running", ended_at: t >= R.t1 ? R.job.ended_at : null,
    items: items.filter((i) => i.kind === "table" || i.kind === "check"), stages: items.filter((i) => i.kind === "stage"), tables: [], pausable: false, paused: false };
  state.liveByConfig.set(R.configId, [job]);
  $("[data-scrub-clock]", root).textContent = "replay " + fmtClock(t - R.t0) + " / " + fmtClock(R.t1 - R.t0);
  render();
  if (currentNode() === R.configId && currentTab() === "run") onLive();
}

function stopReplay() {
  state.replay = null;
  $("[data-atlas-scrubber]", root).hidden = true;
  pollLive();
}

/* -------------------------------------------------------- drag rebind */

function wireDrag() {
  lanesEl.addEventListener("dragstart", (e) => {
    const card = e.target.closest("[data-part-id][draggable], .atl-card[draggable]");
    if (!card) return;
    dragId = card.dataset.partId || card.dataset.id;
    e.stopPropagation();
    e.dataTransfer.setData("text/plain", dragId);
    e.dataTransfer.effectAllowed = "link";
    root.classList.add("is-dragging");
  });
  lanesEl.addEventListener("dragend", () => { dragId = null; root.classList.remove("is-dragging"); $$(".is-droptarget", root).forEach((x) => x.classList.remove("is-droptarget", "is-refused")); });
  lanesEl.addEventListener("dragover", (e) => {
    const card = e.target.closest(".atl-card.is-config");
    if (!card || !dragId) return;
    e.preventDefault();
    $$(".is-droptarget", root).forEach((x) => { if (x !== card) x.classList.remove("is-droptarget"); });
    card.classList.add("is-droptarget");
  });
  lanesEl.addEventListener("drop", async (e) => {
    const card = e.target.closest(".atl-card.is-config");
    if (!card || !dragId) return;
    e.preventDefault();
    const src = state.byId.get(dragId), cfg = state.byId.get(card.dataset.id);
    let role = "engine";
    if (src.kind === "address") {
      role = window.confirm("Bind " + src.name + " as the INPUT of " + cfg.name + "?\n(Cancel = bind it as the OUTPUT)") ? "input" : "output";
    }
    const check = await api("POST", "/atlas/rebind:check", { config: cfg.name, role, target: src.name }).catch((err) => ({ ok: false, reason: err.message }));
    if (!check.ok) {
      card.classList.add("is-refused");
      hud.textContent = "✗ " + check.reason;
      hud.className = "atlas-hud small mono is-err";
      window.setTimeout(() => card.classList.remove("is-refused", "is-droptarget"), 1600);
      return;
    }
    if (!window.confirm("Re-bind " + cfg.name + ": " + role + " → " + src.name + "?\n(checked: the engine supports every step and the storage pair is valid)")) return;
    try {
      const d = await api("GET", "/pipelines/" + enc(cfg.name));
      const { pipelineWrite } = await import("/static/atlas/dock.js");
      await api("PUT", "/pipelines/" + enc(cfg.name), Object.assign(pipelineWrite(d), check.patch));
      hud.textContent = "✓ " + cfg.name + " " + role + " → " + src.name;
      await refreshGraph();
    } catch (err) { hud.textContent = err.message; }
  });
}

/* -------------------------------------------------------- dock resize */

function applyDock() {
  shell.dataset.side = state.dock.side;
  shell.classList.toggle("dock-min", !!state.dock.min);
  shell.classList.toggle("dock-max", !!state.dock.max);
  shell.style.setProperty("--dock-size", Math.max(160, state.dock.size) + "px");
}

function wireDock() {
  const rz = $("[data-atlas-resizer]", root);
  let drag = null;
  rz.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, s: state.dock.size }; rz.setPointerCapture(e.pointerId); });
  rz.addEventListener("pointermove", (e) => {
    if (!drag) return;
    state.dock.min = false; state.dock.max = false;
    state.dock.size = state.dock.side === "bottom" ? drag.s - (e.clientY - drag.y) : drag.s - (e.clientX - drag.x);
    state.dock.size = Math.max(160, Math.min(state.dock.side === "bottom" ? window.innerHeight - 200 : window.innerWidth - 360, state.dock.size));
    applyDock();
  });
  rz.addEventListener("pointerup", () => { if (drag) { drag = null; persist(); } });
  rz.addEventListener("keydown", (e) => {
    const d = (e.key === "ArrowUp" || e.key === "ArrowLeft") ? 40 : (e.key === "ArrowDown" || e.key === "ArrowRight") ? -40 : 0;
    if (!d) return;
    e.preventDefault();
    state.dock.size += d; applyDock(); persist();
  });
  $("[data-atlas-dock-side]", root).addEventListener("click", () => { state.dock.side = state.dock.side === "bottom" ? "right" : "bottom"; state.dock.size = state.dock.side === "bottom" ? 340 : 520; applyDock(); persist(); });
  $("[data-atlas-dock-max]", root).addEventListener("click", () => { state.dock.max = !state.dock.max; state.dock.min = false; applyDock(); persist(); });
  $("[data-atlas-dock-min]", root).addEventListener("click", () => { state.dock.min = !state.dock.min; state.dock.max = false; applyDock(); persist(); });
  applyDock();
}

/* -------------------------------------------------------------- views */

async function loadViews() {
  const sel = $("[data-atlas-view]", root);
  let views = [];
  try { views = await api("GET", "/atlas/layouts"); } catch (_e) { views = []; }
  sel.innerHTML = '<option value="">—</option>' + views.map((v) => '<option value="' + esc(v.name) + '"' + (v.name === state.view ? " selected" : "") + ">" + esc(v.name) + " · " + esc(v.updated_by) + "</option>").join("");
  sel._views = views;
  const del = $("[data-atlas-delete-view]", root);
  if (del) del.hidden = !state.view;
}

function wireViews() {
  const sel = $("[data-atlas-view]", root);
  sel.addEventListener("change", () => {
    const v = (sel._views || []).find((x) => x.name === sel.value);
    state.view = sel.value;
    if (v) { applyView(v.layout); syncBar(); applyDock(); persist(); render(); }
    const del = $("[data-atlas-delete-view]", root);
    if (del) del.hidden = !state.view;
  });
  const save = $("[data-atlas-save-view]", root);
  if (save) save.addEventListener("click", async () => {
    const name = window.prompt("Save this view for everyone in the workspace as:", state.view || "");
    if (!name) return;
    try { await api("PUT", "/atlas/layouts/" + enc(name), { layout: viewState() }); state.view = name; await loadViews(); hud.textContent = "view '" + name + "' saved for the workspace"; }
    catch (err) { hud.textContent = err.message; }
  });
  const del = $("[data-atlas-delete-view]", root);
  if (del) del.addEventListener("click", async () => {
    if (!state.view || !window.confirm("Delete the saved view '" + state.view + "'?")) return;
    try { await api("DELETE", "/atlas/layouts/" + enc(state.view)); state.view = ""; await loadViews(); } catch (err) { hud.textContent = err.message; }
  });
}

/* ---------------------------------------------------------------- bar */

function syncBar() {
  for (const b of $$("[data-layer-toggle]", root)) {
    const on = state.layers.has(b.dataset.layerToggle);
    b.classList.toggle("is-on", on);
    b.setAttribute("aria-pressed", String(on));
  }
  const fr = $("[data-atlas-float-running]", root);
  if (fr) fr.checked = !!state.floatRunning;
  for (const cb of $$("[data-overlay]", root)) cb.checked = state.overlays.has(cb.dataset.overlay);
}

function toggleLayer(layer, isolate) {
  if (isolate) state.layers = new Set([layer]);
  else if (state.layers.has(layer) && state.layers.size > 1) state.layers.delete(layer);
  else state.layers.add(layer);
  persist(); syncBar(); render();
}

function setFloat(on) {
  state.floatRunning = !!on;
  persist(); syncBar(); render();
  if (on) lanesEl.scrollTo({ top: 0, behavior: REDUCED.matches ? "auto" : "smooth" });
}

/* ------------------------------------------------------------- events */

function wireCanvas() {
  lanesEl.addEventListener("click", (e) => {
    const cardEl = e.target.closest(".atl-card");
    if (!cardEl) return;
    const partEl = e.target.closest("[data-part-id]");
    const id = partEl && !e.target.closest("[data-act]") ? partEl.dataset.partId : cardEl.dataset.id;
    const a = e.target.closest("[data-act]");
    if (a) {
      e.stopPropagation();
      const what = a.dataset.act;
      if (["run", "pause", "resume", "cancel"].includes(what)) { act(cardEl.dataset.id, what); return; }
    }
    if (e.shiftKey) { showImpact(id); return; }
    const n = state.byId.get(id);
    select(id, { tab: n && n.kind === "config" && LIVE.has(cardStatus(n)) ? "run" : undefined });
  });
  lanesEl.addEventListener("keydown", (e) => {
    const cardEl = e.target.closest(".atl-card");
    if (cardEl && (e.key === "Enter" || e.key === " ") && !e.target.closest("button")) { e.preventDefault(); select(cardEl.dataset.id); }
  });
  let hoverT = 0;
  lanesEl.addEventListener("mouseover", (e) => {
    const cardEl = e.target.closest("[data-part-id], .atl-card");
    window.clearTimeout(hoverT);
    if (!cardEl || !e.altKey) return;
    hoverT = window.setTimeout(() => showImpact(elId(cardEl)), 120);
  });
  lanesEl.addEventListener("click", (e) => {
    if (!e.target.closest(".atl-card") && state.impact) { state.impact = null; applyImpact(); hud.textContent = ""; }
  });
  root.addEventListener("click", (e) => {
    const g = e.target.closest("[data-goto-config], [data-goto-node]");
    if (g) { const id = g.dataset.gotoConfig || g.dataset.gotoNode; if (id) select(id, { fly: true, tab: g.dataset.gotoConfig ? "run" : undefined }); }
  });
  for (const b of $$("[data-layer-toggle]", root)) {
    b.addEventListener("click", (e) => toggleLayer(b.dataset.layerToggle, e.altKey));
    b.addEventListener("dblclick", () => toggleLayer(b.dataset.layerToggle, true));
  }
  const fr = $("[data-atlas-float-running]", root);
  if (fr) fr.addEventListener("change", () => setFloat(fr.checked));
  $("[data-atlas-collapse]", root).addEventListener("click", clearAll);
  $("[data-atlas-focus-running]", root).addEventListener("click", focusRunning);
  for (const cb of $$("[data-overlay]", root)) cb.addEventListener("change", () => {
    if (cb.checked) state.overlays.add(cb.dataset.overlay); else state.overlays.delete(cb.dataset.overlay);
    persist(); loadOverlays(); beatPresence();
  });
  const search = $("[data-atlas-search]", root);
  search.addEventListener("input", debounce(() => { state.search = search.value; applyMatches(); }, 100));
  search.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); searchFirst(); } if (e.key === "Escape") { search.value = ""; state.search = ""; applyMatches(); search.blur(); } });
  $("[data-scrub-close]", root).addEventListener("click", stopReplay);
  $("[data-scrub-play]", root).addEventListener("click", () => { if (state.replay) { if (state.replay.t >= state.replay.t1) state.replay.t = state.replay.t0; state.replay.playing = !state.replay.playing; } });
  $("[data-scrub-range]", root).addEventListener("input", (e) => {
    if (!state.replay) return;
    state.replay.playing = false;
    state.replay.t = state.replay.t0 + (Number(e.target.value) / 1000) * (state.replay.t1 - state.replay.t0);
    replayFrame();
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, textarea, select, [contenteditable], .cm-editor")) return;
    if (e.metaKey || e.ctrlKey) return;
    const k = e.key;
    if (k === "/") { e.preventDefault(); search.focus(); }
    else if (k === "c") clearAll();
    else if (k === "r") focusRunning();
    else if (k === "t") setFloat(!state.floatRunning);
    else if (k === "p" && state.selected) { if (state.pinned.has(state.selected)) state.pinned.delete(state.selected); else state.pinned.add(state.selected); persist(); render(); }
    else if (k === "1" || k === "2" || k === "3") toggleLayer(LAYERS[Number(k) - 1], e.altKey);
    else if (k === "Escape") { state.impact = null; applyImpact(); hud.textContent = ""; if (state.replay) stopReplay(); }
  });
  window.addEventListener("popstate", () => {
    const qs = new URLSearchParams(location.search);
    select(qs.get("node") || null, { tab: qs.get("tab") || undefined, push: false, fly: true });
  });
  // Re-fit whenever the canvas changes size: window, dock, assistant panel.
  let lastH = 0, lastW = 0;
  const refit = debounce(() => {
    const H = lanesEl.clientHeight, W = lanesEl.clientWidth;
    if (H === lastH && W === lastW) return;
    lastH = H; lastW = W;
    render();
  }, 60);
  if (window.ResizeObserver) new ResizeObserver(refit).observe(lanesEl);
  else window.addEventListener("resize", refit);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollLive(); refreshGraph(); } });
  window.addEventListener("beforeunload", () => {
    try { navigator.sendBeacon && navigator.sendBeacon("/api/v1/atlas/presence", new Blob([JSON.stringify({ client_id: CLIENT_ID, node: null, mode: "view" })], { type: "application/json" })); } catch (_e) { /* ignore */ }
  });
}

function focusRunning() {
  const live = lanes.filter(laneLive);
  if (!live.length) { hud.textContent = "nothing is running"; return; }
  const lane = live[0];
  const step = lane.kind === "workflow" ? lane.steps.find((s) => LIVE.has(cardStatus(state.byId.get(s.config) || {}))) : null;
  const id = step ? step.config : lane.head;
  select(id, { tab: state.byId.get(id) && state.byId.get(id).kind === "config" ? "run" : undefined, fly: true });
}

/* --------------------------------------------------- graph lifecycle */

async function refreshGraph() {
  try {
    const g = await api("GET", "/atlas");
    if (g.digest !== state.graph.digest) { setGraph(g); render(); }
  } catch (_e) { /* keep the last graph */ }
}

/* Proposals from the assistant panel → highlight the nodes they touch. */
function wireSteward() {
  const canvas = document.getElementById("llm-canvas");
  if (!canvas) return;
  const scan = () => {
    state.proposals.clear();
    for (const card of $$(".llm-proposal:not(.done):not(.failed):not(.dismissed)", canvas)) {
      const txt = card.textContent || "";
      for (const n of state.graph.nodes) {
        const hit = (n.kind === "config" && (txt.includes("/pipelines/" + n.name) || txt.includes('"' + n.name + '"'))) ||
          (n.kind === "workflow" && txt.includes("/workflows/" + n.name)) ||
          (n.kind === "package" && txt.includes("/pipeline-packages/" + n.name));
        if (hit) { if (!state.proposals.has(n.id)) state.proposals.set(n.id, []); state.proposals.get(n.id).push(txt.slice(0, 200)); }
      }
    }
    applyProposals();
  };
  new MutationObserver(debounce(scan, 250)).observe(canvas, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
}


on("goto", (id) => select(id, { fly: true }));
on("action", ({ id, act: what }) => act(id, what));
on("replay", startReplay);
on("refresh-graph", () => { state.graph.digest = ""; refreshGraph(); });
on("refresh-node", () => render());
on("presence-mode", (m) => { if (m !== presenceMode) { presenceMode = m; beatPresence(); } });

function boot() {
  let bootData = null;
  try { bootData = JSON.parse($("#atlas-boot").textContent); } catch (_e) { bootData = null; }
  if (bootData && bootData.graph) setGraph(bootData.graph);
  syncBar();
  initDock(dockEl);
  wireCanvas();
  wireDock();
  wireDrag();
  wireViews();
  wireSteward();
  render();
  // Deep links: ?node= / ?p=&t=&f= (the historical navigator links).
  const qs = new URLSearchParams(location.search);
  const node = root.dataset.initialNode || qs.get("node");
  const p = root.dataset.initialP, t = root.dataset.initialT, f = root.dataset.initialF;
  if (node && state.byId.has(node)) select(node, { tab: qs.get("tab") || undefined, fly: true, push: false });
  else if (p && state.byId.has("package:" + p)) {
    state.layers.add("algorithm");
    select("package:" + p, { tab: t ? "lanes" : "overview", extra: t ? { table: t, field: f } : {}, fly: true, push: false });
  }
  pollLive();
  loadOverlays();
  loadViews();
  beatPresence();
  window.setInterval(beatPresence, 15000);
  window.setInterval(refreshGraph, 30000);
  requestAnimationFrame(loop);
}

if (root) {
  try { boot(); } catch (err) {
    stage.insertAdjacentHTML("beforeend", '<p class="flash error">Atlas failed to start: ' + esc(err.message) + "</p>");
  }
}

export { configsUsing, openTab };
