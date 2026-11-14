/*
 * Ovid2 UDF dialog — "+ UDF" in the pipeline navigator.
 *
 * A UDF is *declarative*: a transform chain (library ops + checked
 * Spark-SQL expressions); Python UDFs are deliberately not supported
 * (see adr-pipeline-builder.md). Three ways to define one:
 *
 *   computed  derive_column — the field (or a new column) = expression,
 *             checked live against the allow-list (POST /expressions:check),
 *             with column / function autocomplete.
 *   steps     an inline chain built from the op catalogue (to_upper,
 *             null_if, regex_replace, …), column params pre-filled.
 *   library   assign an existing chain (user / shipped / private), with
 *             where-used.
 *
 * Then where to keep it (inline in assignments.yaml / a private chain /
 * the user library) and the scope (this table / every table). One write:
 * POST /pipeline-packages/{p}/udfs:apply (validated server-side; nothing
 * is written on error). Fires ``ovid2:udf-applied`` on success.
 */

import { api } from "/static/spec_builder.js";

const esc = (s) => String(s == null ? "" : s).replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;
const tip = (t) => (window.Ovid2HelpTip ? window.Ovid2HelpTip.html(t) : "");
const NAME_RE = /^[a-z][a-z0-9_]*$/;

/* Simple, frequently wanted single-column ops for the "steps" mode. */
const QUICK_OPS = ["trim", "to_upper", "to_lower", "to_title", "normalize_whitespace", "standardize_null",
  "strip_non_numeric", "strip_non_alpha", "regex_replace", "null_if", "default_value", "pad", "clamp", "cast"];

let fnCache = null, opCache = null, libCache = null;

function columnParams(op, field) {
  // Pre-fill the op's column parameter(s) from the field.
  const props = (op.params_schema && op.params_schema.properties) || {};
  const out = {};
  if (props.columns) out.columns = field ? [field] : [];
  else if (props.column) out.column = field || "";
  if (op.name === "cast") out.types = field ? { [field]: "string" } : {};
  for (const req of op.params_schema.required || []) {
    if (out[req] !== undefined) continue;
    const p = props[req] || {};
    out[req] = p.type === "array" ? [] : p.type === "integer" || p.type === "number" ? 0 : p.type === "object" ? {} : "";
  }
  return out;
}

function paramInput(key, prop, value, columns, onChange) {
  const wrap = document.createElement("label");
  wrap.className = "pb-param";
  wrap.innerHTML = "<span>" + esc(key) + "</span>";
  let input;
  const isCol = /^(column|columns|target|target_column)$/.test(key);
  if (isCol && columns.length) {
    // U2: a column must be one of the table's columns → select.
    input = document.createElement("select");
    if (prop.type === "array") input.multiple = true;
    input.innerHTML = columns.map((c) => '<option value="' + esc(c) + '"' +
      ((Array.isArray(value) ? value.includes(c) : value === c) ? " selected" : "") + ">" + esc(c) + "</option>").join("");
    input.addEventListener("change", () => onChange(prop.type === "array" ? Array.from(input.selectedOptions).map((o) => o.value) : input.value));
  } else if (prop.enum) {
    input = document.createElement("select");
    input.innerHTML = prop.enum.map((v) => "<option" + (v === value ? " selected" : "") + ">" + esc(v) + "</option>").join("");
    input.addEventListener("change", () => onChange(input.value));
  } else if (prop.type === "boolean") {
    input = document.createElement("select");
    input.innerHTML = '<option value="true"' + (value ? " selected" : "") + '>true</option><option value="false"' + (!value ? " selected" : "") + ">false</option>";
    input.addEventListener("change", () => onChange(input.value === "true"));
  } else {
    input = document.createElement("input");
    input.type = prop.type === "integer" || prop.type === "number" ? "number" : "text";
    input.value = typeof value === "object" && value !== null ? JSON.stringify(value) : value == null ? "" : String(value);
    input.addEventListener("input", () => {
      let v = input.value;
      if (prop.type === "integer" || prop.type === "number") v = v === "" ? undefined : Number(v);
      else if (prop.type === "array" || prop.type === "object") { try { v = JSON.parse(v); } catch (_e) { /* keep text */ } }
      onChange(v);
    });
  }
  wrap.appendChild(input);
  if (prop.description) wrap.insertAdjacentHTML("beforeend", tip(prop.description));
  return wrap;
}

