/*
 * Workflow canvas editor — /workflows/new and /workflows/<name>.
 *
 * State is one plain object (the WorkflowWrite shape plus a name); every
 * mutation goes through ``commit()`` which snapshots it for undo / redo,
 * re-renders the SVG and the inspector, and marks the page dirty.
 * Persisting goes through the JSON API (POST / PUT /api/v1/workflows/<n>),
 * validation through POST /api/v1/workflows/<n>/validate (errors are
 * pinned on their nodes). Field relevance in the inspector is handled by
 * field_visibility.js (UI rule U1) via hidden inputs + data-show-when;
 * kind behaviour comes from the kind-flags island (Ovid2Visibility.kindSpec)
 * — never from kind-name literals.
 */
import {
  NODE_H, NODE_W, PanZoom, autoLayout, bounds, curve, drawGraph, esc, layout, mountCanvas, svgEl,
} from "/static/workflow_graph.js";
import { ConditionBuilder, summarise } from "/static/workflow_conditions.js";

const root = document.getElementById("wf-editor");
const CAN_WRITE = root.dataset.canWrite === "true";
const CAN_RUN = root.dataset.canRun === "true";
const catalog = JSON.parse(document.getElementById("wf-catalog").textContent || "{}");
const initial = JSON.parse(document.getElementById("wf-initial").textContent || "null");
const PIPES = Object.fromEntries((catalog.pipelines || []).map((p) => [p.name, p]));
const ADDRS = Object.fromEntries((catalog.addresses || []).map((a) => [a.name, a]));
const CONDS = catalog.conditions || { fields: [], ops: {}, ops_by_type: {}, probe_aggs: [] };
const API = "/api/v1";
const $ = (sel, el = root) => el.querySelector(sel);
const $$ = (sel, el = root) => Array.from(el.querySelectorAll(sel));

/* ------------------------------------------------------------------ state */

let state = {
  name: root.dataset.name || "",
  description: (initial && initial.description) || "",
  enabled: initial ? !!initial.enabled : true,
  trigger: (initial && initial.trigger) || { type: "manual" },
  steps: (initial && initial.steps) || [],
  edges: (initial && initial.edges) || [],
};
let savedJson = JSON.stringify(state);
let selected = null; // {kind: "step", id} | {kind: "edge", id: index}
let errors = {};
let triggerTouched = !!initial;
const undo = [];
const redo = [];

const clone = (o) => JSON.parse(JSON.stringify(o));
const isNew = () => !root.dataset.name;

function commit(mutator, { record = true } = {}) {
  if (record) {
    undo.push(clone(state));
    if (undo.length > 100) undo.shift();
    redo.length = 0;
  }
  mutator(state);
  render();
}

function dirty() {
  return JSON.stringify(state) !== savedJson;
}

/* ------------------------------------------------------------------ helpers */

function kindSpec(kind) {
  return (window.Ovid2Visibility && window.Ovid2Visibility.kindSpec(kind)) ||
    (catalog.kinds || []).find((k) => k.name === kind) || null;
}

function nextStepId(pipeline) {
  const base = String(pipeline || "step").toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^[^a-z0-9]+/, "").slice(0, 30) || "step";
  let id = base;
  let n = 2;
  const used = new Set(state.steps.map((s) => s.id));
  while (used.has(id)) id = `${base}_${n++}`;
  return id;
}

function dataParent(stepId) {
  const e = state.edges.find((x) => x.to === stepId && x.type === "data");
  return e ? e.from : null;
}

/** A step may take several data links only when it joins ANY (one branch runs). */
function canTakeAnotherDataLink(stepId) {
  const step = state.steps.find((s) => s.id === stepId);
  return !dataParent(stepId) || (step && step.join === "any");
}

function incomingCount(stepId) {
  return state.edges.filter((e) => e.to === stepId).length;
}

function resolveIO(step) {
  const pipe = PIPES[step.pipeline] || {};
  const ov = step.overrides || {};
  const parent = dataParent(step.id);
  let input = ov.input_address || pipe.input_address || "";
  let inputFrom = null;
  if (parent) {
    const ps = state.steps.find((s) => s.id === parent);
    inputFrom = ps ? (ps.label || ps.pipeline) : parent;
    input = ps ? resolveIO(ps).output : "";
  }
  return { input, inputFrom, output: ov.output_address || pipe.output_address || "" };
}

function wouldCycle(from, to) {
  if (from === to) return true;
  const kids = new Map(state.steps.map((s) => [s.id, []]));
  for (const e of state.edges) kids.get(e.from) && kids.get(e.from).push(e.to);
  const stack = [to];
  const seen = new Set();
  while (stack.length) {
    const n = stack.pop();
    if (n === from) return true;
    if (seen.has(n)) continue;
    seen.add(n);
    (kids.get(n) || []).forEach((k) => stack.push(k));
  }
  return false;
}

