/*
 * Workflow run view — /workflows/<name>/runs/<run_id>.
 *
 * Draws the run's frozen definition read-only with every step coloured
 * by its state and polls GET /api/v1/workflow-runs/<id> every 2.5 s while
 * the run is active. The side list shows each step's job link, error and
 * rollback action; Cancel / Retry rollback / Run again call the JSON API.
 */
import { PanZoom, bounds, drawGraph, esc, layout, mountCanvas } from "/static/workflow_graph.js";
import { summarise } from "/static/workflow_conditions.js";

const root = document.getElementById("wf-run");
const NAME = root.dataset.name;
const RUN_ID = root.dataset.runId;
const CAN_RUN = root.dataset.canRun === "true";
const catalog = JSON.parse(document.getElementById("wf-catalog").textContent || "{}");
const PIPES = Object.fromEntries((catalog.pipelines || []).map((p) => [p.name, p]));
const CONDS = catalog.conditions || {};

function edgeLabel(e) {
  const on = e.on || "success";
  if (on === "failure") return { text: e.label || "on fail", cls: "is-failure", title: "Runs when the step fails" };
  if (on === "always") return { text: e.label || "always", cls: "is-always", title: "" };
  if (on === "otherwise") return { text: e.label || "else", cls: "is-otherwise", title: "Runs when no other branch matched" };
  if (e.when) return { text: e.label || "if", cls: "is-cond", title: summarise(e.when, CONDS) };
  return null;
}
let run = JSON.parse(document.getElementById("wf-run-initial").textContent || "{}");
const ACTIVE = new Set(["running", "stopping", "rolling_back", "finishing"]);
const $ = (sel) => root.querySelector(sel);

const svg = $("[data-wf-canvas]");
const { layer, graph } = mountCanvas(svg);
const pz = new PanZoom(svg, layer);
let fitted = false;

function model() {
  const d = run.definition || { steps: [], edges: [], trigger: { type: run.trigger } };
  return { steps: d.steps || [], edges: d.edges || [], trigger: d.trigger || { type: run.trigger } };
}

function stepRun(id) {
  return (run.steps || []).find((s) => s.step_id === id) || {};
}

function draw() {
  const m = model();
  const pos = layout(m.steps, m.edges);
  const status = Object.fromEntries((run.steps || []).map((s) => [s.step_id, s.status]));
  const decisions = Object.fromEntries((run.edges || []).map((d) => [`${d.from}\u2192${d.to}`, d]));
  drawGraph(graph, m, pos, {
    pipelines: PIPES,
    status,
    decisions,
    edgeLabel,
    resolveIO: (s) => {
      const r = stepRun(s.id);
      const p = PIPES[s.pipeline] || {};
      return {
        input: r.input_address || (s.overrides || {}).input_address || p.input_address || "",
        output: r.output_address || (s.overrides || {}).output_address || p.output_address || "",
        inputFrom: null,
      };
    },
  });
  if (!fitted) {
    window.requestAnimationFrame(() => pz.fit(bounds(m.steps, m.edges, pos)));
    fitted = true;
  }
}

const ACTION_LABEL = {
  drop: "removing new tables",
  restore: "restoring overwritten tables",
  "drop-backup": "removing backup",
  "nothing to undo": "nothing to undo",
};

