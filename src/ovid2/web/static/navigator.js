/*
 * Ovid2 pipeline navigator (/navigator).
 *
 *   Tree     group (CDM / profile / kind / template) → package → table →
 *            field → step. Packages and tables load lazily from
 *            GET /api/v1/pipeline-packages/{p}/lineage[?table=]. Every level
 *            collapses; one filter box narrows all loaded levels (Enter
 *            loads every package's table summary to search them too);
 *            stage checkboxes hide whole stages. Open nodes, grouping and
 *            stage filters persist in localStorage; the selection is a
 *            deep link (?p=&t=&f=).
 *   Views    Overview — d3 tree of the package's tables with stage strips.
 *            Lanes    — one row per field × one column per stage; chips are
 *                       the functions applied; table-wide steps are bands.
 *                       Hovering a chip draws a line to every column it reads.
 *            YAML     — the selection's lineage as read-only YAML.
 *   Editor   spec_builder.js's Editor, re-targeted per package
 *            (setContext); a clicked step opens its document scrolled to
 *            the step. Generated / vendored sources open read-only.
 *   UDFs     "+ UDF" on a table or field opens udf_dialog.js.
 */

import { Editor, DOCS, api, setContext, yamlMod } from "/static/spec_builder.js";

const root = document.getElementById("navigator");
const CAN_WRITE = root ? root.dataset.canWrite === "true" : false;
const STAGES = ["format", "deduplicate", "transform", "map", "fix"];
const STORE = "ovid2.navigator.v1";

const esc = (s) => String(s == null ? "" : s).replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[c]);
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const enc = encodeURIComponent;
const tip = (text) => (window.Ovid2HelpTip ? window.Ovid2HelpTip.html(text) : "");

/* ------------------------------------------------------------- state */

const saved = (() => { try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (_e) { return {}; } })();
const state = {
  groupBy: root ? root.dataset.groupBy || saved.groupBy || "cdm" : "cdm",
  open: new Set(saved.open || []),
  stages: new Set(saved.stages || STAGES),
  only: "",
  filter: "",
  view: saved.view || "lanes",
  sel: { p: "", t: "", f: "", step: "" },
};
const persist = () => {
  try {
    localStorage.setItem(STORE, JSON.stringify({
      groupBy: state.groupBy, open: [...state.open].slice(-400), stages: [...state.stages], view: state.view,
    }));
  } catch (_e) { /* storage full / disabled */ }
};

const cache = { tree: null, pkg: {}, table: {}, inspect: {} };
const pkgFacts = (p) => {
  for (const g of (cache.tree && cache.tree.groups) || []) {
    const hit = g.pipelines.find((x) => x.pipeline === p);
    if (hit) return hit;
  }
  return null;
};
async function pkgSummary(p) {
  if (!cache.pkg[p]) cache.pkg[p] = await api("GET", "/pipeline-packages/" + enc(p) + "/lineage");
  return cache.pkg[p];
}
async function tableLineage(p, t) {
  const k = p + "/" + t;
  if (!cache.table[k]) cache.table[k] = await api("GET", "/pipeline-packages/" + enc(p) + "/lineage?table=" + enc(t));
  return cache.table[k];
}
async function inspection(p) {
  if (!cache.inspect[p]) cache.inspect[p] = await api("GET", "/pipeline-packages/" + enc(p) + "/inspect");
  return cache.inspect[p];
}
const invalidate = (p) => {
  delete cache.pkg[p]; delete cache.inspect[p];
  for (const k of Object.keys(cache.table)) if (k.startsWith(p + "/")) delete cache.table[k];
};

/* -------------------------------------------------------------- tree */

const ICON = {
  chev: '<svg class="icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9 18 15 12 9 6"/></svg>',
};

function rowHtml({ caret, icon, name, mono, badge, flags, cls, title }) {
  return '<button type="button" class="nv-row ' + (cls || "") + '" data-toggle data-select' + (title ? ' title="' + esc(title) + '"' : "") + ">" +
    '<span class="nv-caret">' + (caret ? ICON.chev : "") + "</span>" + (icon || "") +
    '<span class="nv-name' + (mono ? " mono" : "") + '">' + esc(name) + "</span>" + (flags || "") +
    (badge !== undefined && badge !== "" ? '<span class="nv-badge">' + esc(badge) + "</span>" : "") + "</button>";
}

const stageDots = (stages) => '<span class="nv-dots" aria-hidden="true">' + STAGES.map((s) => {
  const st = stages && stages[s];
  const n = st ? st.count : 0;
  return '<i class="is-' + s + (n ? " on" : "") + (st && st.opaque ? " is-opaque" : "") + '" title="' + s + ": " + n + '"></i>';
}).join("") + "</span>";

function renderTree() {
  const body = $("[data-nv-tree-body]");
  const groups = (cache.tree && cache.tree.groups) || [];
  body.innerHTML = groups.length ? groups.map((g) => {
    const key = "g:" + g.label;
    const open = !state.open.has("closed:" + key);
    return '<div class="nv-node nv-group" role="treeitem" aria-expanded="' + open + '" data-node="group" data-key="' + esc(key) + '">' +
      rowHtml({ caret: true, icon: "", name: g.label, badge: g.pipelines.length, cls: "is-group" }) +
      '<div class="nv-children" role="group"' + (open ? "" : " hidden") + ">" + g.pipelines.map((pkg) => {
        const pk = "p:" + pkg.pipeline;
        const flags = (pkg.editable ? "" : '<span class="nv-flag" title="Generated legacy-parity port — read-only">ro</span>') +
          (pkg.has_transforms ? '<span class="nv-flag is-udf" title="Has transform assignments">ƒ</span>' : "");
        return '<div class="nv-node nv-pkg" role="treeitem" aria-expanded="false" data-node="pipeline" data-key="' + esc(pk) +
          '" data-pipeline="' + esc(pkg.pipeline) + '" data-editable="' + pkg.editable + '" data-search="' +
          esc([pkg.pipeline, pkg.cdm, pkg.profile, pkg.template].join(" ").toLowerCase()) + '">' +
          rowHtml({ caret: true, name: pkg.pipeline, mono: true, badge: pkg.formatted_tables, flags }) +
          '<div class="nv-children" role="group" hidden data-lazy></div></div>';
      }).join("") + "</div></div>";
  }).join("") : '<p class="empty">No pipeline packages installed.</p>';
  // Re-open what was open.
  for (const n of $$('[data-node="pipeline"]', body)) if (state.open.has(n.dataset.key)) expand(n, { quiet: true });
  markSelection();
  applyFilter();
}