function flash(msg, kind = "ok") {
  const ok = $("[data-wf-flash]");
  const err = $("[data-wf-error]");
  ok.hidden = err.hidden = true;
  if (!msg) return;
  const el = kind === "ok" ? ok : err;
  el.textContent = msg;
  el.hidden = false;
  if (kind === "ok") window.setTimeout(() => { el.hidden = true; }, 4000);
}

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  let data = null;
  try { data = await r.json(); } catch (_e) { data = null; }
  if (!r.ok || r.status === 202) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d
      : d && d.status === "awaiting_approval"
        ? `Awaiting human approval (${d.approval_id}) — approve it on the Approvals page, then retry.`
        : JSON.stringify(d || data || r.statusText);
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return data;
}

function payload() {
  const trig = { type: state.trigger.type || "manual" };
  if (trig.type === "cron") trig.cron = state.trigger.cron || "";
  if (trig.type === "interval") trig.interval_minutes = Number(state.trigger.interval_minutes) || null;
  if (trig.type === "watch" && state.trigger.watch_min_wait_minutes !== "" && state.trigger.watch_min_wait_minutes != null) {
    trig.watch_min_wait_minutes = Number(state.trigger.watch_min_wait_minutes);
  }
  const pos = layout(state.steps, state.edges);
  return {
    description: state.description || "",
    enabled: !!state.enabled,
    trigger: trig,
    steps: state.steps.map((s) => {
      const ov = {};
      const o = s.overrides || {};
      if (o.input_address) ov.input_address = o.input_address;
      if (o.output_address) ov.output_address = o.output_address;
      if (o.tables && o.tables.length) ov.tables = o.tables;
      if (o.allow_overwrite === true || o.allow_overwrite === false) ov.allow_overwrite = o.allow_overwrite;
      const p = pos[s.id];
      const out = {
        id: s.id, pipeline: s.pipeline, label: s.label || "", rollback: s.rollback !== false,
        overrides: ov, position: p ? { x: Math.round(p.x), y: Math.round(p.y) } : null,
        join: s.join === "any" ? "any" : "all",
      };
      const probes = (s.probes || []).filter((x) => x.name || x.table).map((x) => {
        const pr = { name: x.name || "", table: x.table || "", agg: x.agg || "count" };
        if (x.column) pr.column = x.column;
        if (x.where) pr.where = x.where;
        return pr;
      });
      if (probes.length) out.probes = probes;
      return out;
    }),
    edges: state.edges.map((e) => {
      const out = { from: e.from, to: e.to, type: e.type, on: e.on || "success" };
      if (e.when && out.on !== "otherwise") out.when = e.when;
      if (e.label) out.label = e.label;
      return out;
    }),
  };
}

/* ------------------------------------------------------------------ canvas */

const svg = $("[data-wf-canvas]");
const { layer, graph, overlay } = mountCanvas(svg);
const zoomLabel = $("[data-wf-zoom]");
const pz = new PanZoom(svg, layer, (k) => { zoomLabel.textContent = `${Math.round(k * 100)}%`; });

function render() {
  const pos = layout(state.steps, state.edges);
  drawGraph(graph, state, pos, {
    pipelines: PIPES,
    selected,
    errors,
    resolveIO,
    edgeLabel: (e) => edgeChip(e),
  });
  $("[data-wf-empty]").hidden = state.steps.length > 0;
  $("[data-wf-dirty]").textContent = dirty() ? "Unsaved changes" : (isNew() ? "" : "Saved");
  $("[data-wf-dirty]").classList.toggle("is-dirty", dirty());
  $("[data-wf-act=undo]").disabled = !undo.length;
  $("[data-wf-act=redo]").disabled = !redo.length;
  renderInspector();
}

/* Node drag + port linking. */
let drag = null;

function stepAt(target) {
  const g = target.closest && target.closest("[data-step]");
  return g ? g.getAttribute("data-step") : null;
}

svg.addEventListener("pointerdown", (ev) => {
  const sid = stepAt(ev.target);
  const edgeG = ev.target.closest && ev.target.closest("[data-edge]");
  if (!sid && !edgeG) {
    if (ev.target === svg || ev.target.classList.contains("wf-bg")) {
      selected = null;
      render();
    }
    return;
  }
  ev.stopPropagation();
  if (edgeG) {
    selected = { kind: "edge", id: Number(edgeG.getAttribute("data-edge")) };
    render();
    return;
  }
  const pt = pz.toGraph(ev.clientX, ev.clientY);
  const pos = layout(state.steps, state.edges);
  if (ev.target.getAttribute("data-port") === "out" && CAN_WRITE) {
    drag = { mode: "link", from: sid, start: { x: pos[sid].x + NODE_W, y: pos[sid].y + NODE_H / 2 } };
  } else {
    drag = { mode: "move", id: sid, dx: pt.x - pos[sid].x, dy: pt.y - pos[sid].y, moved: false, before: clone(state) };
    selected = { kind: "step", id: sid };
    render();
  }
  svg.setPointerCapture(ev.pointerId);
});