function renderSide() {
  const pill = $("[data-run-status]");
  pill.className = `wf-run-pill status-${run.status}`;
  pill.innerHTML = `<span class="wf-run-dot" aria-hidden="true"></span>${esc(String(run.status).replace(/_/g, " "))}`;
  const err = $("[data-run-error]");
  err.hidden = !run.error;
  err.textContent = run.error || "";
  if (run.ended_at) $("[data-run-ended]").textContent = new Date(run.ended_at).toLocaleString();
  const order = (model().steps || []).map((s) => s.id);
  const steps = [...(run.steps || [])].sort((a, b) => order.indexOf(a.step_id) - order.indexOf(b.step_id));
  $("[data-run-steps]").innerHTML = steps.map((s) => {
    const def = model().steps.find((x) => x.id === s.step_id) || {};
    const job = s.instance_id
      ? `<a href="/runs/${encodeURIComponent(s.instance_id)}" class="small">open job</a>` : "";
    const action = s.rollback_action
      ? `<div class="wf-step-rb small">${esc(ACTION_LABEL[s.rollback_action] || s.rollback_action)}</div>` : "";
    const pre = s.pre_existing_tables && s.pre_existing_tables.length
      ? `<div class="small">backed up ${s.pre_existing_tables.length} existing table(s)</div>` : "";
    return `<li class="wf-run-step status-${esc(s.status)}">` +
      `<span class="wf-run-step-dot" aria-hidden="true"></span>` +
      `<div class="wf-run-step-body"><div class="wf-run-step-head"><strong>${esc(def.label || s.pipeline)}</strong>` +
      `<span class="wf-run-pill status-${esc(s.status)}">${esc(String(s.status).replace(/_/g, " "))}</span></div>` +
      `<div class="mono small">${esc(s.input_address || "")} \u2192 ${esc(s.output_address || "")}</div>` +
      pre + action + (s.error ? `<div class="wf-step-err small">${esc(s.error)}</div>` : "") +
      (s.reason ? `<div class="wf-step-reason small">${esc(s.reason)}</div>` : "") +
      branches(s.step_id) + job +
      `</div></li>`;
  }).join("");
  if (CAN_RUN) {
    $("[data-run-act=cancel]").hidden = !ACTIVE.has(run.status) || run.status === "rolling_back";
    $("[data-run-act=rollback]").hidden = run.status !== "rollback_failed";
    $("[data-run-act=rerun]").hidden = ACTIVE.has(run.status);
  }
}

function branches(stepId) {
  const out = (run.edges || []).filter((d) => d.from === stepId);
  if (!out.length || out.every((d) => d.taken && /^on (success|always)$/.test(d.reason))) return "";
  const name = (id) => { const s = model().steps.find((x) => x.id === id); return s ? (s.label || s.pipeline) : id; };
  return `<ul class="wf-run-branches small">` + out.map((d) =>
    `<li class="${d.taken ? "is-taken" : "is-not-taken"}" title="${esc(d.reason)}">` +
    `<span aria-hidden="true">${d.taken ? "\u2713" : "\u2715"}</span> ${esc(name(d.to))}` +
    `<span class="mono"> \u2014 ${esc(d.reason)}</span></li>`).join("") + `</ul>`;
}

async function call(method, path) {
  const r = await fetch(`/api/v1${path}`, {
    method, credentials: "same-origin", headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: method === "GET" ? undefined : "{}",
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || r.statusText);
  return data;
}

async function poll() {
  try {
    run = await call("GET", `/workflow-runs/${encodeURIComponent(RUN_ID)}`);
    draw();
    renderSide();
  } catch (_e) { /* keep last state */ }
  if (ACTIVE.has(run.status)) window.setTimeout(poll, 2500);
}

root.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-run-act],[data-wf-act]");
  if (!b) return;
  try {
    if (b.dataset.wfAct === "fit") {
      const m = model();
      pz.fit(bounds(m.steps, m.edges, layout(m.steps, m.edges)));
      return;
    }
    const act = b.dataset.runAct;
    if (act === "cancel") {
      if (!window.confirm("Cancel this run? Finished steps will be rolled back.")) return;
      run = await call("POST", `/workflow-runs/${encodeURIComponent(RUN_ID)}/cancel`);
    } else if (act === "rollback") {
      run = await call("POST", `/workflow-runs/${encodeURIComponent(RUN_ID)}/rollback`);
    } else if (act === "rerun") {
      const r = await call("POST", `/workflows/${encodeURIComponent(NAME)}/run`);
      window.location.assign(`/workflows/${encodeURIComponent(NAME)}/runs/${encodeURIComponent(r.run_id)}`);
      return;
    }
    draw();
    renderSide();
    window.setTimeout(poll, 1000);
  } catch (err) {
    const e = $("[data-run-error]");
    e.hidden = false;
    e.textContent = err.message;
  }
});

draw();
renderSide();
if (ACTIVE.has(run.status)) window.setTimeout(poll, 1500);