async function fillPackage(node) {
  const p = node.dataset.pipeline;
  const box = $(":scope > .nv-children", node);
  if (box.dataset.loaded) return;
  box.innerHTML = '<p class="tree-msg small">Loading…</p>';
  let s;
  try { s = await pkgSummary(p); } catch (err) { box.innerHTML = '<p class="flash error">' + esc(err.message) + "</p>"; return; }
  const rows = [...s.tables].sort((a, b) => (b.configured - a.configured) || a.table.localeCompare(b.table));
  box.innerHTML = rows.map((t) => {
    const tk = "t:" + p + "/" + t.table;
    const ops = STAGES.flatMap((st) => t.ops[st] || []).join(" ");
    const flags = (t.has_udf ? '<span class="nv-flag is-udf" title="Has a computed column / row filter (UDF)">ƒ</span>' : "") +
      (t.statuses && (t.statuses.unmapped || t.statuses.constant) ? '<span class="nv-flag is-warn" title="Unmapped / constant fields">' + ((t.statuses.unmapped || 0) + (t.statuses.constant || 0)) + "</span>" : "");
    return '<div class="nv-node nv-table' + (t.configured ? "" : " is-unconfigured") + '" role="treeitem" aria-expanded="false" data-node="table" data-key="' + esc(tk) +
      '" data-pipeline="' + esc(p) + '" data-table="' + esc(t.table) + '" data-search="' + esc((t.table + " " + ops + " " + (t.chains || []).join(" ")).toLowerCase()) +
      '" data-udf="' + (t.has_udf ? "1" : "") + '" data-opaque="' + (STAGES.some((st) => t.stages[st] && t.stages[st].opaque) ? "1" : "") +
      '" data-unmapped="' + (t.statuses && (t.statuses.unmapped || t.statuses.constant) ? "1" : "") + '">' +
      rowHtml({ caret: true, name: t.table, mono: true, flags: flags + stageDots(t.stages), title: t.configured ? "" : "Not configured — skipped by --tables all" }) +
      '<div class="nv-children" role="group" hidden data-lazy></div></div>';
  }).join("") || '<p class="tree-msg small">No CDM tables.</p>';
  box.dataset.loaded = "1";
  for (const n of $$('[data-node="table"]', box)) if (state.open.has(n.dataset.key)) expand(n, { quiet: true });
  applyFilter();
}

const STAGE_SHORT = { format: "F", deduplicate: "D", transform: "T", map: "M", fix: "X" };

async function fillTable(node) {
  const p = node.dataset.pipeline, t = node.dataset.table;
  const box = $(":scope > .nv-children", node);
  if (box.dataset.loaded) return;
  box.innerHTML = '<p class="tree-msg small">Loading…</p>';
  let L;
  try { L = await tableLineage(p, t); } catch (err) { box.innerHTML = '<p class="flash error">' + esc(err.message) + "</p>"; return; }
  const byId = Object.fromEntries(L.steps.map((s) => [s.id, s]));
  const stepNode = (s, fieldKey) => '<div class="nv-node nv-step" role="treeitem" data-node="step" data-stage="' + s.stage + '" data-step="' + esc(s.id) +
    '" data-pipeline="' + esc(p) + '" data-table="' + esc(t) + '" data-field="' + esc(fieldKey || "") + '" data-search="' + esc((s.op + " " + s.label + " " + (s.chain || "")).toLowerCase()) +
    '" data-opaque="' + (s.opaque ? "1" : "") + '" data-udf="' + (s.kind === "transform" ? "1" : "") + '">' +
    '<button type="button" class="nv-row" data-select><span class="nv-caret"></span><span class="nv-stage-pill is-' + s.stage + '">' + STAGE_SHORT[s.stage] + "</span>" +
    '<span class="nv-name">' + esc(s.label) + "</span>" + (s.opaque ? '<span class="nv-flag">py</span>' : "") + "</button></div>";
  const tableWide = L.table_steps.map((id) => byId[id]).filter(Boolean);
  box.innerHTML = (tableWide.length ? '<div class="nv-node nv-tablewide" role="treeitem" aria-expanded="false" data-node="tablewide" data-key="w:' + esc(p + "/" + t) + '">' +
      rowHtml({ caret: true, name: "table-wide steps", badge: tableWide.length, cls: "is-tablewide" }) +
      '<div class="nv-children" role="group" hidden>' + tableWide.map((s) => stepNode(s, "")).join("") + "</div></div>" : "") +
    L.fields.map((f) => {
      const fk = "f:" + p + "/" + t + "/" + f.name;
      const steps = f.steps.map((id) => byId[id]).filter(Boolean);
      const flags = (f.is_key ? '<span class="nv-flag" title="Composite key">key</span>' : "") +
        (f.status !== "ok" ? '<span class="nv-flag is-' + (f.status === "extra" ? "info" : "warn") + '">' + esc(f.status) + "</span>" : "") +
        (steps.some((s) => s.kind === "transform") ? '<span class="nv-flag is-udf">ƒ</span>' : "");
      return '<div class="nv-node nv-field" role="treeitem" aria-expanded="false" data-node="field" data-key="' + esc(fk) + '" data-pipeline="' + esc(p) +
        '" data-table="' + esc(t) + '" data-field="' + esc(f.name) + '" data-search="' + esc((f.name + " " + steps.map((s) => s.op + " " + s.label).join(" ")).toLowerCase()) +
        '" data-udf="' + (steps.some((s) => s.kind === "transform") ? "1" : "") + '" data-opaque="' + (steps.some((s) => s.opaque) ? "1" : "") +
        '" data-unmapped="' + (["unmapped", "constant"].includes(f.status) ? "1" : "") + '">' +
        rowHtml({ caret: steps.length > 0, name: f.name, mono: true, flags, badge: steps.length || "" }) +
        '<div class="nv-children" role="group" hidden>' + steps.map((s) => stepNode(s, f.name)).join("") + "</div></div>";
    }).join("");
  box.dataset.loaded = "1";
  for (const n of $$('[data-node="field"], [data-node="tablewide"]', box)) if (state.open.has(n.dataset.key)) expand(n, { quiet: true });
  applyFilter();
}

async function expand(node, { quiet } = {}) {
  const box = $(":scope > .nv-children", node);
  if (!box) return;
  node.setAttribute("aria-expanded", "true");
  box.hidden = false;
  if (node.dataset.node === "group") state.open.delete("closed:" + node.dataset.key);
  else state.open.add(node.dataset.key);
  if (node.dataset.node === "pipeline") await fillPackage(node);
  if (node.dataset.node === "table") await fillTable(node);
  if (!quiet) persist();
}

function collapse(node) {
  const box = $(":scope > .nv-children", node);
  if (!box) return;
  node.setAttribute("aria-expanded", "false");
  box.hidden = true;
  if (node.dataset.node === "group") state.open.add("closed:" + node.dataset.key);
  else state.open.delete(node.dataset.key);
  persist();
}