svg.addEventListener("pointermove", (ev) => {
  if (!drag) return;
  const pt = pz.toGraph(ev.clientX, ev.clientY);
  if (drag.mode === "move") {
    if (!CAN_WRITE) return;
    const step = state.steps.find((s) => s.id === drag.id);
    if (!step) return;
    const pos = layout(state.steps, state.edges);
    if (!step.position) step.position = pos[step.id];
    step.position = { x: Math.round((pt.x - drag.dx) / 8) * 8, y: Math.round((pt.y - drag.dy) / 8) * 8 };
    drag.moved = true;
    render();
  } else {
    overlay.innerHTML = "";
    svgEl("path", { d: curve(drag.start.x, drag.start.y, pt.x, pt.y), class: "wf-edge is-ghost" }, overlay);
    const over = document.elementFromPoint(ev.clientX, ev.clientY);
    $$(".wf-node.is-target", svg).forEach((n) => n.classList.remove("is-target"));
    const tid = over ? stepAt(over) : null;
    if (tid && tid !== drag.from) {
      const n = svg.querySelector(`[data-step="${CSS.escape(tid)}"]`);
      if (n) n.classList.add(wouldCycle(drag.from, tid) ? "is-bad-target" : "is-target");
    }
  }
});

svg.addEventListener("pointerup", (ev) => {
  if (!drag) return;
  const d = drag;
  drag = null;
  overlay.innerHTML = "";
  if (d.mode === "move") {
    if (d.moved) {
      undo.push(d.before);
      redo.length = 0;
      render();
    }
    return;
  }
  const over = document.elementFromPoint(ev.clientX, ev.clientY);
  const tid = over ? stepAt(over) : null;
  if (!tid || tid === d.from) { render(); return; }
  if (state.edges.some((e) => e.from === d.from && e.to === tid)) { flash("Those steps are already linked.", "err"); render(); return; }
  if (wouldCycle(d.from, tid)) { flash("That link would create a loop.", "err"); render(); return; }
  const type = canTakeAnotherDataLink(tid) ? "data" : "after";
  commit((s) => { s.edges.push({ from: d.from, to: tid, type, on: "success" }); });
  selected = { kind: "edge", id: state.edges.length - 1 };
  render();
});

svg.addEventListener("dblclick", (ev) => {
  const sid = stepAt(ev.target);
  if (sid) {
    const pipe = state.steps.find((s) => s.id === sid);
    if (pipe) window.open(`/pipelines/${encodeURIComponent(pipe.pipeline)}`, "_blank", "noopener");
  }
});

svg.addEventListener("keydown", (ev) => {
  const sid = stepAt(ev.target);
  if (sid && (ev.key === "Enter" || ev.key === " ")) {
    selected = { kind: "step", id: sid };
    render();
    ev.preventDefault();
  }
});

/* Drop from the palette. */
const stage = $(".wf-stage");
stage.addEventListener("dragover", (ev) => { if (CAN_WRITE) { ev.preventDefault(); stage.classList.add("is-drop"); } });
stage.addEventListener("dragleave", () => stage.classList.remove("is-drop"));
stage.addEventListener("drop", (ev) => {
  ev.preventDefault();
  stage.classList.remove("is-drop");
  const name = ev.dataTransfer.getData("text/x-ovid2-pipeline");
  if (!name) return;
  const pt = pz.toGraph(ev.clientX, ev.clientY);
  addStep(name, { x: Math.round((pt.x - NODE_W / 2) / 8) * 8, y: Math.round((pt.y - NODE_H / 2) / 8) * 8 });
});

function addStep(pipeline, position) {
  if (!CAN_WRITE) return;
  const id = nextStepId(pipeline);
  let pos = position;
  if (!pos) {
    const cur = layout(state.steps, state.edges);
    const sel = selected && selected.kind === "step" ? cur[selected.id] : null;
    const xs = Object.values(cur);
    pos = sel ? { x: sel.x + NODE_W + 96, y: sel.y }
      : xs.length ? { x: Math.max(...xs.map((p) => p.x)) + NODE_W + 96, y: xs[0].y } : null;
  }
  const after = selected && selected.kind === "step" && !position ? selected.id : null;
  commit((s) => {
    s.steps.push({ id, pipeline, label: "", overrides: {}, rollback: true, position: pos });
    if (after) s.edges.push({ from: after, to: id, type: "data" });
    if (s.steps.length === 1 && !triggerTouched && PIPES[pipeline]) {
      s.trigger = { type: PIPES[pipeline].trigger_type || "manual" };
      if (s.trigger.type === "cron") s.trigger.cron = "0 2 * * *";
      if (s.trigger.type === "interval") s.trigger.interval_minutes = 60;
    }
  });
  selected = { kind: "step", id };
  render();
}

/* ------------------------------------------------------------------ palette */

