/*
 * Ovid Atlas — lineage: a table's field lanes and the spec editor.
 *
 * Ported from the former /navigator page (it is now the deepest zoom of
 * the Atlas). One row per CDM field × one column per stage; each chip is a
 * function applied to the field; table-wide steps are a band. Clicking a
 * chip opens the document that defines it in the shared spec editor
 * (static/spec_builder.js) scrolled to the step; generated / vendored
 * sources open read-only. Hovering a field highlights its *blast radius*:
 * every step that reads it and every field those steps write, transitively.
 */

import { Editor, DOCS, api as sbApi, setContext, yamlMod } from "/static/spec_builder.js";
import { state } from "/static/atlas/state.js";
import { $, $$, api, enc, esc, tip } from "/static/atlas/util.js";

const STAGES = ["format", "deduplicate", "transform", "map", "fix"];

export async function pkgSummary(p) {
  if (!state.cache.pkg[p]) state.cache.pkg[p] = await api("GET", "/pipeline-packages/" + enc(p) + "/lineage");
  return state.cache.pkg[p];
}

export async function tableLineage(p, t) {
  const k = p + "/" + t;
  if (!state.cache.table[k]) state.cache.table[k] = await api("GET", "/pipeline-packages/" + enc(p) + "/lineage?table=" + enc(t));
  return state.cache.table[k];
}

async function inspection(p) {
  if (!state.cache.inspect[p]) state.cache.inspect[p] = await api("GET", "/pipeline-packages/" + enc(p) + "/inspect");
  return state.cache.inspect[p];
}

export function invalidatePackage(p) {
  delete state.cache.pkg[p];
  delete state.cache.inspect[p];
  for (const k of Object.keys(state.cache.table)) if (k.startsWith(p + "/")) delete state.cache.table[k];
}

function chipHtml(s, field) {
  const role = field ? (s.writes.includes(field) ? "is-write" : s.reads.includes(field) ? "is-read" : "") : "";
  const cls = ["nv-chip", "is-" + s.stage, role, s.opaque ? "is-opaque" : "", s.editable ? "" : "is-readonly", s.filters_rows ? "is-filter" : ""].filter(Boolean).join(" ");
  const t = [s.op, s.chain ? "chain " + s.chain : "", s.reads.length ? "reads " + s.reads.join(", ") : "", s.writes.length ? "writes " + s.writes.join(", ") : "",
    s.tables.length ? "uses " + s.tables.join(", ") : "", s.filters_rows ? "drops rows" : "", s.note].filter(Boolean).join("\n");
  return '<button type="button" class="' + cls + '" data-step="' + esc(s.id) + '" data-field="' + esc(field || "") + '" title="' + esc(t) + '">' +
    (s.filters_rows ? "⊘ " : "") + esc(s.label) + "</button>";
}

/* Downstream of ``field`` inside the table (client-side twin of atlas_svc.field_impact). */
export function fieldBlast(L, field) {
  const fields = new Set([field]);
  const steps = new Set();
  let grew = true;
  while (grew) {
    grew = false;
    for (const s of L.steps) {
      if (steps.has(s.id)) continue;
      if (s.reads.some((r) => fields.has(r))) {
        steps.add(s.id);
        for (const w of s.writes) fields.add(w);
        grew = true;
      }
    }
  }
  fields.delete(field);
  return { fields, steps };
}