function markSelection() {
  $$(".nv-row.is-current").forEach((r) => r.classList.remove("is-current"));
  const s = state.sel;
  let sel = "";
  if (s.step) sel = '[data-node="step"][data-pipeline="' + CSS.escape(s.p) + '"][data-table="' + CSS.escape(s.t) + '"][data-step="' + CSS.escape(s.step) + '"]' + (s.f ? '[data-field="' + CSS.escape(s.f) + '"]' : "");
  else if (s.f) sel = '[data-node="field"][data-pipeline="' + CSS.escape(s.p) + '"][data-table="' + CSS.escape(s.t) + '"][data-field="' + CSS.escape(s.f) + '"]';
  else if (s.t) sel = '[data-node="table"][data-pipeline="' + CSS.escape(s.p) + '"][data-table="' + CSS.escape(s.t) + '"]';
  else if (s.p) sel = '[data-node="pipeline"][data-pipeline="' + CSS.escape(s.p) + '"]';
  const n = sel && $(sel);
  if (n) $(":scope > .nv-row", n).classList.add("is-current");
}

/* ----------------------------------------------------------- filters */

function applyFilter() {
  const q = state.filter.trim().toLowerCase();
  const only = state.only;
  const terms = q ? q.split(/\s+/) : [];
  const matches = (n) => {
    const hay = n.dataset.search || "";
    return terms.every((x) => hay.includes(x));
  };
  const onlyOk = (n) => !only || (only === "udf" ? !!n.dataset.udf : only === "opaque" ? !!n.dataset.opaque : only === "unmapped" ? !!n.dataset.unmapped : true);
  let shownPkgs = 0;
  // Bottom-up: a node is visible if it matches, or any descendant does.
  const visit = (n, ancestorHit) => {
    const kids = $$(":scope > .nv-children > .nv-node", n);
    const stageOk = n.dataset.node !== "step" || state.stages.has(n.dataset.stage);
    const selfHit = (!terms.length || matches(n)) && onlyOk(n) && stageOk;
    let kidHit = false;
    for (const k of kids) if (visit(k, ancestorHit || (selfHit && terms.length > 0))) kidHit = true;
    let show;
    if (n.dataset.node === "group") show = kidHit;
    else if (n.dataset.node === "pipeline") {
      const editableOk = only !== "editable" || n.dataset.editable === "true";
      const loaded = !!$(":scope > .nv-children[data-loaded]", n);
      const onlyNeedsKids = only && only !== "editable";
      show = editableOk && (onlyNeedsKids ? (loaded ? kidHit : true) : ((!terms.length || matches(n)) || kidHit));
      if (show) shownPkgs++;
    } else if (n.dataset.node === "step") show = stageOk && ((!terms.length || ancestorHit || matches(n)) && (only !== "opaque" || n.dataset.opaque || ancestorHit) && (only !== "udf" || n.dataset.udf || ancestorHit));
    else show = (selfHit || kidHit || (ancestorHit && onlyOk(n))) && stageOk;
    n.hidden = !show;
    if (terms.length && kidHit && n.dataset.node !== "group") {
      // Reveal matches inside (without persisting the open state).
      const box = $(":scope > .nv-children", n);
      if (box && (!box.hasAttribute("data-lazy") || box.dataset.loaded)) { box.hidden = false; n.setAttribute("aria-expanded", "true"); }
    }
    return show;
  };
  for (const g of $$('[data-nv-tree-body] > .nv-node')) visit(g, false);
  const c = $("[data-nv-count]");
  if (c) c.textContent = shownPkgs + " pipeline" + (shownPkgs === 1 ? "" : "s");
  if (lanesHost()) filterLanes();
}

async function deepSearch() {
  // Enter in the filter: load every visible package's table summary so
  // table / op names become searchable without expanding by hand.
  const pkgs = $$('[data-node="pipeline"]');
  const c = $("[data-nv-count]");
  if (c) c.textContent = "Searching " + pkgs.length + " packages…";
  for (const n of pkgs) await fillPackage(n);
  applyFilter();
}

/* --------------------------------------------------------- selection */

function setUrl(push) {
  const s = state.sel;
  const qs = new URLSearchParams();
  if (state.groupBy !== "cdm") qs.set("group_by", state.groupBy);
  if (s.p) qs.set("p", s.p);
  if (s.t) qs.set("t", s.t);
  if (s.f) qs.set("f", s.f);
  const url = "/navigator" + (qs.toString() ? "?" + qs : "");
  if (url !== location.pathname + location.search) (push ? history.pushState : history.replaceState).call(history, null, "", url);
}

async function revealPath({ p, t, f }) {
  const pn = $('[data-node="pipeline"][data-pipeline="' + CSS.escape(p) + '"]');
  if (!pn) return;
  const g = pn.closest(".nv-group");
  if (g && g.getAttribute("aria-expanded") !== "true") await expand(g);
  if (t) await expand(pn);
  const tn = t && $('[data-node="table"][data-pipeline="' + CSS.escape(p) + '"][data-table="' + CSS.escape(t) + '"]');
  if (tn && f) await expand(tn);
  const target = (f && tn && $('[data-node="field"][data-field="' + CSS.escape(f) + '"]', tn)) || tn || pn;
  const row = $(":scope > .nv-row", target);
  if (row) row.scrollIntoView({ block: "nearest" });
}

async function select(sel, { push = true, reveal = true } = {}) {
  state.sel = { p: sel.p || "", t: sel.t || "", f: sel.f || "", step: sel.step || "" };
  setUrl(push);
  if (reveal) await revealPath(state.sel);
  markSelection();
  await drawMain();
}

/* --------------------------------------------------------------- views */

const graphHost = () => $("[data-nv-graph]");
const lanesHost = () => $(".nv-lanes", graphHost());

function setViews(available) {
  const tabs = $("[data-nv-views]");
  tabs.hidden = !available.length;
  for (const b of $$("[data-view]", tabs)) {
    b.hidden = !available.includes(b.dataset.view);
    b.setAttribute("aria-selected", b.dataset.view === state.view ? "true" : "false");
  }
}