function renderPalette() {
  const list = $("[data-wf-palette]");
  const q = ($("[data-wf-palette-filter]").value || "").toLowerCase();
  list.innerHTML = "";
  for (const p of catalog.pipelines || []) {
    if (q && !`${p.name} ${p.kind} ${p.description}`.toLowerCase().includes(q)) continue;
    const li = document.createElement("li");
    li.className = "wf-palette-item";
    li.dataset.kind = p.kind;
    li.draggable = CAN_WRITE;
    li.innerHTML =
      `<span class="wf-palette-accent" aria-hidden="true"></span>` +
      `<span class="wf-palette-body"><span class="wf-palette-name">${esc(p.name)}</span>` +
      `<span class="wf-palette-meta"><span class="wf-kind-tag">${esc(p.kind.replace("_", " "))}</span>` +
      `<span class="mono">${esc(p.input_address || "open")} \u2192 ${esc(p.output_address || "open")}</span></span></span>` +
      (CAN_WRITE ? `<button type="button" class="icon-button wf-palette-add" title="Add to the canvas" aria-label="Add ${esc(p.name)}">+</button>` : "");
    li.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.setData("text/x-ovid2-pipeline", p.name);
      ev.dataTransfer.effectAllowed = "copy";
    });
    const add = li.querySelector(".wf-palette-add");
    if (add) add.addEventListener("click", () => addStep(p.name));
    if (p.description && window.Ovid2HelpTip) {
      li.querySelector(".wf-palette-name").insertAdjacentHTML("beforeend", " " + window.Ovid2HelpTip.html(p.description, "About this pipeline"));
    }
    list.appendChild(li);
  }
}
$("[data-wf-palette-filter]").addEventListener("input", renderPalette);

/* ------------------------------------------------------------------ inspector */

function fillAddressSelect(sel, value, placeholder) {
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = "";
  def.textContent = placeholder;
  sel.appendChild(def);
  for (const a of catalog.addresses || []) {
    const o = document.createElement("option");
    o.value = a.name;
    o.textContent = a.type ? `${a.name}  (${a.type})` : a.name;
    sel.appendChild(o);
  }
  if (value && !ADDRS[value]) {
    const o = document.createElement("option");
    o.value = value;
    o.textContent = `${value} (missing)`;
    sel.appendChild(o);
  }
  sel.value = value || "";
}

function showPane(kind) {
  $$("[data-insp]").forEach((p) => { p.hidden = p.dataset.insp !== kind; });
}

function renderInspector() {
  if (selected && selected.kind === "step") {
    const step = state.steps.find((s) => s.id === selected.id);
    if (!step) { selected = null; return renderInspector(); }
    showPane("step");
    const pane = $("[data-insp=step]");
    $("[data-step-id]", pane).textContent = step.id;
    const ps = $("#st-pipeline");
    ps.innerHTML = "";
    for (const p of catalog.pipelines || []) {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = `${p.name}  (${p.kind})`;
      ps.appendChild(o);
    }
    if (!PIPES[step.pipeline]) {
      const o = document.createElement("option");
      o.value = step.pipeline;
      o.textContent = `${step.pipeline} (missing)`;
      ps.appendChild(o);
    }
    ps.value = step.pipeline;
    ps.disabled = !CAN_WRITE;
    const pipe = PIPES[step.pipeline] || {};
    const ov = step.overrides || {};
    $("#st-label").value = step.label || "";
    fillAddressSelect($("#st-input"), ov.input_address, `Pipeline default${pipe.input_address ? " — " + pipe.input_address : " (open)"}`);
    fillAddressSelect($("#st-output"), ov.output_address, `Pipeline default${pipe.output_address ? " — " + pipe.output_address : " (open)"}`);
    const parent = dataParent(step.id);
    $("[data-step-has-data]").value = parent ? "1" : "0";
    if (parent) {
      const io = resolveIO(step);
      $("[data-step-data-input]").innerHTML =
        `<span class="wf-chip">${esc(io.inputFrom)}</span> output <span class="mono">${esc(io.input || "open")}</span>`;
    }
    const spec = kindSpec(pipe.kind);
    $("[data-step-tables-mode]").value = spec ? spec.tables_mode : "";
    $("#st-tables").value = (ov.tables || []).join(" ");
    $("#st-tables").placeholder = (pipe.tables || []).join(" ") || "all";
    $("#st-overwrite").value = ov.allow_overwrite === true ? "true" : ov.allow_overwrite === false ? "false" : "";
    $("[data-step-field=rollback]", pane).checked = step.rollback !== false;
    $("[data-step-incoming]").value = incomingCount(step.id) > 1 ? "many" : String(incomingCount(step.id));
    $("#st-join").value = step.join === "any" ? "any" : "all";
    renderProbes(step);
    const outName = ov.output_address || pipe.output_address;
    const outInfo = outName ? ADDRS[outName] : null;
    $("[data-step-pipeline-card]").innerHTML = pipe.name
      ? `<a href="/pipelines/${encodeURIComponent(pipe.name)}" target="_blank" rel="noopener">Open pipeline <span class="mono">${esc(pipe.name)}</span></a>` +
        ` <span class="small">engine <span class="mono">${esc(pipe.engine || "")}</span></span>` +
        (outInfo && outInfo.rollback === false ? `<div class="flash error">Output storage (${esc(outInfo.type)}) cannot be rolled back.</div>` : "")
      : `<div class="flash error">Pipeline '${esc(step.pipeline)}' no longer exists.</div>`;
    const errs = errors[step.id] || [];
    const ul = $("[data-step-errors]");
    ul.hidden = !errs.length;
    ul.innerHTML = errs.map((e) => `<li>${esc(e)}</li>`).join("");
    $$("input,select,textarea,button[data-probe-add],button[data-probe-del]", pane).forEach((el) => { if (el.id !== "st-pipeline") el.disabled = !CAN_WRITE && el.type !== "hidden"; });
    if (window.Ovid2Visibility) window.Ovid2Visibility.refresh(pane);
    return;
  }
  if (selected && selected.kind === "edge") {
    const e = state.edges[selected.id];
    if (!e) { selected = null; return renderInspector(); }
    showPane("edge");
    const name = (id) => { const s = state.steps.find((x) => x.id === id); return s ? (s.label || s.pipeline) : id; };
    $("[data-edge-ends]").textContent = `${name(e.from)}  \u2192  ${name(e.to)}`;
    $$("input[name=edge_type]").forEach((r) => { r.checked = r.value === e.type; r.disabled = !CAN_WRITE; });
    const epane = $("[data-insp=edge]");
    $("#ed-on").value = e.on || "success";
    $("#ed-on").disabled = !CAN_WRITE;
    $("#ed-label").value = e.label || "";
    $("#ed-label").disabled = !CAN_WRITE;
    const enable = $("[data-cond-enable]");
    enable.checked = !!e.when;
    enable.disabled = !CAN_WRITE;
    const src = state.steps.find((x) => x.id === e.from) || {};
    condBuilder.render(e.when || null, { probes: src.probes || [], disabled: !CAN_WRITE });
    $("[data-cond-summary]").textContent = e.when ? summarise(e.when, CONDS) : "";
    if (window.Ovid2Visibility) window.Ovid2Visibility.refresh(epane);
    return;
  }
  showPane("workflow");
  const pane = $("[data-insp=workflow]");
  $("#wf-name").value = state.name;
  $("#wf-desc").value = state.description || "";
  $("#wf-trigger").value = state.trigger.type || "manual";
  $("#wf-cron").value = state.trigger.cron || "";
  $("#wf-interval").value = state.trigger.interval_minutes || "";
  $("#wf-wait").value = state.trigger.watch_min_wait_minutes ?? "";
  $("[data-wf-enabled]").checked = !!state.enabled;
  const roots = state.steps.filter((s) => !state.edges.some((e) => e.to === s.id));
  const nData = state.edges.filter((e) => e.type === "data").length;
  const nCond = state.edges.filter((e) => e.when || (e.on && e.on !== "success")).length;
  $("[data-wf-summary]").innerHTML =
    `<div class="wf-kpis"><span><b>${state.steps.length}</b> steps</span><span><b>${nData}</b> data links</span>` +
    `<span><b>${state.edges.length - nData}</b> after links</span><span><b>${nCond}</b> branches</span>` +
    `<span><b>${roots.length}</b> start steps</span></div>`;
  $$("input,select,textarea", pane).forEach((el) => { if (el.id !== "wf-name" || isNew()) el.disabled = !CAN_WRITE; });
  if (window.Ovid2Visibility) window.Ovid2Visibility.refresh(pane);
}