export async function openUdfDialog({ pipeline, table, field, lineage }) {
  const columns = (lineage.fields || []).filter((f) => f.status !== "extra").map((f) => f.name);
  [fnCache, opCache, libCache] = await Promise.all([
    fnCache || api("GET", "/expressions/functions"),
    opCache || api("GET", "/transforms"),
    api("GET", "/transform-library"),
  ]);
  let priv = [];
  try { priv = await api("GET", "/pipeline-packages/" + enc(pipeline) + "/transforms"); } catch (_e) { priv = []; }
  const chains = [...priv.map((c) => ({ ...c, scope: "pipeline" })), ...libCache];
  // Column-level ops only (row filters / reshapes belong in the library editor).
  const ops = opCache.filter((o) => QUICK_OPS.includes(o.name));

  const dlg = document.createElement("dialog");
  dlg.className = "nv-udf-dialog";
  dlg.innerHTML =
    '<form method="dialog" class="nv-udf-form">' +
    '<header class="nv-udf-head"><h2>Apply a UDF ' + tip("A UDF is a declarative transform chain run in the transform stage (after deduplicate, before map). Computed columns use a Spark-SQL expression checked against an allow-list of built-in functions; Python UDFs are not supported.") + "</h2>" +
    '<span class="mono small">' + esc(pipeline) + " · " + esc(table) + (field ? " · " + esc(field) : "") + "</span></header>" +
    '<fieldset class="nv-udf-modes" role="radiogroup" aria-label="Kind of UDF">' +
    '<label><input type="radio" name="mode" value="computed" checked> Computed column</label>' +
    '<label><input type="radio" name="mode" value="steps"> Chain of functions</label>' +
    '<label><input type="radio" name="mode" value="library"> Existing chain</label></fieldset>' +

    '<section data-mode="computed">' +
    '<label class="pb-param"><span>column</span><select data-col>' +
    columns.map((c) => "<option" + (c === field ? " selected" : "") + ">" + esc(c) + "</option>").join("") + "</select>" +
    tip("The column to write. It is replaced when it already exists.") + "</label>" +
    '<label class="pb-param is-wide"><span>expression</span><textarea class="mono" rows="3" spellcheck="false" data-expr aria-autocomplete="list">' +
    esc(field ? "coalesce(nullif(trim(" + field + "), ''), " + field + ")" : "") + "</textarea>" +
    tip("Spark SQL, one row at a time — e.g. coalesce(nullif(upper(trim(SEX)), ''), 'UN'). No sub-queries, aggregates or unknown functions.") + "</label>" +
    '<div class="nv-udf-ac small" data-ac></div>' +
    '<label class="pb-param"><span>cast</span><select data-cast><option value="">(none)</option>' +
    ["string", "int", "bigint", "double", "date", "timestamp", "boolean"].map((t) => "<option>" + t + "</option>").join("") + "</select></label>" +
    '<p class="nv-udf-check small" data-check aria-live="polite"></p></section>' +

    '<section data-mode="steps" hidden><ol class="pb-steps" data-steps></ol>' +
    '<div class="pb-add-row"><select data-add-op aria-label="Function to add">' +
    ops.map((o) => '<option value="' + esc(o.name) + '">' + esc(o.name) + " — " + esc(o.summary) + "</option>").join("") +
    '</select><button type="button" class="button small" data-add>+ add</button></div></section>' +

    '<section data-mode="library" hidden><label class="pb-param is-wide"><span>chain</span><select data-chain>' +
    chains.map((c) => '<option value="' + esc(c.name) + '">' + esc(c.name) + " (" + esc(c.scope) + ", " + c.steps + " steps)</option>").join("") +
    '</select></label><p class="small" data-chain-desc></p><div class="small" data-usage></div></section>' +

    '<fieldset class="nv-udf-where" data-where><legend class="small">Keep it</legend>' +
    '<label class="pb-param"><span>save as</span><select data-save>' +
    '<option value="inline">inline in this pipeline\'s assignments</option>' +
    '<option value="pipeline">a named chain private to this pipeline</option>' +
    '<option value="library">a named chain in my transform library</option></select>' +
    tip("Library chains can be reused by any pipeline; private chains override library chains of the same name for this pipeline.") + "</label>" +
    '<label class="pb-param" data-name-row hidden><span>name</span><input type="text" data-name pattern="[a-z][a-z0-9_]*" placeholder="normalise_sex"></label>' +
    '<label class="pb-param" data-name-row hidden><span>description</span><input type="text" data-desc></label>' +
    "</fieldset>" +
    '<label class="pb-param"><span>apply to</span><select data-scope>' +
    '<option value="table">' + esc(table) + " only</option>" +
    '<option value="all_tables">every table (*)</option></select></label>' +
    '<ul class="pb-errors" data-errors hidden></ul>' +
    '<footer class="nv-udf-actions"><span class="pb-status small" data-status></span>' +
    '<button type="button" class="button" value="cancel" data-cancel>Cancel</button>' +
    '<button type="button" class="button primary" data-apply>Apply</button></footer></form>';
  document.body.appendChild(dlg);
  const $ = (s) => dlg.querySelector(s);
  const $$ = (s) => Array.from(dlg.querySelectorAll(s));

  // --- mode switching (U1: only the chosen mode's fields are shown)
  const mode = () => $('input[name="mode"]:checked').value;
  const syncMode = () => {
    for (const s of $$("section[data-mode]")) s.hidden = s.dataset.mode !== mode();
    $("[data-where]").hidden = mode() === "library";
    syncSave();
  };
  const syncSave = () => {
    const named = mode() !== "library" && $("[data-save]").value !== "inline";
    $$("[data-name-row]").forEach((r) => { r.hidden = !named; });
  };
  $$('input[name="mode"]').forEach((r) => r.addEventListener("change", syncMode));
  $("[data-save]").addEventListener("change", syncSave);

  // --- computed: live check + autocomplete
  const exprEl = $("[data-expr]");
  let checkT = 0, lastOk = false;
  const runCheck = async () => {
    const out = $("[data-check]");
    const text = exprEl.value.trim();
    if (!text) { out.textContent = ""; lastOk = false; return; }
    try {
      const r = await api("POST", "/expressions:check", { expr: text, pipeline, table });
      lastOk = r.ok;
      out.className = "nv-udf-check small " + (r.ok ? (r.unknown_columns.length ? "is-warn" : "is-ok") : "is-err");
      out.textContent = r.ok
        ? "✓ valid" + (r.columns.length ? " · reads " + r.columns.join(", ") : "") +
          (r.unknown_columns.length ? " · not columns of " + table + ": " + r.unknown_columns.join(", ") : "")
        : "✗ " + r.errors.join("; ");
    } catch (err) { out.className = "nv-udf-check small is-err"; out.textContent = err.message; lastOk = false; }
  };
  const suggest = () => {
    const pos = exprEl.selectionStart;
    const word = (/[A-Za-z_][A-Za-z0-9_]*$/.exec(exprEl.value.slice(0, pos)) || [""])[0];
    const box = $("[data-ac]");
    if (word.length < 2) { box.replaceChildren(); return; }
    const lw = word.toLowerCase();
    const hits = [
      ...columns.filter((c) => c.toLowerCase().startsWith(lw)).map((c) => ({ t: c, k: "col" })),
      ...fnCache.filter((f) => f.startsWith(lw)).map((f) => ({ t: f + "(", k: "fn" })),
    ].slice(0, 12);
    box.innerHTML = hits.map((h) => '<button type="button" class="nv-ac is-' + h.k + '" data-ins="' + esc(h.t) + '">' + esc(h.t) + "</button>").join("");
    box.querySelectorAll("[data-ins]").forEach((b) => b.addEventListener("click", () => {
      const before = exprEl.value.slice(0, pos - word.length), after = exprEl.value.slice(pos);
      exprEl.value = before + b.dataset.ins + after;
      const at = before.length + b.dataset.ins.length;
      exprEl.focus(); exprEl.setSelectionRange(at, at);
      box.replaceChildren();
      window.clearTimeout(checkT); checkT = window.setTimeout(runCheck, 250);
    }));
  };
  exprEl.addEventListener("input", () => { suggest(); window.clearTimeout(checkT); checkT = window.setTimeout(runCheck, 400); });
  runCheck();

  // --- steps
  const steps = [];
  const drawSteps = () => {
    const list = $("[data-steps]");
    list.replaceChildren();
    steps.forEach((step, i) => {
      const op = opCache.find((o) => o.name === step.op);
      const li = document.createElement("li");
      li.className = "pb-step";
      li.innerHTML = '<div class="pb-step-head"><span class="pb-step-summary mono">' + esc(step.op) + '</span><span class="pb-step-tools">' +
        '<button type="button" class="icon-button" data-up title="Move up">↑</button><button type="button" class="icon-button" data-rm title="Remove">✕</button></span></div>';
      const body = document.createElement("div");
      body.className = "pb-form";
      const props = (op && op.params_schema.properties) || {};
      for (const [k, prop] of Object.entries(props)) {
        body.appendChild(paramInput(k, prop, step[k], columns, (v) => { if (v === undefined || v === "") delete step[k]; else step[k] = v; }));
      }
      li.appendChild(body);
      li.querySelector("[data-rm]").addEventListener("click", () => { steps.splice(i, 1); drawSteps(); });
      li.querySelector("[data-up]").addEventListener("click", () => { if (i) { [steps[i - 1], steps[i]] = [steps[i], steps[i - 1]]; drawSteps(); } });
      list.appendChild(li);
    });
    if (!steps.length) list.innerHTML = '<li class="empty small">No steps yet.</li>';
  };
  $("[data-add]").addEventListener("click", () => {
    const op = opCache.find((o) => o.name === $("[data-add-op]").value);
    if (op) { steps.push({ op: op.name, ...columnParams(op, field) }); drawSteps(); }
  });
  drawSteps();

  // --- library: description + where used
  const showChain = async () => {
    const c = chains.find((x) => x.name === $("[data-chain]").value);
    $("[data-chain-desc]").textContent = c ? [c.description, (c.ops || []).join(" → ")].filter(Boolean).join(" — ") : "";
    const u = $("[data-usage]");
    if (!c) { u.textContent = ""; return; }
    try {
      const rows = await api("GET", "/transform-library/" + enc(c.name) + "/usage");
      u.innerHTML = rows.length ? "Used by: " + rows.map((r) => '<a href="/navigator?p=' + enc(r.pipeline) + (r.table !== "*" ? "&t=" + enc(r.table) : "") + '">' +
        esc(r.pipeline) + "·" + esc(r.table) + "</a>" + (r.fields.length ? " (" + r.fields.map(esc).join(", ") + ")" : "")).join(", ") : "Not assigned anywhere yet.";
    } catch (_e) { u.textContent = ""; }
  };
  $("[data-chain]").addEventListener("change", showChain);
  if (chains.length) showChain();

  // --- apply
  const errors = (list) => {
    const ul = $("[data-errors]");
    ul.hidden = !list.length;
    ul.innerHTML = list.map((e) => "<li>" + esc(e) + "</li>").join("");
  };
  $("[data-cancel]").addEventListener("click", () => { dlg.close(); dlg.remove(); });
  dlg.addEventListener("cancel", () => dlg.remove());
  $("[data-apply]").addEventListener("click", async () => {
    errors([]);
    const body = { table, scope: $("[data-scope]").value };
    const m = mode();
    if (m === "library") {
      if (!$("[data-chain]").value) { errors(["Pick a chain."]); return; }
      body.chain = $("[data-chain]").value;
      body.save_to = "inline";
    } else {
      body.save_to = $("[data-save]").value;
      if (body.save_to !== "inline") {
        body.name = $("[data-name]").value.trim();
        body.description = $("[data-desc]").value.trim();
        if (!NAME_RE.test(body.name || "")) { errors(["Name: lower-case letters, digits and _ (start with a letter)."]); return; }
      }
      if (m === "computed") {
        if (!lastOk) { await runCheck(); if (!lastOk) { errors(["Fix the expression first."]); return; } }
        const step = { op: "derive_column", column: $("[data-col]").value, expr: exprEl.value.trim() };
        if ($("[data-cast]").value) step.cast = $("[data-cast]").value;
        body.steps = [step];
      } else {
        if (!steps.length) { errors(["Add at least one step."]); return; }
        body.steps = steps;
      }
    }
    const btn = $("[data-apply]");
    btn.disabled = true;
    $("[data-status]").textContent = "Applying…";
    try {
      await api("POST", "/pipeline-packages/" + enc(pipeline) + "/udfs:apply", body);
      dlg.close(); dlg.remove();
      document.dispatchEvent(new CustomEvent("ovid2:udf-applied", { detail: { pipeline, table, field: m === "computed" ? body.steps[0].column : field } }));
    } catch (err) {
      $("[data-status]").textContent = "Not applied";
      errors(String(err.message).split("; "));
      btn.disabled = false;
    }
  });

  syncMode();
  dlg.showModal();
  (field ? exprEl : $("[data-col]")).focus();
}