async function drawMain({ keepPanel = false } = {}) {
  const s = state.sel;
  const title = $("[data-nv-graph-title]"), sub = $("[data-nv-graph-sub]");
  $("[data-nv-legend]").hidden = !s.t;
  if (!s.p) {
    title.textContent = "Pick a pipeline";
    sub.textContent = "";
    setViews([]);
    graphHost().innerHTML = '<p class="empty">Choose a pipeline on the left.</p>';
    $("#nv-step-panel").hidden = true;
    return;
  }
  const facts = pkgFacts(s.p) || {};
  if (!s.t) {
    title.innerHTML = '<span class="mono">' + esc(s.p) + "</span>";
    sub.textContent = [facts.cdm, facts.profile, facts.template].filter(Boolean).join(" · ");
    if (state.view === "lanes") state.view = "overview";
    setViews(["overview", "yaml"]);
    const summary = await pkgSummary(s.p);
    if (state.view === "yaml") await drawYaml(summary);
    else drawOverview(summary);
    if (!keepPanel) drawPackagePanel(summary, facts);
    return;
  }
  title.innerHTML = '<span class="mono">' + esc(s.p) + " · " + esc(s.t) + (s.f ? " · " + esc(s.f) : "") + "</span>";
  const L = await tableLineage(s.p, s.t);
  sub.textContent = [L.cdm, L.profile, L.editable ? "" : "read-only port", L.configured ? "" : "not configured"].filter(Boolean).join(" · ");
  if (state.view === "overview") state.view = "lanes";
  setViews(["lanes", "yaml"]);
  if (state.view === "yaml") await drawYaml(s.f ? fieldDoc(L, s.f) : L);
  else drawLanes(L);
  if (keepPanel) return;
  if (s.step) await showStep(L, s.step);
  else if (s.f) drawFieldPanel(L, s.f);
  else drawTablePanel(L);
}

/* Overview: d3 tree pipeline → tables, each with a five-stage strip. */
function drawOverview(summary) {
  const host = graphHost();
  const tables = [...summary.tables].sort((a, b) => (b.configured - a.configured) || a.table.localeCompare(b.table));
  if (!window.d3) {
    host.innerHTML = '<ul class="nv-overview-list">' + tables.map((t) => '<li><a href="#" data-goto-table="' + esc(t.table) + '">' + esc(t.table) + "</a> " + stageDots(t.stages) + "</li>").join("") + "</ul>";
  } else {
    const d3 = window.d3;
    const rowH = 22, width = Math.max(host.clientWidth || 720, 560);
    const data = { name: summary.pipeline, children: tables.map((t) => ({ name: t.table, t })) };
    const rootNode = d3.hierarchy(data);
    const height = Math.max(rowH * tables.length, 120);
    d3.tree().size([height, 200])(rootNode);
    host.innerHTML = "";
    const svg = d3.select(host).append("svg").attr("class", "nv-overview").attr("width", width).attr("height", height + 24)
      .attr("viewBox", [-20, -12, width, height + 24].join(" ")).attr("role", "img").attr("aria-label", "Tables of " + summary.pipeline);
    svg.append("g").attr("class", "nv-ov-links").selectAll("path").data(rootNode.links()).join("path")
      .attr("d", d3.linkHorizontal().x((d) => d.y).y((d) => d.x))
      .attr("class", (d) => "nv-ov-link" + (d.target.data.t && !d.target.data.t.configured ? " is-off" : ""));
    const node = svg.append("g").selectAll("g").data(rootNode.descendants()).join("g")
      .attr("transform", (d) => "translate(" + d.y + "," + d.x + ")")
      .attr("class", (d) => "nv-ov-node" + (d.depth ? " is-table" : " is-root") + (d.data.t && !d.data.t.configured ? " is-off" : ""))
      .attr("tabindex", (d) => (d.depth ? 0 : null))
      .attr("role", (d) => (d.depth ? "button" : null));
    // Transparent hit area so the whole row is clickable.
    node.filter((d) => d.depth).append("rect").attr("class", "nv-ov-hit").attr("x", -8).attr("y", -rowH / 2).attr("width", 520).attr("height", rowH);
    node.append("circle").attr("r", (d) => (d.depth ? 4 : 7));
    node.append("text").attr("x", (d) => (d.depth ? 9 : -10)).attr("dy", "0.32em")
      .attr("text-anchor", (d) => (d.depth ? "start" : "end")).text((d) => d.data.name);
    const strip = node.filter((d) => d.depth).append("g").attr("transform", "translate(170,-6)");
    STAGES.forEach((st, i) => {
      strip.append("rect").attr("x", i * 15).attr("width", 12).attr("height", 12).attr("rx", 2)
        .attr("class", (d) => "nv-ov-stage is-" + st + ((d.data.t.stages[st] || {}).count ? " on" : "") + ((d.data.t.stages[st] || {}).opaque ? " is-opaque" : ""))
        .append("title").text((d) => st + ": " + ((d.data.t.ops[st] || []).join(", ") || "—"));
    });
    node.filter((d) => d.depth).append("text").attr("x", 252).attr("dy", "0.32em").attr("class", "nv-ov-meta")
      .text((d) => d.data.t.fields + " fields" + (d.data.t.has_udf ? " · ƒ" : "") + (d.data.t.chains.length ? " · " + d.data.t.chains.join(", ") : ""));
    node.filter((d) => d.depth).on("click", (_e, d) => select({ p: summary.pipeline, t: d.data.name }))
      .on("keydown", (e, d) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select({ p: summary.pipeline, t: d.data.name }); } });
  }
  $$("[data-goto-table]", host).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); select({ p: summary.pipeline, t: a.dataset.gotoTable }); }));
}

function chipHtml(s, field, L) {
  const role = field ? (s.writes.includes(field) ? "is-write" : s.reads.includes(field) ? "is-read" : "") : "";
  const cls = ["nv-chip", "is-" + s.stage, role, s.opaque ? "is-opaque" : "", s.editable ? "" : "is-readonly", s.filters_rows ? "is-filter" : ""].filter(Boolean).join(" ");
  const tipText = [s.op, s.chain ? "chain " + s.chain : "", s.reads.length ? "reads " + s.reads.join(", ") : "", s.writes.length ? "writes " + s.writes.join(", ") : "",
    s.tables.length ? "uses " + s.tables.join(", ") : "", s.filters_rows ? "drops rows" : "", s.note].filter(Boolean).join("\n");
  return '<button type="button" class="' + cls + '" data-step="' + esc(s.id) + '" data-field="' + esc(field || "") + '" title="' + esc(tipText) + '">' +
    (s.filters_rows ? "⊘ " : "") + esc(s.label) + "</button>";
}