/* Inspector edits. */
$("[data-insp=workflow]").addEventListener("change", (ev) => {
  const t = ev.target;
  commit((s) => {
    if (t.id === "wf-name") s.name = t.value.trim();
    if (t.id === "wf-desc") s.description = t.value;
    if (t.id === "wf-trigger") { s.trigger = { type: t.value }; triggerTouched = true; }
    if (t.id === "wf-cron") s.trigger.cron = t.value.trim();
    if (t.id === "wf-interval") s.trigger.interval_minutes = t.value ? Number(t.value) : null;
    if (t.id === "wf-wait") s.trigger.watch_min_wait_minutes = t.value === "" ? null : Number(t.value);
    if (t.matches("[data-wf-enabled]")) s.enabled = t.checked;
  });
});
$("#wf-name").addEventListener("input", (ev) => { state.name = ev.target.value.trim(); $("[data-wf-title]").firstChild.textContent = state.name || "New workflow"; });

$("[data-insp=step]").addEventListener("change", (ev) => {
  const t = ev.target;
  const pf = t.getAttribute("data-probe-field");
  if (pf && selected && selected.kind === "step") {
    const sid = selected.id;
    const i = Number(t.closest("[data-probe]").dataset.probe);
    commit((s) => {
      const p = s.steps.find((x) => x.id === sid).probes[i];
      p[pf] = t.value.trim();
      if (pf === "agg" && p.agg === "count") delete p.column;
    });
    $("[data-step-probes-box]").open = true;
    return;
  }
  const field = t.getAttribute("data-step-field");
  if (!field || !selected || selected.kind !== "step") return;
  const sid = selected.id;
  commit((s) => {
    const step = s.steps.find((x) => x.id === sid);
    if (!step) return;
    step.overrides = step.overrides || {};
    if (field === "pipeline") step.pipeline = t.value;
    else if (field === "label") step.label = t.value;
    else if (field === "rollback") step.rollback = t.checked;
    else if (field === "join") step.join = t.value;
    else if (field === "tables") step.overrides.tables = t.value.split(/[\s,]+/).filter(Boolean);
    else if (field === "allow_overwrite") step.overrides.allow_overwrite = t.value === "" ? null : t.value === "true";
    else step.overrides[field] = t.value || null;
  });
});