/* Render the lanes of ``p``/``t`` into ``host``. ``health`` = failed checks of this table. */
export async function drawLanes(host, p, t, { health = [], canWrite = false, onOpenStep } = {}) {
  host.innerHTML = '<p class="atl-loading small">loading lineage of ' + esc(t) + "…</p>";
  let L;
  try { L = await tableLineage(p, t); } catch (err) { host.innerHTML = '<p class="flash error">' + esc(err.message) + "</p>"; return; }
  const byId = Object.fromEntries(L.steps.map((s) => [s.id, s]));
  const cols = STAGES;
  const grid = "minmax(9rem, 13rem) " + cols.map(() => "minmax(7rem, 1fr)").join(" ");
  const wide = L.table_steps.map((id) => byId[id]).filter(Boolean);
  const head = '<div class="nv-lane nv-lane-head" style="grid-template-columns:' + grid + '"><div class="nv-lane-name">field</div>' +
    cols.map((s) => '<div class="nv-lane-stage is-' + s + '">' + s + ' <span class="nv-badge">' + (L.stage_summary[s] ? L.stage_summary[s].count : 0) + "</span></div>").join("") + "</div>";
  const band = wide.length ? '<div class="nv-lane nv-lane-band" style="grid-template-columns:' + grid + '"><div class="nv-lane-name">table-wide ' + tip("Steps that act on whole rows or the table's shape: dedup, joins, row filters, reshapes.") + "</div>" +
    cols.map((st) => '<div class="nv-lane-cell">' + wide.filter((s) => s.stage === st).map((s) => chipHtml(s, "")).join("") + "</div>").join("") + "</div>" : "";
  const udf = canWrite && L.editable;
  const rows = L.fields.map((f) => {
    const steps = f.steps.map((id) => byId[id]).filter(Boolean);
    const status = f.status !== "ok" ? '<span class="nv-flag is-' + (f.status === "extra" ? "info" : "warn") + '">' + esc(f.status) + "</span>" : "";
    return '<div class="nv-lane nv-lane-field" style="grid-template-columns:' + grid + '" data-lane="' + esc(f.name) + '">' +
      '<div class="nv-lane-name"><button type="button" class="nv-field-link mono" data-field-name="' + esc(f.name) + '">' + esc(f.name) + "</button>" +
      (f.is_key ? '<span class="nv-flag">key</span>' : "") + status +
      (udf ? '<button type="button" class="nv-udf-add" data-udf-field="' + esc(f.name) + '" title="Apply a UDF to ' + esc(f.name) + '">+ƒ</button>' : "") + "</div>" +
      cols.map((st) => '<div class="nv-lane-cell">' + steps.filter((s) => s.stage === st).map((s) => chipHtml(s, f.name)).join("") + "</div>").join("") + "</div>";
  }).join("");
  const healthHtml = health.length ? '<div class="atl-health-list small">' + health.map((h) =>
    '<span class="atl-badge is-health sev-' + esc(h.severity) + '" title="' + esc((h.remediation && h.remediation.hint) || "") + '">✚ ' + esc(h.check_id) + " · " + esc(h.name || "") + " · " + h.exceptions + "</span>" +
    ((h.remediation && h.remediation.fixers || []).length ? ' <button type="button" class="button small" data-remediate="fixers" title="Open the fix chain">fix: ' + esc(h.remediation.fixers.join(", ")) + "</button>" : "")).join(" ") + "</div>" : "";
  host.innerHTML = '<div class="nv-lanes-wrap"><div class="nv-lanes-tools">' +
    '<span class="mono">' + esc(p) + " · " + esc(t) + "</span>" +
    (udf ? '<button type="button" class="button small" data-udf-table>+ UDF for ' + esc(t) + "</button>" : "") +
    (L.editable ? "" : '<span class="nv-flag">read-only port</span>') +
    (L.warnings.length ? '<span class="flash error small">' + esc(L.warnings.join("; ")) + "</span>" : "") +
    tip("Hover a field to see everything downstream of it (its blast radius). Click a chip to open the YAML that defines it.") +
    "</div>" + healthHtml +
    '<div class="nv-lanes" role="table" aria-label="Field lanes">' + head + band + rows + "</div></div>";
  host.onclick = async (e) => {
    const chip = e.target.closest(".nv-chip[data-step]");
    if (chip) { const s = byId[chip.dataset.step]; if (s && onOpenStep) onOpenStep(L, s); return; }
    const udfF = e.target.closest("[data-udf-field]");
    const udfT = e.target.closest("[data-udf-table]");
    if (udfF || udfT) {
      const mod = await import("/static/udf_dialog.js");
      mod.openUdfDialog({ pipeline: p, table: t, field: udfF ? udfF.dataset.udfField : "", lineage: L });
      return;
    }
    const rem = e.target.closest("[data-remediate]");
    if (rem && onOpenStep) { onOpenStep(L, { source_doc: "fixers", source_ref: { table: t }, editable: L.editable, step_path: "", label: "fix chain" }); }
  };
  const blast = (name) => {
    $$(".nv-lane-field", host).forEach((r) => r.classList.remove("is-src", "is-blast"));
    $$(".nv-chip", host).forEach((c) => c.classList.remove("is-blast"));
    if (!name) return;
    const b = fieldBlast(L, name);
    const src = $('[data-lane="' + CSS.escape(name) + '"]', host);
    if (src) src.classList.add("is-src");
    for (const f of b.fields) { const r = $('[data-lane="' + CSS.escape(f) + '"]', host); if (r) r.classList.add("is-blast"); }
    for (const id of b.steps) $$('.nv-chip[data-step="' + CSS.escape(id) + '"]', host).forEach((c) => c.classList.add("is-blast"));
  };
  host.onmouseover = (e) => { const f = e.target.closest("[data-field-name]"); blast(f ? f.dataset.fieldName : ""); };
  host.onmouseleave = () => blast("");
  return L;
}