function drawLanes(L) {
  const host = graphHost();
  const byId = Object.fromEntries(L.steps.map((s) => [s.id, s]));
  const cols = STAGES.filter((s) => state.stages.has(s));
  const grid = "minmax(9rem, 13rem) " + cols.map(() => "minmax(7rem, 1fr)").join(" ");
  const wide = L.table_steps.map((id) => byId[id]).filter(Boolean);
  const head = '<div class="nv-lane nv-lane-head" style="grid-template-columns:' + grid + '"><div class="nv-lane-name">field</div>' +
    cols.map((s) => '<div class="nv-lane-stage is-' + s + '">' + s + " <span class=\"nv-badge\">" + (L.stage_summary[s] ? L.stage_summary[s].count : 0) + "</span></div>").join("") + "</div>";
  const band = wide.length ? '<div class="nv-lane nv-lane-band" style="grid-template-columns:' + grid + '"><div class="nv-lane-name">table-wide ' + tip("Steps that act on whole rows or the table's shape: dedup, joins, row filters, reshapes.") + "</div>" +
    cols.map((st) => '<div class="nv-lane-cell">' + wide.filter((s) => s.stage === st).map((s) => chipHtml(s, "", L)).join("") + "</div>").join("") + "</div>" : "";
  const udfBtn = CAN_WRITE && L.editable;
  const rows = L.fields.map((f) => {
    const steps = f.steps.map((id) => byId[id]).filter(Boolean);
    const status = f.status !== "ok" ? '<span class="nv-flag is-' + (f.status === "extra" ? "info" : "warn") + '">' + esc(f.status) + "</span>" : "";
    return '<div class="nv-lane nv-lane-field' + (state.sel.f === f.name ? " is-current" : "") + '" style="grid-template-columns:' + grid + '" data-lane="' + esc(f.name) + '" data-search="' +
      esc((f.name + " " + steps.map((s) => s.op + " " + s.label).join(" ")).toLowerCase()) + '" data-udf="' + (steps.some((s) => s.kind === "transform") ? "1" : "") +
      '" data-opaque="' + (steps.some((s) => s.opaque) ? "1" : "") + '" data-unmapped="' + (["unmapped", "constant"].includes(f.status) ? "1" : "") + '">' +
      '<div class="nv-lane-name"><button type="button" class="nv-field-link mono" data-goto-field="' + esc(f.name) + '">' + esc(f.name) + "</button>" +
      (f.is_key ? '<span class="nv-flag">key</span>' : "") + status +
      (udfBtn ? '<button type="button" class="nv-udf-add" data-udf-field="' + esc(f.name) + '" title="Apply a UDF to ' + esc(f.name) + '">+ƒ</button>' : "") + "</div>" +
      cols.map((st) => '<div class="nv-lane-cell">' + steps.filter((s) => s.stage === st).map((s) => chipHtml(s, f.name, L)).join("") + "</div>").join("") + "</div>";
  }).join("");
  host.innerHTML = '<div class="nv-lanes-wrap"><div class="nv-lanes-tools">' +
    (udfBtn ? '<button type="button" class="button small" data-udf-table>' + "+ UDF for " + esc(L.table) + "</button>" : "") +
    (L.editable ? "" : '<span class="nv-flag">read-only</span>') +
    (L.warnings.length ? '<span class="flash error small">' + esc(L.warnings.join("; ")) + "</span>" : "") +
    '<a class="small" href="/pipeline-packages/' + enc(L.pipeline) + "/builder?table=" + enc(L.table) + '">open in builder</a></div>' +
    '<div class="nv-lanes" role="table" aria-label="Field lanes">' + head + band + rows + '</div><svg class="nv-wires" aria-hidden="true"></svg></div>';
  filterLanes();
  if (state.sel.f) {
    const r = $('[data-lane="' + CSS.escape(state.sel.f) + '"]', host);
    if (r) r.scrollIntoView({ block: "nearest" });
  }
}

function filterLanes() {
  const q = state.filter.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  for (const r of $$(".nv-lane-field", graphHost())) {
    const hit = terms.every((x) => (r.dataset.search || "").includes(x));
    const only = state.only;
    const ok = !only || only === "editable" || (only === "udf" ? !!r.dataset.udf : only === "opaque" ? !!r.dataset.opaque : !!r.dataset.unmapped);
    r.hidden = !(hit && ok) && r.dataset.lane !== state.sel.f;
  }
}

/* Hovering / focusing a chip draws lines from every column it reads. */
function drawWires(chip) {
  const wrap = chip.closest(".nv-lanes-wrap");
  if (!wrap) return;
  const svg = $(".nv-wires", wrap);
  const L = cache.table[state.sel.p + "/" + state.sel.t];
  const s = L && L.steps.find((x) => x.id === chip.dataset.step);
  $$(".nv-lane-field.is-src", wrap).forEach((r) => r.classList.remove("is-src"));
  svg.innerHTML = "";
  if (!s || !s.reads.length) return;
  const box = wrap.getBoundingClientRect();
  svg.setAttribute("width", wrap.scrollWidth);
  svg.setAttribute("height", wrap.scrollHeight);
  const c = chip.getBoundingClientRect();
  const x2 = c.left - box.left + wrap.scrollLeft, y2 = c.top - box.top + c.height / 2 + wrap.scrollTop;
  for (const col of s.reads) {
    const row = $('[data-lane="' + CSS.escape(col) + '"]', wrap);
    if (!row || row.hidden) continue;
    row.classList.add("is-src");
    const n = $(".nv-lane-name", row).getBoundingClientRect();
    const x1 = n.right - box.left + wrap.scrollLeft, y1 = n.top - box.top + n.height / 2 + wrap.scrollTop;
    const mid = (x1 + x2) / 2;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M" + x1 + "," + y1 + " C" + mid + "," + y1 + " " + mid + "," + y2 + " " + x2 + "," + y2);
    path.setAttribute("class", "nv-wire is-" + s.stage);
    svg.appendChild(path);
  }
}

function fieldDoc(L, name) {
  const f = L.fields.find((x) => x.name === name);
  if (!f) return {};
  const byId = Object.fromEntries(L.steps.map((s) => [s.id, s]));
  return {
    pipeline: L.pipeline, table: L.table, field: f.name, type: f.rdbms_type, key: f.is_key, status: f.status,
    valueset: f.valueset.length ? f.valueset : undefined, sources: f.sources.length ? f.sources : undefined,
    chain: f.steps.map((id) => byId[id]).filter(Boolean).map((s) => {
      const o = { stage: s.stage, op: s.op };
      if (Object.keys(s.params).length) o.params = s.params;
      if (s.reads.length) o.reads = s.reads;
      if (s.writes.length) o.writes = s.writes;
      if (s.chain) o.chain = s.chain;
      o.source = s.source_ref.path || s.source_ref.name || s.source_doc;
      if (s.step_path) o.at = s.step_path;
      return o;
    }),
  };
}

async function drawYaml(obj) {
  const m = await yamlMod();
  const text = m.yamlStringify(JSON.parse(JSON.stringify(obj)), { lineWidth: 0 });
  graphHost().innerHTML = '<pre class="nv-yaml mono">' + esc(text) + "</pre>";
}

/* --------------------------------------------------------- side panel */

const stepPanel = () => $("#nv-step-panel");

function panel(title, sub, html) {
  const p = stepPanel();
  p.hidden = false;
  $("[data-nv-step-title]", p).innerHTML = title;
  $("[data-nv-step-sub]", p).textContent = sub || "";
  $("[data-nv-step-body]", p).innerHTML = html;
}