$("[data-insp=edge]").addEventListener("change", (ev) => {
  if (!selected || selected.kind !== "edge") return;
  const idx = selected.id;
  const e = state.edges[idx];
  const t = ev.target;
  if (t.closest("[data-cond-tree]")) return; // handled by the condition builder
  if (t.name === "edge_type") {
    const val = t.value;
    const target = state.steps.find((x) => x.id === e.to) || {};
    if (val === "data" && target.join !== "any" && state.edges.some((x, i) => i !== idx && x.to === e.to && x.type === "data")) {
      flash("A step reads one input — it already has a data link. Set its 'Run when' to 'any' where branches meet.", "err");
      render();
      return;
    }
    commit((s) => { s.edges[idx].type = val; });
  } else if (t.id === "ed-on") {
    commit((s) => {
      s.edges[idx].on = t.value;
      if (t.value === "otherwise") delete s.edges[idx].when;
    });
  } else if (t.id === "ed-label") {
    commit((s) => { s.edges[idx].label = t.value.trim(); });
  } else if (t.matches("[data-cond-enable]")) {
    commit((s) => {
      if (t.checked) s.edges[idx].when = s.edges[idx].when || { field: "status", op: "eq", value: "succeeded" };
      else delete s.edges[idx].when;
    });
  }
});

const condBuilder = new ConditionBuilder($("[data-cond-tree]"), CONDS, (tree) => {
  if (!selected || selected.kind !== "edge") return;
  const idx = selected.id;
  commit((s) => {
    if (tree) s.edges[idx].when = tree;
    else delete s.edges[idx].when;
  });
});

/* Edge chip text: outcome and a short condition (null keeps the type chip). */
function edgeChip(e) {
  const on = e.on || "success";
  if (on === "failure") return { text: e.label || "on fail", cls: "is-failure", title: "Runs when the step fails" };
  if (on === "always") return { text: e.label || "always", cls: "is-always", title: "Runs whether the step succeeds or fails" };
  if (on === "otherwise") return { text: e.label || "else", cls: "is-otherwise", title: "Runs when no other branch matched" };
  if (e.when) return { text: e.label || "if", cls: "is-cond", title: summarise(e.when, CONDS) };
  return null;
}

/* Data probes (step inspector). */
const AGG_LABEL = { count: "row count", count_distinct: "distinct values of", null_count: "empty values of", non_null_count: "filled values of" };

function renderProbes(step) {
  const list = $("[data-probes]");
  const probes = step.probes || [];
  $("[data-probe-count]").textContent = probes.length ? `(${probes.length})` : "";
  list.innerHTML = probes.map((p, i) => {
    const needsCol = (p.agg || "count") !== "count";
    return `<li class="wf-probe" data-probe="${i}">` +
      `<div class="wf-probe-row"><input type="text" class="mono" data-probe-field="name" value="${esc(p.name || "")}" placeholder="name" aria-label="Probe name" data-allow-new>` +
      `<button type="button" class="icon-button small" data-probe-del title="Remove probe" aria-label="Remove probe">&times;</button></div>` +
      `<div class="wf-probe-row"><select data-probe-field="agg" aria-label="Aggregate">` +
      (CONDS.probe_aggs || ["count"]).map((a) => `<option value="${a}"${a === (p.agg || "count") ? " selected" : ""}>${esc(AGG_LABEL[a] || a)}</option>`).join("") +
      `</select>` +
      (needsCol ? `<input type="text" class="mono" data-probe-field="column" value="${esc(p.column || "")}" placeholder="column" aria-label="Column" data-allow-new>` : "") +
      `</div>` +
      `<div class="wf-probe-row"><span class="small">in</span><input type="text" class="mono" data-probe-field="table" value="${esc(p.table || "")}" placeholder="table" aria-label="Table" data-allow-new></div>` +
      `<div class="wf-probe-row"><span class="small">where</span><input type="text" class="mono" data-probe-field="where" value="${esc(p.where || "")}" placeholder="SEX = 'F'" aria-label="Row filter (optional)" data-allow-new></div>` +
      `</li>`;
  }).join("");
}

$("[data-insp=step]").addEventListener("click", (ev) => {
  if (!CAN_WRITE || !selected || selected.kind !== "step") return;
  const sid = selected.id;
  if (ev.target.closest("[data-probe-add]")) {
    commit((s) => {
      const st = s.steps.find((x) => x.id === sid);
      st.probes = st.probes || [];
      let n = st.probes.length + 1;
      while (st.probes.some((p) => p.name === `probe${n}`)) n += 1;
      st.probes.push({ name: `probe${n}`, table: "", agg: "count" });
    });
    $("[data-step-probes-box]").open = true;
  } else if (ev.target.closest("[data-probe-del]")) {
    const i = Number(ev.target.closest("[data-probe]").dataset.probe);
    commit((s) => { s.steps.find((x) => x.id === sid).probes.splice(i, 1); });
    $("[data-step-probes-box]").open = true;
  }
});

/* ------------------------------------------------------------------ actions */