/* ------------------------------------------------------------ editor */

DOCS.source = {
  icon: "file",
  async load(ref) {
    const p = ref.pipeline;
    const path = ref.path || "";
    if (path.startsWith("cdm:") || path.startsWith("partners/")) {
      const d = await sbApi("GET", "/pipeline-packages/" + enc(p) + "/lineage/source?path=" + enc(path));
      return { text: d.text, path, readonly: true, code: path.endsWith(".py"), note: "Generated / vendored source — read-only." };
    }
    const d = await sbApi("GET", "/pipeline-packages/" + enc(p) + "/file?relative_path=" + enc(path));
    return { text: d.content, path, readonly: true, code: path.endsWith(".py"), note: "Python source — read-only here." };
  },
};

export function docFor(s, p) {
  const ref = s.source_ref || {};
  switch (s.source_doc) {
    case "formatter": return ["formatter", { table: ref.table, label: ref.table + " · formatter" }];
    case "mapper": return ["mapper", { table: ref.table, label: ref.table + " · mapper" }];
    case "fixers": return ["fixers", { table: ref.table, label: (ref.table || "") + " · fix chain" }];
    case "assignments": return ["assignments", { table: ref.table, label: (ref.table === "*" ? "every table" : ref.table) + " · transforms" }];
    case "transform": return ["transform", { scope: ref.scope === "pipeline" || ref.scope === "pipeline_py" ? "pipeline" : ref.scope, name: ref.name, label: ref.name + " (" + ref.scope + ")" }];
    case "settings": return ["settings", { label: "pipeline settings" }];
    case "file": case "vendor_file": case "cdm_mapper": return ["source", { pipeline: p, path: ref.path, label: s.label }];
    default: return null;
  }
}

/* Find the line a lineage step_path (``ops[3]``, ``fixes[2]``, …) points at. */
export function lineForStep(text, stepPath) {
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

export function editorPanel() {
  return document.getElementById("pb-editor-panel");
}

async function showCode(host, p, path, stepPath, note) {
  let text;
  try {
    if (path.startsWith("cdm:") || path.startsWith("partners/")) text = (await api("GET", "/pipeline-packages/" + enc(p) + "/lineage/source?path=" + enc(path))).text;
    else text = (await api("GET", "/pipeline-packages/" + enc(p) + "/file?relative_path=" + enc(path))).content;
  } catch (err) { host.innerHTML = '<p class="flash error">' + esc(err.message) + "</p>"; return; }
  const line = lineForStep(text, stepPath);
  const rows = text.split("\n").map((l, i) => '<span class="nv-code-line' + (i + 1 === line ? " is-hit" : "") + '">' + esc(l) + "</span>").join("\n");
  host.innerHTML = '<div class="nv-code-head small"><code>' + esc(path) + "</code> " + tip(note || "Python source — read-only. Generated ports are regenerated, never hand-edited.") + '</div><pre class="nv-code mono">' + rows + "</pre>";
  const hit = $(".nv-code-line.is-hit", host);
  if (hit) hit.scrollIntoView({ block: "center" });
}

/*
 * Open the document that defines step ``s`` of package ``p`` in ``host``.
 * The shared editor panel is moved into ``host`` (one editor per page).
 */
export async function openStepDoc(host, s, p, { canWrite, editable }) {
  const spec = docFor(s, p);
  if (!spec) { host.innerHTML = '<p class="empty">Nothing to open for this step.</p>'; return; }
  if (spec[0] === "source" && /\.py$/.test(spec[1].path || "")) { await showCode(host, p, spec[1].path, s.step_path, s.note); return; }
  const ep = editorPanel();
  let ins;
  try { ins = await inspection(p); } catch (_e) { ins = null; }
  setContext({ pipeline: p, canWrite: canWrite && s.editable !== false && editable !== false, inspection: ins });
  host.replaceChildren(ep);
  ep.hidden = false;
  if (!editor) editor = new Editor(ep);
  const [type, ref] = spec;
  const ok = await editor.open(type, ref, { tab: s.step_path ? "yaml" : "form" });
  if (!ok) return;
  if (s.editable === false && editor.saveBtn) {
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

export function editorDirty() {
  return !!(editor && editor.dirty);
}

/* Park the editor back in the dock root so a re-render never destroys it. */
export function parkEditor(root) {
  const ep = editorPanel();
  if (ep && root && ep.parentElement !== root) { ep.hidden = true; root.appendChild(ep); }
}

export { yamlMod };