function drawPackagePanel(summary, facts) {
  $("#pb-editor-panel").hidden = true;
  const cfgs = (facts.configs || []).map((c) => '<a class="nw-chip" href="/pipelines/' + enc(c.name) + '">' + esc(c.name) + " · " + esc(c.kind) + "</a>").join(" ");
  const configured = summary.tables.filter((t) => t.configured).length;
  panel('<span class="mono">' + esc(summary.pipeline) + "</span>", summary.cdm + " · " + summary.profile,
    '<dl class="nv-facts"><dt>tables</dt><dd>' + configured + " / " + summary.tables.length + " configured</dd>" +
    "<dt>pipelines</dt><dd>" + (cfgs || '<span class="nw-muted">none use it yet</span>') + "</dd>" +
    (summary.multi_table_fixes.length ? "<dt>multi-table fixes</dt><dd>" + summary.multi_table_fixes.map((x) => '<span class="nv-chip is-fix">' + esc(x) + "</span>").join(" ") + "</dd>" : "") +
    "<dt>editing</dt><dd>" + (summary.editable ? (CAN_WRITE ? "editable" : "read-only (needs manage-config)") : "generated port — read-only") + "</dd></dl>" +
    '<p class="nv-links small"><a href="/pipeline-packages/' + enc(summary.pipeline) + '">package hub</a> · <a href="/pipeline-packages/' + enc(summary.pipeline) + '/builder">builder</a></p>');
}

function drawTablePanel(L) {
  $("#pb-editor-panel").hidden = true;
  const counts = {};
  for (const f of L.fields) counts[f.status] = (counts[f.status] || 0) + 1;
  panel('<span class="mono">' + esc(L.table) + "</span>", L.fields.length + " fields",
    '<dl class="nv-facts">' + Object.entries(counts).map(([k, v]) => "<dt>" + esc(k) + "</dt><dd>" + v + "</dd>").join("") +
    STAGES.map((s) => "<dt>" + s + "</dt><dd>" + (L.stage_summary[s].count || 0) + " step(s)" + (L.stage_summary[s].source ? " · " + esc(L.stage_summary[s].source) : "") + "</dd>").join("") + "</dl>" +
    (L.extra_columns.length ? '<p class="small">Columns outside the CDM: ' + L.extra_columns.map((c) => "<code>" + esc(c) + "</code>").join(" ") + " " + tip("Referenced or projected by a step but not a column of this CDM table (e.g. a placeholder or a dropped column).") + "</p>" : ""));
}

function chainHtml(L, steps, field) {
  return '<ol class="nv-chain">' + steps.map((s) => '<li class="is-' + s.stage + '"><span class="nv-stage-pill is-' + s.stage + '">' + esc(s.stage) + "</span>" + chipHtml(s, field, L) +
    (s.reads.filter((r) => r !== field).length ? ' <span class="small nw-muted">← ' + s.reads.filter((r) => r !== field).map((r) => '<button type="button" class="nv-field-link mono" data-goto-field="' + esc(r) + '">' + esc(r) + "</button>").join(", ") + "</span>" : "") + "</li>").join("") + "</ol>";
}

function drawFieldPanel(L, name) {
  $("#pb-editor-panel").hidden = true;
  const f = L.fields.find((x) => x.name === name);
  if (!f) { panel(esc(name), "", '<p class="empty">Not a field of this table.</p>'); return; }
  const byId = Object.fromEntries(L.steps.map((s) => [s.id, s]));
  const steps = f.steps.map((id) => byId[id]).filter((s) => s && state.stages.has(s.stage));
  const usedBy = L.steps.filter((s) => s.reads.includes(f.name) && !s.writes.includes(f.name));
  panel('<span class="mono">' + esc(f.name) + "</span>", [f.rdbms_type, f.is_key ? "key" : "", f.status !== "ok" ? f.status : ""].filter(Boolean).join(" · "),
    (f.sources.length ? '<p class="small">from source <code>' + f.sources.map(esc).join("</code>, <code>") + "</code></p>" : "") +
    (steps.length ? chainHtml(L, steps, f.name) : '<p class="empty">No step touches this field.</p>') +
    (usedBy.length ? '<p class="small">read by ' + usedBy.map((s) => chipHtml(s, f.name, L)).join(" ") + "</p>" : "") +
    (f.valueset.length ? '<details class="nv-valueset"><summary class="small">valueset (' + f.valueset.length + ")</summary><p>" + f.valueset.map((v) => "<code>" + esc(v) + "</code>").join(" ") + "</p></details>" : "") +
    (CAN_WRITE && L.editable ? '<p><button type="button" class="button small" data-udf-field="' + esc(f.name) + '">+ Apply UDF to ' + esc(f.name) + "</button></p>" : ""));
}

/* Open the document that defines a step, scrolled to it. */
DOCS.source = {
  icon: "file",
  async load(ref) {
    const p = ref.pipeline;
    const path = ref.path || "";
    if (path.startsWith("cdm:") || path.startsWith("partners/")) {
      const d = await api("GET", "/pipeline-packages/" + enc(p) + "/lineage/source?path=" + enc(path));
      return { text: d.text, path, readonly: true, code: path.endsWith(".py"), note: "Generated / vendored source — read-only." };
    }
    const d = await api("GET", "/pipeline-packages/" + enc(p) + "/file?relative_path=" + enc(path));
    return { text: d.content, path, readonly: true, code: path.endsWith(".py"), note: "Python source — read-only here." };
  },
};

function docFor(s, p) {
  const label = s.label;
  switch (s.source_doc) {
    case "formatter": return ["formatter", { table: s.source_ref.table, label: s.source_ref.table + " · formatter" }];
    case "mapper": return ["mapper", { table: s.source_ref.table, label: s.source_ref.table + " · mapper" }];
    case "fixers": return ["fixers", { table: s.source_ref.table, label: s.source_ref.table + " · fix chain" }];
    case "assignments": return ["assignments", { table: s.source_ref.table, label: (s.source_ref.table === "*" ? "every table" : s.source_ref.table) + " · transforms" }];
    case "transform": return ["transform", { scope: s.source_ref.scope === "pipeline" || s.source_ref.scope === "pipeline_py" ? "pipeline" : s.source_ref.scope, name: s.source_ref.name, label: s.source_ref.name + " (" + s.source_ref.scope + ")" }];
    case "file": case "vendor_file": case "cdm_mapper": return ["source", { pipeline: p, path: s.source_ref.path, label }];
    default: return null;
  }
}

/* Find the line a lineage step_path (``ops[3]``, ``tables.x[1].steps[0]``,
 * ``projections.SEX``, ``fixes[2]``) points at in YAML / a legacy script. */