function deleteSelection() {
  if (!CAN_WRITE || !selected) return;
  if (selected.kind === "step") {
    const sid = selected.id;
    commit((s) => {
      s.steps = s.steps.filter((x) => x.id !== sid);
      s.edges = s.edges.filter((e) => e.from !== sid && e.to !== sid);
    });
  } else {
    const idx = selected.id;
    commit((s) => { s.edges.splice(idx, 1); });
  }
  selected = null;
  render();
}

function duplicateSelection() {
  if (!CAN_WRITE || !selected || selected.kind !== "step") return;
  const src = state.steps.find((s) => s.id === selected.id);
  if (!src) return;
  const id = nextStepId(src.pipeline);
  const pos = layout(state.steps, state.edges)[src.id];
  commit((s) => {
    const copy = clone(src);
    copy.id = id;
    copy.label = src.label ? `${src.label} (copy)` : "";
    copy.overrides = Object.assign({}, copy.overrides, { output_address: null });
    copy.position = { x: pos.x + 24, y: pos.y + NODE_H + 30 };
    s.steps.push(copy);
  });
  selected = { kind: "step", id };
  render();
}

async function validate(show = true) {
  const name = state.name || "draft";
  try {
    const v = await api("POST", `/workflows/${encodeURIComponent(name)}/validate`, payload());
    errors = v.step_errors || {};
    render();
    if (show) {
      if (v.ok) flash(v.warnings && v.warnings.length ? `Valid — ${v.warnings.join(" ")}` : "Workflow is valid.");
      else flash(v.errors.join(" "), "err");
    }
    return v;
  } catch (err) {
    flash(err.message, "err");
    return null;
  }
}

async function save() {
  if (!CAN_WRITE) return false;
  if (!state.name) { selected = null; render(); flash("Give the workflow a name first.", "err"); $("#wf-name").focus(); return false; }
  const v = await validate(false);
  if (v && !v.ok) { flash(v.errors.join(" "), "err"); return false; }
  try {
    const method = isNew() ? "POST" : "PUT";
    await api(method, `/workflows/${encodeURIComponent(state.name)}`, payload());
    savedJson = JSON.stringify(state);
    flash("Saved.");
    if (isNew()) {
      window.location.assign(`/workflows/${encodeURIComponent(state.name)}`);
      return true;
    }
    render();
    return true;
  } catch (err) {
    flash(err.message, "err");
    return false;
  }
}

async function run() {
  if (!CAN_RUN || isNew()) return;
  if (dirty() && !(await save())) return;
  try {
    const r = await api("POST", `/workflows/${encodeURIComponent(state.name)}/run`, {});
    window.location.assign(`/workflows/${encodeURIComponent(state.name)}/runs/${encodeURIComponent(r.run_id)}`);
  } catch (err) {
    flash(err.message, "err");
  }
}

async function duplicateWorkflow() {
  const proposed = window.prompt("Name for the copy", `${state.name}_copy`);
  if (!proposed) return;
  try {
    const r = await api("POST", `/workflows/${encodeURIComponent(state.name)}/duplicate`, { new_name: proposed });
    window.location.assign(`/workflows/${encodeURIComponent(r.name)}`);
  } catch (err) {
    flash(err.message, "err");
  }
}

function tidy() {
  commit((s) => {
    const auto = autoLayout(s.steps, s.edges);
    s.steps.forEach((st) => { st.position = auto[st.id]; });
  });
  fit();
}

function fit() {
  pz.fit(bounds(state.steps, state.edges, layout(state.steps, state.edges)));
}

root.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-wf-act]");
  if (!btn) return;
  const act = btn.dataset.wfAct;
  if (act === "undo" && undo.length) { redo.push(clone(state)); state = undo.pop(); render(); }
  else if (act === "redo" && redo.length) { undo.push(clone(state)); state = redo.pop(); render(); }
  else if (act === "tidy") tidy();
  else if (act === "fit") fit();
  else if (act === "zoom-in") pz.zoom(1.2);
  else if (act === "zoom-out") pz.zoom(1 / 1.2);
  else if (act === "validate") validate(true);
  else if (act === "save") save();
  else if (act === "run") run();
  else if (act === "duplicate") duplicateWorkflow();
  else if (act === "delete") deleteSelection();
});

document.addEventListener("keydown", (ev) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((ev.target && ev.target.tagName) || "");
  const mod = ev.ctrlKey || ev.metaKey;
  if (mod && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); return; }
  if (typing) return;
  if (mod && ev.key.toLowerCase() === "z" && !ev.shiftKey) { ev.preventDefault(); $("[data-wf-act=undo]").click(); }
  else if (mod && (ev.key.toLowerCase() === "y" || (ev.key.toLowerCase() === "z" && ev.shiftKey))) { ev.preventDefault(); $("[data-wf-act=redo]").click(); }
  else if (mod && ev.key.toLowerCase() === "d") { ev.preventDefault(); duplicateSelection(); }
  else if (ev.key === "Delete" || ev.key === "Backspace") { if (selected) { ev.preventDefault(); deleteSelection(); } }
  else if (ev.key === "Escape") { selected = null; render(); }
});