function lineForStep(text, stepPath) {
  const lines = String(text || "").split("\n");
  if (!stepPath) return 0;
  const fixes = /^fixes\[(\d+)\]$/.exec(stepPath);
  if (fixes) {
    const i = lines.findIndex((l) => new RegExp('^\\s*"' + fixes[1] + '"\\s*:').test(l));
    return i + 1;
  }
  const tokens = [];
  for (const part of stepPath.split(".")) {
    const m = /^([^[\]]*)((?:\[\d+\])*)$/.exec(part);
    if (!m) return 0;
    if (m[1]) tokens.push({ key: m[1] });
    for (const ix of m[2].matchAll(/\[(\d+)\]/g)) tokens.push({ index: Number(ix[1]) });
  }
  const indent = (l) => l.length - l.trimStart().length;
  let at = -1, minIndent = -1;
  for (const tok of tokens) {
    let found = -1;
    if (tok.key !== undefined) {
      const re = new RegExp("^\\s*(?:-\\s+)?[\"']?" + tok.key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "[\"']?\\s*:");
      for (let i = at + 1; i < lines.length; i++) {
        if (lines[i].trim() && indent(lines[i]) <= minIndent && i > at + 1 && at >= 0) break;
        if (re.test(lines[i])) { found = i; break; }
      }
      if (found < 0) return at + 1;
      minIndent = indent(lines[found]);
    } else {
      let n = -1, itemIndent = -1;
      for (let i = at + 1; i < lines.length; i++) {
        const l = lines[i];
        if (!l.trim() || l.trim().startsWith("#")) continue;
        if (indent(l) < minIndent || (indent(l) === minIndent && !/^\s*-/.test(l) && i > at + 1)) break;
        if (/^\s*-(\s|$)/.test(l) && (itemIndent < 0 || indent(l) === itemIndent)) {
          itemIndent = indent(l);
          n++;
          if (n === tok.index) { found = i; break; }
        }
      }
      if (found < 0) return at + 1;
      minIndent = indent(lines[found]);
    }
    at = found;
  }
  return at + 1;
}

let editor = null;

/* Python sources (custom / generated formatters, vendored legacy fixers)
 * are shown in a plain, line-numbered viewer — never the YAML editor. */
async function showCode(p, path, stepPath, note) {
  const ep = $("#pb-editor-panel");
  ep.hidden = true;
  const host = $("[data-nv-step-body]", stepPanel());
  let text;
  try {
    if (path.startsWith("cdm:") || path.startsWith("partners/")) text = (await api("GET", "/pipeline-packages/" + enc(p) + "/lineage/source?path=" + enc(path))).text;
    else text = (await api("GET", "/pipeline-packages/" + enc(p) + "/file?relative_path=" + enc(path))).content;
  } catch (err) { host.insertAdjacentHTML("beforeend", '<p class="flash error">' + esc(err.message) + "</p>"); return; }
  const line = lineForStep(text, stepPath);
  const rows = text.split("\n").map((l, i) => '<span class="nv-code-line' + (i + 1 === line ? " is-hit" : "") + '" data-line="' + (i + 1) + '">' + esc(l) + "</span>").join("\n");
  host.insertAdjacentHTML("beforeend", '<div class="nv-code-head small"><code>' + esc(path) + "</code> " + tip(note || "Python source — read-only. Generated ports are regenerated, never hand-edited.") + '</div><pre class="nv-code mono">' + rows + "</pre>");
  const hit = $(".nv-code-line.is-hit", host);
  if (hit) hit.scrollIntoView({ block: "center" });
}

async function openStepDoc(s, p) {
  const spec = docFor(s, p);
  const ep = $("#pb-editor-panel");
  if (!spec) { ep.hidden = true; return; }
  if (spec[0] === "source" && /\.py$/.test(spec[1].path || "")) { await showCode(p, spec[1].path, s.step_path, s.note); return; }
  const facts = pkgFacts(p) || {};
  let ins;
  try { ins = await inspection(p); } catch (_e) { ins = null; }
  setContext({ pipeline: p, canWrite: CAN_WRITE && s.editable && facts.editable !== false, inspection: ins });
  ep.hidden = false;
  if (!editor) editor = new Editor(ep);
  const [type, ref] = spec;
  const ok = await editor.open(type, ref, { tab: s.step_path ? "yaml" : "form" });
  if (!ok) return;
  if (!s.editable && editor.saveBtn) {
    editor.readonly = true;
    editor.saveBtn.hidden = true;
    editor.revertBtn.hidden = true;
    if (!editor.status.textContent) editor.setStatus(s.note || "Read-only source.", "");
  }
  if (s.step_path && editor.cm) {
    editor.showTab("yaml");
    const line = lineForStep(editor.cm.getValue(), s.step_path);
    if (line > 0) editor.cm.focusLine(line);
  }
}

async function showStep(L, id) {
  const s = L.steps.find((x) => x.id === id);
  if (!s) return;
  const params = Object.keys(s.params).length ? (await yamlMod()).yamlStringify(s.params, { lineWidth: 0 }) : "";
  const link = (c) => '<button type="button" class="nv-field-link mono" data-goto-field="' + esc(c) + '">' + esc(c) + "</button>";
  panel('<span class="nv-stage-pill is-' + s.stage + '">' + esc(s.stage) + "</span> " + esc(s.label), s.kind + (s.chain ? " · chain " + s.chain : ""),
    '<dl class="nv-facts">' +
    (s.writes.length ? "<dt>writes</dt><dd>" + s.writes.map(link).join(", ") + "</dd>" : "") +
    (s.reads.length ? "<dt>reads</dt><dd>" + s.reads.map(link).join(", ") + "</dd>" : "") +
    (s.tables.length ? "<dt>uses table</dt><dd>" + s.tables.map((t) => "<code>" + esc(t) + "</code>").join(", ") + "</dd>" : "") +
    "<dt>scope</dt><dd>" + esc(s.scope) + (s.filters_rows ? " · drops rows" : "") + "</dd>" +
    "<dt>defined in</dt><dd><code>" + esc(s.source_ref.path || s.source_ref.name || s.source_ref.table || s.source_doc || "—") + "</code>" + (s.step_path ? " <code>" + esc(s.step_path) + "</code>" : "") + "</dd>" +
    (s.note ? "<dt>note</dt><dd>" + esc(s.note) + "</dd>" : "") + "</dl>" +
    (params ? '<pre class="nv-yaml mono small">' + esc(params) + "</pre>" : ""));
  await openStepDoc(s, L.pipeline);
}

/* --------------------------------------------------------------- boot */

async function loadTree() {
  cache.tree = await api("GET", "/navigator?group_by=" + enc(state.groupBy));
  renderTree();
}

function rowsVisible() {
  return $$(".nv-row", $("[data-nv-tree-body]")).filter((r) => r.offsetParent !== null);
}

async function boot() {
  try { cache.tree = JSON.parse($("#nv-tree-data").textContent); } catch (_e) { cache.tree = null; }
  if (!cache.tree || cache.tree.group_by !== state.groupBy) await loadTree(); else renderTree();
  $("[data-nv-group]").value = state.groupBy;
  for (const cb of $$("[data-nv-stage]")) cb.checked = state.stages.has(cb.dataset.nvStage);

  const tree = $("#nv-tree");
  tree.addEventListener("click", async (e) => {
    const row = e.target.closest(".nv-row");
    if (!row) return;
    const node = row.closest(".nv-node");
    const kind = node.dataset.node;
    const onCaret = !!e.target.closest(".nv-caret");
    const isOpen = node.getAttribute("aria-expanded") === "true";
    if (kind === "group" || kind === "tablewide") { isOpen ? collapse(node) : await expand(node); return; }
    if (kind === "step") {
      await select({ p: node.dataset.pipeline, t: node.dataset.table, f: node.dataset.field, step: node.dataset.step }, { reveal: false });
      return;
    }
    if (onCaret) { isOpen ? collapse(node) : await expand(node); return; }
    if (!isOpen) await expand(node);
    if (kind === "pipeline") await select({ p: node.dataset.pipeline }, { reveal: false });
    else if (kind === "table") await select({ p: node.dataset.pipeline, t: node.dataset.table }, { reveal: false });
    else if (kind === "field") await select({ p: node.dataset.pipeline, t: node.dataset.table, f: node.dataset.field }, { reveal: false });
  });
  tree.addEventListener("keydown", async (e) => {
    const row = e.target.closest(".nv-row");
    if (!row) return;
    const rows = rowsVisible();
    const i = rows.indexOf(row);
    const node = row.closest(".nv-node");
    if (e.key === "ArrowDown" && rows[i + 1]) { e.preventDefault(); rows[i + 1].focus(); }
    else if (e.key === "ArrowUp" && rows[i - 1]) { e.preventDefault(); rows[i - 1].focus(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); if (node.getAttribute("aria-expanded") === "false") await expand(node); else if (rows[i + 1]) rows[i + 1].focus(); }
    else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (node.getAttribute("aria-expanded") === "true") collapse(node);
      else { const parent = node.parentElement.closest(".nv-node"); if (parent) $(":scope > .nv-row", parent).focus(); }
    }
  });

  const filter = $("[data-nv-filter]");
  let t = 0;
  filter.addEventListener("input", () => { window.clearTimeout(t); t = window.setTimeout(() => { state.filter = filter.value; applyFilter(); }, 120); });
  filter.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); state.filter = filter.value; deepSearch(); } });
  $("[data-nv-only]").addEventListener("change", async (e) => {
    state.only = e.target.value;
    if (state.only && state.only !== "editable") await deepSearch(); else applyFilter();
  });
  $("[data-nv-group]").addEventListener("change", async (e) => {
    state.groupBy = e.target.value;
    persist();
    await loadTree();
    setUrl(false);
    if (state.sel.p) await revealPath(state.sel);
  });
  for (const cb of $$("[data-nv-stage]")) cb.addEventListener("change", () => {
    if (cb.checked) state.stages.add(cb.dataset.nvStage); else state.stages.delete(cb.dataset.nvStage);
    persist();
    applyFilter();
    if (state.sel.t) drawMain();
  });
  $("[data-nv-expand]").addEventListener("click", async () => {
    for (const n of $$('[data-node="group"], [data-node="pipeline"]').filter((x) => !x.hidden)) await expand(n, { quiet: true });
    persist();
  });
  $("[data-nv-collapse]").addEventListener("click", () => {
    for (const n of $$(".nv-node[aria-expanded=\"true\"]")) if (n.dataset.node !== "group") collapse(n);
  });
  $("[data-nv-views]").addEventListener("click", (e) => {
    const b = e.target.closest("[data-view]");
    if (!b) return;
    state.view = b.dataset.view;
    persist();
    drawMain();
  });

  // Chips / field links anywhere in the main column.
  const main = $(".nv-main");
  main.addEventListener("click", async (e) => {
    const udfF = e.target.closest("[data-udf-field]");
    const udfT = e.target.closest("[data-udf-table]");
    if (udfF || udfT) {
      const mod = await import("/static/udf_dialog.js");
      const L = await tableLineage(state.sel.p, state.sel.t);
      mod.openUdfDialog({ pipeline: state.sel.p, table: state.sel.t, field: udfF ? udfF.dataset.udfField : "", lineage: L, facts: pkgFacts(state.sel.p) || {} });
      return;
    }
    const chip = e.target.closest(".nv-chip[data-step]");
    if (chip) {
      await select({ p: state.sel.p, t: state.sel.t, f: chip.dataset.field || state.sel.f, step: chip.dataset.step }, { reveal: !!chip.dataset.field });
      return;
    }
    const f = e.target.closest("[data-goto-field]");
    if (f) await select({ p: state.sel.p, t: state.sel.t, f: f.dataset.gotoField });
  });
  const wire = (e) => { const chip = e.target.closest(".nv-lanes .nv-chip[data-step]"); if (chip) drawWires(chip); };
  main.addEventListener("mouseover", wire);
  main.addEventListener("focusin", wire);

  document.addEventListener("ovid2:spec-saved", async () => {
    // Re-read the lineage and redraw tree + lanes; keep the editor open.
    const p = state.sel.p;
    if (!p) return;
    invalidate(p);
    const pn = $('[data-node="pipeline"][data-pipeline="' + CSS.escape(p) + '"]');
    if (pn) {
      const box = $(":scope > .nv-children", pn);
      delete box.dataset.loaded;
      if (pn.getAttribute("aria-expanded") === "true") await fillPackage(pn);
      if (state.sel.t) await revealPath(state.sel);
      markSelection();
    }
    await drawMain({ keepPanel: true });
  });
  document.addEventListener("ovid2:udf-applied", async (e) => {
    document.dispatchEvent(new CustomEvent("ovid2:spec-saved", { detail: { type: "assignments", ref: {} } }));
    if (e.detail && e.detail.field) await select({ p: state.sel.p, t: state.sel.t, f: e.detail.field }, { push: false });
  });
  window.addEventListener("popstate", () => {
    const qs = new URLSearchParams(location.search);
    select({ p: qs.get("p") || "", t: qs.get("t") || "", f: qs.get("f") || "" }, { push: false });
  });

  const init = { p: root.dataset.initialP, t: root.dataset.initialT, f: root.dataset.initialF };
  if (init.p) await select(init, { push: false });
  else applyFilter();
}

if (root) {
  boot().catch((err) => {
    const host = graphHost();
    if (host) host.innerHTML = '<p class="flash error">Navigator failed to start: ' + esc(err.message) + "</p>";
  });
}

export { lineForStep };