window.addEventListener("beforeunload", (ev) => {
  if (dirty() && CAN_WRITE) { ev.preventDefault(); ev.returnValue = ""; }
});

/* ------------------------------------------------- assistant pre-fill */

/*
 * open_workflow_canvas (assistant UI action): llm_panel.js leaves an
 * UNSAVED working copy in sessionStorage and opens /workflows/new?prefill=1.
 * It is read once, removed, and every name is re-checked against what this
 * page lists — unknown configs / addresses are dropped and reported, never
 * trusted. Nothing is saved until the human presses Save.
 */
const PREFILL_KEY = "ovid2WorkflowPrefill";
const STEP_ID = /^[a-z0-9][a-z0-9_-]{0,39}$/;
const WF_NAME = /^[A-Za-z0-9_.-]{1,64}$/;
const TEXT = /^[^<>]*$/;

function takePrefill() {
  if (!isNew()) return null;
  if (new URLSearchParams(window.location.search).get("prefill") !== "1") return null;
  let raw = null;
  try {
    raw = window.sessionStorage.getItem(PREFILL_KEY);
    window.sessionStorage.removeItem(PREFILL_KEY);
  } catch (_e) { return null; }
  if (!raw) return null;
  if (!CAN_WRITE) {
    flash("The assistant prepared a workflow, but your role cannot create workflows here.", "err");
    return null;
  }
  try { return JSON.parse(raw); } catch (_e) { return null; }
}

function applyPrefill(doc) {
  const notes = [];
  const steps = [];
  const ids = new Set();
  for (const st of Array.isArray(doc.steps) ? doc.steps.slice(0, 20) : []) {
    if (!st || typeof st.id !== "string" || !STEP_ID.test(st.id) || ids.has(st.id)) continue;
    if (typeof st.pipeline !== "string" || !PIPES[st.pipeline]) {
      notes.push(`Step '${st.id}': pipeline '${String(st.pipeline || "")}' is not a saved config — add that step yourself.`);
      continue;
    }
    const ov = {};
    const o = st.overrides && typeof st.overrides === "object" ? st.overrides : {};
    for (const key of ["input_address", "output_address"]) {
      if (typeof o[key] !== "string" || !o[key]) continue;
      if (ADDRS[o[key]]) ov[key] = o[key];
      else notes.push(`Step '${st.id}': address '${o[key]}' does not exist — pick one.`);
    }
    if (Array.isArray(o.tables)) {
      const tables = o.tables.filter((t) => typeof t === "string" && /^[A-Za-z0-9_*]{1,64}$/.test(t));
      if (tables.length) ov.tables = tables;
    }
    const label = typeof st.label === "string" && st.label.length <= 80 && TEXT.test(st.label) ? st.label : "";
    ids.add(st.id);
    steps.push({ id: st.id, pipeline: st.pipeline, label, overrides: ov, rollback: true, position: null });
  }
  const edges = [];
  for (const e of Array.isArray(doc.edges) ? doc.edges.slice(0, 40) : []) {
    if (!e || !ids.has(e.from) || !ids.has(e.to) || e.from === e.to) continue;
    if (edges.some((x) => x.from === e.from && x.to === e.to)) continue;
    const type = e.type === "after" ? "after" : "data";
    const on = ["success", "failure", "always", "otherwise"].includes(e.on) ? e.on : "success";
    edges.push({ from: e.from, to: e.to, type, on });
  }
  if (!steps.length) return false;
  commit((s) => {
    if (typeof doc.name === "string" && WF_NAME.test(doc.name)) s.name = doc.name;
    if (typeof doc.description === "string" && doc.description.length <= 500 && TEXT.test(doc.description)) {
      s.description = doc.description;
    }
    s.enabled = false;
    s.steps = steps;
    s.edges = edges;
    const auto = autoLayout(s.steps, s.edges);
    s.steps.forEach((st) => { st.position = auto[st.id]; });
    const first = PIPES[steps[0].pipeline];
    s.trigger = { type: (first && first.trigger_type) || "manual" };
    if (s.trigger.type === "cron") s.trigger.cron = "0 2 * * *";
    if (s.trigger.type === "interval") s.trigger.interval_minutes = 60;
  });
  if (state.name) $("[data-wf-title]").firstChild.textContent = state.name;
  const missing = (Array.isArray(doc.missing) ? doc.missing : [])
    .filter((m) => typeof m === "string" && m.length <= 160 && TEXT.test(m)).slice(0, 10);
  const box = $("[data-wf-prefill]");
  if (box) {
    const items = [...missing, ...notes];
    box.innerHTML = "<strong>Pre-filled by the assistant (not saved).</strong> " +
      (items.length ? "Still to do:<ul>" + items.map((m) => `<li>${esc(m)}</li>`).join("") + "</ul>"
        : "Review it, then Save.");
    box.hidden = false;
  }
  return true;
}

/* ------------------------------------------------------------------ boot */

renderPalette();
render();
const prefill = takePrefill();
if (prefill && applyPrefill(prefill)) {
  window.requestAnimationFrame(fit);
  validate(false);
} else {
  window.requestAnimationFrame(fit);
}
if (root.dataset.draft === "true") validate(true);
