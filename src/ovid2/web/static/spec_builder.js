/*
 * Ovid2 spec builder — the pipeline builder (/pipeline-packages/{p}/builder)
 * and the transform library (/transforms).
 *
 * Every document has two synchronised views:
 *
 *   Form — generated from the document's schema / catalogue (transform op
 *          params_schema, mapper-op catalogue, fixer catalogue, settings
 *          constants). Editing the form re-serialises the YAML.
 *   YAML — a CodeMirror editor (yaml_editor.js) live-linted through
 *          POST /api/v1/pipeline-packages/{p}/builder:validate.
 *          Editing the YAML re-renders the form when it parses.
 *
 * Saving goes through the matching PUT / PATCH endpoint; the server
 * re-validates before writing (schema, op params, SQL expression
 * allow-list, fixer registry, CDM tables). Comments in the YAML are kept
 * when you edit in the YAML tab; saving from the Form tab re-serialises
 * (the UI warns when that would drop comments).
 *
 * Documents (``doc.type``):
 *   transform    library / private chain     steps of library ops
 *   assignments  transforms/assignments.yaml table → [chain | inline steps]
 *   fixers       fixer_configs.yaml          table → [fix entries]
 *   formatter    formatters/<t>.yaml         adapter, sources, projections …
 *   mapper       mappers/<t>.yaml            ops
 *   settings     pipeline_settings.py        literal constants (PATCH)
 */

import { FlowGraph } from "/static/flow_graph.js";

const root = document.getElementById("spec-builder");
const MODE = root ? root.dataset.mode || "pipeline" : "";
const PIPELINE = root ? root.dataset.pipeline || "" : "";
const CAN_WRITE = root ? root.dataset.canWrite === "true" : false;
const API = "/api/v1";

/* -------------------------------------------------------------- helpers */

const esc = (s) => String(s == null ? "" : s).replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[c]);
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const enc = encodeURIComponent;

async function api(method, path, body) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(API + path, opts);
  let data = null;
  try { data = await r.json(); } catch (_e) { data = null; }
  if (!r.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.status === "awaiting_approval"
      ? "Awaiting human approval (approval " + d.approval_id + ") — approve it on the Approvals page, then retry."
      : JSON.stringify(d || data || r.statusText);
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return data;
}

function debounce(fn, ms) {
  let t = 0;
  return (...a) => { window.clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
}

let yamlLib = null;
async function yamlMod() {
  if (!yamlLib) yamlLib = await import("/static/yaml_editor.js");
  return yamlLib;
}

function parseScalar(text, schema) {
  const t = String(text);
  if (schema && (schema.type === "integer" || schema.type === "number")) {
    if (t.trim() === "") return undefined;
    const n = Number(t);
    return Number.isFinite(n) ? n : t;
  }
  if (schema && schema.type === "boolean") return t === "true";
  if (!schema || !schema.type) {
    if (t === "") return undefined;
    if (/^-?\d+(\.\d+)?$/.test(t)) return Number(t);
    if (t === "true" || t === "false") return t === "true";
    if (t === "null") return null;
  }
  return t;
}

/* ----------------------------------------------------- catalogues (cached) */

const cache = {};
async function catalogue(name) {
  if (cache[name]) return cache[name];
  const path = { transforms: "/transforms", mapper_ops: "/mapper-ops", fixers: "/fixers",
    library: "/transform-library" }[name];
  cache[name] = await api("GET", path);
  return cache[name];
}

/* ---------------------------------------------------- parameter widgets */

/*
 * Render one JSON-schema object's properties as form fields. ``onChange``
 * receives the full new params object. Returns a DOM node.
 */
function paramsForm(schema, params, onChange, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = "pb-params";
  const props = (schema && schema.properties) || {};
  const required = new Set((schema && schema.required) || []);
  const value = { ...(params || {}) };
  const emit = () => onChange(Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined)));
  const keys = Object.keys(props);
  // Unknown keys (permissive schemas, e.g. legacy_select) are edited as YAML.
  const extra = Object.keys(value).filter((k) => !props[k]);
  for (const key of keys) {
    const p = props[key] || {};
    const label = document.createElement("label");
    label.className = "pb-param" + (p.type === "array" || p.type === "object" || key === "expr" ? " is-wide" : "");
    const title = document.createElement("span");
    title.innerHTML = esc(key) + (required.has(key) ? " <em>*</em>" : "");
    label.appendChild(title);
    let input;
    const cur = value[key];
    if (p.enum) {
      input = document.createElement("select");
      input.innerHTML = (required.has(key) ? "" : '<option value="">(default' + (p.default != null ? ": " + esc(p.default) : "") + ")</option>") +
        p.enum.map((e) => '<option value="' + esc(e) + '"' + (cur === e ? " selected" : "") + ">" + esc(e) + "</option>").join("");
      input.addEventListener("change", () => { value[key] = input.value === "" ? undefined : parseScalar(input.value, p); emit(); });
    } else if (p.type === "boolean") {
      input = document.createElement("select");
      input.innerHTML = '<option value="">(default' + (p.default != null ? ": " + p.default : "") + ')</option><option value="true"' +
        (cur === true ? " selected" : "") + '>true</option><option value="false"' + (cur === false ? " selected" : "") + ">false</option>";
      input.addEventListener("change", () => { value[key] = input.value === "" ? undefined : input.value === "true"; emit(); });
    } else if (p.type === "array" && (!p.items || p.items.type === "string" || p.items.type === "integer")) {
      input = document.createElement("input");
      input.type = "text";
      input.value = Array.isArray(cur) ? cur.join(", ") : cur == null ? "" : String(cur);
      input.placeholder = "comma-separated";
      input.addEventListener("input", () => {
        const parts = input.value.split(",").map((x) => x.trim()).filter(Boolean);
        value[key] = parts.length ? parts.map((x) => (p.items && p.items.type === "integer" ? Number(x) : x)) : undefined;
        emit();
      });
    } else if (p.type === "array" || p.type === "object" || (!p.type && cur && typeof cur === "object")) {
      input = document.createElement("textarea");
      input.rows = 3;
      input.className = "mono";
      input.spellcheck = false;
      yamlMod().then((m) => { input.value = cur == null ? "" : m.yamlStringify(cur).trimEnd(); });
      input.addEventListener("input", debounce(async () => {
        const m = await yamlMod();
        try {
          value[key] = input.value.trim() ? m.yamlParse(input.value) : undefined;
          label.classList.remove("is-invalid");
          emit();
        } catch (_e) { label.classList.add("is-invalid"); }
      }, 300));
    } else {
      input = document.createElement(key === "expr" ? "textarea" : "input");
      if (key === "expr") { input.rows = 2; input.className = "mono"; input.spellcheck = false; }
      else input.type = p.type === "integer" || p.type === "number" ? "number" : "text";
      input.value = cur == null ? "" : String(cur);
      if (p.default != null) input.placeholder = "default: " + p.default;
      input.addEventListener("input", () => { value[key] = parseScalar(input.value, p); emit(); });
    }
    input.setAttribute("aria-label", key);
    label.appendChild(input);
    if (p.description) {
      const d = document.createElement("small");
      d.textContent = p.description;
      label.appendChild(d);
    }
    if (key === "expr" && opts.exprCheck) {
      const msg = document.createElement("small");
      msg.className = "pb-expr-msg";
      label.appendChild(msg);
      const check = debounce(() => opts.exprCheck(input.value, msg, label), 400);
      input.addEventListener("input", check);
      if (input.value) check();
    }
    wrap.appendChild(label);
  }
  if (extra.length) {
    const label = document.createElement("label");
    label.className = "pb-param is-wide";
    label.innerHTML = "<span>other keys</span>";
    const ta = document.createElement("textarea");
    ta.rows = 4; ta.className = "mono"; ta.spellcheck = false;
    yamlMod().then((m) => { ta.value = m.yamlStringify(Object.fromEntries(extra.map((k) => [k, value[k]]))).trimEnd(); });
    ta.addEventListener("input", debounce(async () => {
      const m = await yamlMod();
      try {
        const obj = ta.value.trim() ? m.yamlParse(ta.value) : {};
        for (const k of extra) delete value[k];
        Object.assign(value, obj || {});
        label.classList.remove("is-invalid");
        emit();
      } catch (_e) { label.classList.add("is-invalid"); }
    }, 300));
    label.appendChild(ta);
    wrap.appendChild(label);
  }
  if (!keys.length && !extra.length) {
    const n = document.createElement("p");
    n.className = "nw-muted small";
    n.textContent = "No parameters.";
    wrap.appendChild(n);
  }
  return wrap;
}

/* Expression check through the validator (derive_column / filter_expr). */
async function checkExpr(op, column, text, msg, label) {
  if (!text.trim()) { msg.textContent = ""; return; }
  const doc = "v: 1\nsteps:\n  - op: " + op + "\n" + (op === "derive_column" ? "    column: " + JSON.stringify(column || "X") + "\n" : "") +
    "    expr: " + JSON.stringify(text) + "\n";
  try {
    const r = await api("POST", "/pipeline-packages/" + enc(PIPELINE || "_") + "/builder:validate", { doc_type: "transform", raw_text: doc });
    const errs = (r.errors || []).filter((e) => /expr/.test(e.path || e.message));
    msg.classList.toggle("is-ok", !errs.length);
    label.classList.toggle("is-invalid", errs.length > 0);
    msg.textContent = errs.length ? errs.map((e) => e.message.replace(/^.*?: /, "")).join("; ") : "✓ allowed row expression";
  } catch (_e) { msg.textContent = ""; }
}

/* ------------------------------------------------------ step list editor */

/*
 * A list of ``{op, ...params}`` steps with add / reorder / remove. ``ops``
 * is the catalogue ``[{name, summary, params_schema, category?}]``.
 */
function stepsEditor(steps, ops, onChange, opts = {}) {
  const box = document.createElement("div");
  box.className = "pb-form";
  const list = document.createElement("ol");
  list.className = "pb-steps";
  box.appendChild(list);
  const byName = Object.fromEntries(ops.map((o) => [o.name, o]));
  const state = (steps || []).map((s) => ({ ...s }));
  const emit = () => onChange(state.map((s) => ({ ...s })));
  const draw = () => {
    list.replaceChildren();
    state.forEach((step, i) => {
      const li = document.createElement("li");
      li.className = "pb-step";
      const head = document.createElement("div");
      head.className = "pb-step-head";
      const sel = document.createElement("select");
      sel.setAttribute("aria-label", "op");
      const cats = {};
      for (const o of ops) (cats[o.category || "ops"] = cats[o.category || "ops"] || []).push(o);
      sel.innerHTML = Object.entries(cats).map(([c, os]) => '<optgroup label="' + esc(c) + '">' +
        os.map((o) => '<option value="' + esc(o.name) + '"' + (o.name === step.op ? " selected" : "") + ">" + esc(o.name) + "</option>").join("") + "</optgroup>").join("") +
        (byName[step.op] ? "" : '<option value="' + esc(step.op) + '" selected>' + esc(step.op) + " (unknown)</option>");
      sel.addEventListener("change", () => { state[i] = { op: sel.value }; emit(); draw(); });
      head.appendChild(sel);
      const sum = document.createElement("span");
      sum.className = "pb-step-summary";
      const meta = byName[step.op];
      sum.textContent = meta ? meta.summary + (meta.requires_family ? " (" + meta.requires_family + " only)" : "") : "";
      head.appendChild(sum);
      const tools = document.createElement("span");
      tools.className = "pb-step-tools";
      const btn = (label, title, fn, disabled) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "icon-button"; b.textContent = label; b.title = title;
        b.disabled = !!disabled;
        b.addEventListener("click", fn);
        tools.appendChild(b);
      };
      btn("↑", "Move up", () => { [state[i - 1], state[i]] = [state[i], state[i - 1]]; emit(); draw(); }, i === 0);
      btn("↓", "Move down", () => { [state[i + 1], state[i]] = [state[i], state[i + 1]]; emit(); draw(); }, i === state.length - 1);
      btn("✕", "Remove step", () => { state.splice(i, 1); emit(); draw(); });
      head.appendChild(tools);
      li.appendChild(head);
      const params = Object.fromEntries(Object.entries(step).filter(([k]) => k !== "op"));
      li.appendChild(paramsForm(meta ? meta.params_schema : { type: "object", additionalProperties: true }, params, (p) => {
        state[i] = { op: step.op, ...p };
        emit();
      }, {
        exprCheck: (step.op === "derive_column" || step.op === "filter_expr")
          ? (text, msg, label) => checkExpr(step.op, state[i].column, text, msg, label) : null,
      }));
      list.appendChild(li);
    });
    if (!state.length) {
      const li = document.createElement("li");
      li.className = "nw-muted small";
      li.textContent = opts.emptyText || "No steps yet — add one below.";
      list.appendChild(li);
    }
  };
  const add = document.createElement("div");
  add.className = "pb-add-row";
  const addSel = document.createElement("select");
  addSel.setAttribute("aria-label", "op to add");
  addSel.innerHTML = '<option value="">+ add a step…</option>' + ops.map((o) => '<option value="' + esc(o.name) + '">' + esc(o.name) + " — " + esc(o.summary) + "</option>").join("");
  addSel.addEventListener("change", () => {
    if (!addSel.value) return;
    state.push({ op: addSel.value });
    addSel.value = "";
    emit(); draw();
  });
  add.appendChild(addSel);
  box.appendChild(add);
  draw();
  return box;
}

/* -------------------------------------------------- document definitions */

/*
 * Each doc type: load(ref) → {text, path, readonly?, deletable?},
 * save(ref, text, data), del?(ref), form(data, onChange, ref) → Node,
 * validateType for the live lint, blank(ref) → initial data.
 */
const DOCS = {
  transform: {
    icon: "zap",
    async load(ref) {
      const d = ref.scope === "pipeline"
        ? await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/transforms/" + enc(ref.name))
        : await api("GET", "/transform-library/" + enc(ref.name));
      return { text: d.raw_text, path: d.path, readonly: d.scope === "shipped", deletable: d.scope !== "shipped", scope: d.scope };
    },
    async save(ref, text) {
      return ref.scope === "pipeline"
        ? api("PUT", "/pipeline-packages/" + enc(PIPELINE) + "/transforms/" + enc(ref.name), { raw_text: text })
        : api("PUT", "/transform-library/" + enc(ref.name), { raw_text: text });
    },
    async del(ref) {
      return ref.scope === "pipeline"
        ? api("DELETE", "/pipeline-packages/" + enc(PIPELINE) + "/transforms/" + enc(ref.name))
        : api("DELETE", "/transform-library/" + enc(ref.name));
    },
    blank() { return { v: 1, description: "", steps: [] }; },
    async form(data, onChange) {
      const ops = await catalogue("transforms");
      const box = document.createElement("div");
      box.className = "pb-form";
      const desc = document.createElement("label");
      desc.className = "pb-param is-wide";
      desc.innerHTML = "<span>description</span>";
      const di = document.createElement("input");
      di.type = "text"; di.value = data.description || "";
      di.addEventListener("input", () => { data.description = di.value || undefined; onChange(data); });
      desc.appendChild(di);
      box.appendChild(desc);
      const en = document.createElement("label");
      en.className = "wz-switch";
      en.innerHTML = '<input type="checkbox"' + (data.enabled === false ? "" : " checked") + "> <span>enabled</span>";
      en.querySelector("input").addEventListener("change", (e) => { data.enabled = e.target.checked ? undefined : false; onChange(data); });
      box.appendChild(en);
      box.appendChild(stepsEditor(data.steps || [], ops, (steps) => { data.steps = steps; onChange(data); }));
      return box;
    },
    validateType: "transform",
  },

  assignments: {
    icon: "zap",
    async load() {
      const d = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/transform-assignments");
      return { text: d.raw_text, path: d.path };
    },
    save(_ref, text) { return api("PUT", "/pipeline-packages/" + enc(PIPELINE) + "/transform-assignments", { raw_text: text }); },
    blank() { return { v: 1, tables: {} }; },
    async form(data, onChange, ref) {
      const [lib, priv, ops] = await Promise.all([
        catalogue("library"),
        api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/transforms"),
        catalogue("transforms"),
      ]);
      const chains = [...priv.map((c) => ({ ...c, scope: "pipeline" })), ...lib];
      data.tables = data.tables || {};
      const box = document.createElement("div");
      box.className = "pb-form";
      const tables = ref && ref.table ? [ref.table] : Object.keys(data.tables);
      const drawTable = (t) => {
        const sec = document.createElement("fieldset");
        sec.className = "nw-id-group";
        sec.innerHTML = "<legend>" + esc(t === "*" ? "* (every table)" : t) + "</legend>";
        const entries = data.tables[t] || [];
        const list = document.createElement("ol");
        list.className = "pb-steps";
        entries.forEach((e, i) => {
          const li = document.createElement("li");
          li.className = "pb-step";
          const head = document.createElement("div");
          head.className = "pb-step-head";
          if (e.spec !== undefined) {
            const sel = document.createElement("select");
            sel.innerHTML = chains.map((c) => '<option value="' + esc(c.name) + '"' + (c.name === e.spec ? " selected" : "") + ">" +
              esc(c.name) + " (" + esc(c.scope) + ")</option>").join("") + (chains.some((c) => c.name === e.spec) ? "" : '<option selected value="' + esc(e.spec) + '">' + esc(e.spec) + " (missing)</option>");
            sel.addEventListener("change", () => { entries[i] = { spec: sel.value }; onChange(data); });
            head.appendChild(sel);
            const link = document.createElement("a");
            link.href = "/transforms?name=" + enc(e.spec);
            link.className = "small";
            link.textContent = "open";
            head.appendChild(link);
          } else {
            head.innerHTML = '<span class="pb-step-summary">inline chain</span>';
          }
          const tools = document.createElement("span");
          tools.className = "pb-step-tools";
          tools.innerHTML = '<button type="button" class="icon-button" title="Remove">✕</button>';
          tools.querySelector("button").addEventListener("click", () => {
            entries.splice(i, 1);
            if (!entries.length) delete data.tables[t]; else data.tables[t] = entries;
            onChange(data); redraw();
          });
          head.appendChild(tools);
          li.appendChild(head);
          if (e.steps !== undefined) {
            li.appendChild(stepsEditor(e.steps, ops, (steps) => { entries[i] = { steps }; onChange(data); }));
          }
          list.appendChild(li);
        });
        sec.appendChild(list);
        const add = document.createElement("div");
        add.className = "pb-add-row";
        add.innerHTML = '<select aria-label="chain"><option value="">+ assign a chain…</option>' +
          chains.map((c) => '<option value="' + esc(c.name) + '">' + esc(c.name) + " (" + esc(c.scope) + ")</option>").join("") +
          '<option value="__inline__">+ inline steps</option></select>';
        add.querySelector("select").addEventListener("change", (ev) => {
          const v = ev.target.value;
          if (!v) return;
          data.tables[t] = [...(data.tables[t] || []), v === "__inline__" ? { steps: [] } : { spec: v }];
          onChange(data); redraw();
        });
        sec.appendChild(add);
        return sec;
      };
      const redraw = () => {
        box.replaceChildren();
        const keys = ref && ref.table ? [ref.table] : Object.keys(data.tables);
        if (!keys.length) {
          const p = document.createElement("p");
          p.className = "note";
          p.textContent = "No transforms assigned yet. Pick a table on the left, or add a table below.";
          box.appendChild(p);
        }
        for (const t of keys) box.appendChild(drawTable(t));
        if (!(ref && ref.table)) {
          const add = document.createElement("div");
          add.className = "pb-add-row";
          const opts = ['<option value="">+ add a table…</option>', '<option value="*">* (every table)</option>',
            ...(window.__pbTables || []).filter((x) => !data.tables[x]).map((x) => '<option value="' + esc(x) + '">' + esc(x) + "</option>")];
          add.innerHTML = '<select aria-label="table">' + opts.join("") + "</select>";
          add.querySelector("select").addEventListener("change", (ev) => {
            if (!ev.target.value) return;
            data.tables[ev.target.value] = data.tables[ev.target.value] || [];
            redraw();
          });
          box.appendChild(add);
        }
      };
      void tables;
      redraw();
      return box;
    },
    validateType: "assignments",
  },

  fixers: {
    icon: "shield-check",
    async load() {
      const d = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/fixers");
      const legacy = root.dataset.profile === "legacy_ovid";
      return {
        text: d.raw_text, path: d.path, readonly: legacy,
        note: legacy ? "legacy_ovid ports run their vendored legacy fixer scripts; native fix chains are disabled."
          : d.source === "python" ? "Converted from fixer_configs.py — saving writes fixer_configs.yaml, which then takes precedence." : d.error,
      };
    },
    save(_ref, text) { return api("PUT", "/pipeline-packages/" + enc(PIPELINE) + "/fixers", { raw_text: text }); },
    blank() { return { v: 1, fixer_chains: {}, multi_table_fixes: [] }; },
    async form(data, onChange, ref) {
      const cdm = root.dataset.cdm || "";
      const fixers = await api("GET", "/fixers" + (cdm ? "?cdm=" + enc(cdm) : ""));
      const ops = fixers.map((f) => {
        const props = {};
        const pos = Object.entries(f.aliases || {});
        for (const [alias, slot] of pos) props[alias] = { type: "string", description: slot + " — " + alias.replace(/_/g, " ") };
        if (!pos.length) { props.src1 = { type: "string" }; props.src2 = { type: "string" }; }
        props.version = { type: "string" };
        return { name: f.name, summary: f.summary, category: f.scope, params_schema: { type: "object", properties: props } };
      });
      // Store fix entries as {op: fix} for the step editor, convert back.
      data.fixer_chains = data.fixer_chains || {};
      const toSteps = (entries) => (entries || []).map((e) => {
        const { fix, ...rest } = e;
        const meta = fixers.find((f) => f.name === fix);
        const out = { op: fix };
        const back = meta ? Object.fromEntries(Object.entries(meta.aliases || {}).map(([a, s]) => [s, a])) : {};
        for (const [k, v] of Object.entries(rest)) out[back[k] || k] = v;
        return out;
      });
      const fromSteps = (steps) => steps.map(({ op, ...rest }) => ({ fix: op, ...rest }));
      const box = document.createElement("div");
      box.className = "pb-form";
      const keys = ref && ref.table ? [ref.table] : Object.keys(data.fixer_chains);
      for (const t of keys) {
        const sec = document.createElement("fieldset");
        sec.className = "nw-id-group";
        sec.innerHTML = "<legend>" + esc(t) + "</legend>";
        sec.appendChild(stepsEditor(toSteps(data.fixer_chains[t]), ops, (steps) => {
          if (steps.length) data.fixer_chains[t] = fromSteps(steps); else delete data.fixer_chains[t];
          onChange(data);
        }, { emptyText: "No fixes for this table." }));
        box.appendChild(sec);
      }
      if (!(ref && ref.table)) {
        const sec = document.createElement("fieldset");
        sec.className = "nw-id-group";
        sec.innerHTML = "<legend>multi-table fixes (after every table)</legend>";
        sec.appendChild(stepsEditor(toSteps(data.multi_table_fixes), ops, (steps) => {
          data.multi_table_fixes = fromSteps(steps); onChange(data);
        }, { emptyText: "None." }));
        box.appendChild(sec);
      }
      return box;
    },
    validateType: "fixers",
  },

  mapper: {
    icon: "layers",
    async load(ref) {
      try {
        const d = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/mappers/" + enc(ref.table));
        return { text: d.raw_text, path: d.path, readonly: d.source_kind !== "yaml", deletable: d.source_kind === "yaml" };
      } catch (err) {
        if (err.status !== 404) throw err;
        const t = window.__pbInspect && window.__pbInspect.tables.find((x) => x.name === ref.table);
        const note = t && t.mapper === "cdm_default" ? "This table uses the CDM-default (legacy) mapper; saving creates a package-specific mapper that replaces it."
          : t && t.mapper === "legacy_class" ? "This table uses a legacy mapper class; saving a YAML mapper overrides it." : "No mapper yet — the map stage passes rows through.";
        return { text: "v: 1\ntable: " + ref.table + "\nops: []\n", path: "mappers/" + ref.table + ".yaml", isNew: true, note };
      }
    },
    save(ref, text) { return api("PUT", "/pipeline-packages/" + enc(PIPELINE) + "/mappers/" + enc(ref.table), { raw_text: text }); },
    del(ref) { return api("DELETE", "/pipeline-packages/" + enc(PIPELINE) + "/mappers/" + enc(ref.table)); },
    async form(data, onChange) {
      const ops = await catalogue("mapper_ops");
      if (data.class) {
        const p = document.createElement("p");
        p.className = "note";
        p.innerHTML = "This mapper delegates to a custom class <code>" + esc(data.class) + "</code>. Edit in the YAML tab.";
        return p;
      }
      return stepsEditor(data.ops || [], ops, (steps) => { data.ops = steps; onChange(data); });
    },
    validateType: "mapper",
  },

  formatter: {
    icon: "table",
    async load(ref) {
      try {
        const d = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/formatters/" + enc(ref.table));
        const generated = /Do not hand-edit|ovid2: legacy-port/.test(d.raw_text || "");
        return { text: d.raw_text, path: d.path, readonly: d.source_kind !== "yaml" || generated,
          deletable: d.source_kind === "yaml" && !generated,
          note: generated ? "Generated by the legacy migrator — regenerate instead of editing (AGENTS.md)." : "" };
      } catch (err) {
        if (err.status !== 404) throw err;
        const t = window.__pbInspect && window.__pbInspect.tables.find((x) => x.name === ref.table);
        if (t && t.formatter === "custom") {
          const rel = "formatters_custom/" + ref.table + "_formatter.py";
          const f = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/file?relative_path=" + enc(rel));
          return { text: f.content, path: f.absolute_path || rel, readonly: true, code: true,
            note: "Hand-written custom formatter (Python) — read-only here. Replace it with a YAML spec to edit declaratively." };
        }
        return { text: "v: 1\nformatter: " + ref.table + "\nadapter: source_passthrough\ninput_file_name: \"*" + ref.table + "*\"\n",
          path: "formatters/" + ref.table + ".yaml", isNew: true, note: "No formatter yet — this table is skipped by --tables all until you save one." };
      }
    },
    save(ref, text) { return api("PUT", "/pipeline-packages/" + enc(PIPELINE) + "/formatters/" + enc(ref.table), { raw_text: text }); },
    del(ref) { return api("DELETE", "/pipeline-packages/" + enc(PIPELINE) + "/formatters/" + enc(ref.table)); },
    async form(data, onChange) {
      const box = document.createElement("div");
      box.className = "pb-form";
      const schema = {
        type: "object",
        required: ["adapter"],
        properties: {
          adapter: { type: "string", enum: ["source_passthrough", "pcornet_passthrough", "omop_concept_join", "semantic_join", "union_of_specs", "custom"] },
          input_file_name: { type: "string", description: "Glob of the primary source file(s) in the input address." },
          auxiliary_tables: { type: "array", items: { type: "string" }, description: "Tables registered as views before this formatter runs." },
          class: { type: "string", description: "Dotted class path (adapter: custom)." },
          enabled: { type: "boolean" },
        },
      };
      const head = Object.fromEntries(Object.entries(data).filter(([k]) => k in schema.properties));
      box.appendChild(paramsForm(schema, head, (p) => {
        for (const k of Object.keys(schema.properties)) delete data[k];
        Object.assign(data, p);
        onChange(data);
      }));
      // Projections: OUT_COL → SQL expression, as a key/value grid.
      if (data.adapter !== "custom" && data.adapter !== "union_of_specs") {
        const sec = document.createElement("fieldset");
        sec.className = "nw-id-group";
        sec.innerHTML = "<legend>projections (output column → SQL expression)</legend>";
        const grid = document.createElement("div");
        grid.className = "pb-kv";
        const proj = { ...(data.projections || {}) };
        const draw = () => {
          grid.innerHTML = '<span class="pb-kv-head">column</span><span class="pb-kv-head">expression</span><span></span>';
          for (const [k, v] of Object.entries(proj)) {
            const ki = document.createElement("input"); ki.value = k; ki.className = "mono"; ki.setAttribute("aria-label", "column");
            const vi = document.createElement("input"); vi.value = v; vi.className = "mono"; vi.setAttribute("aria-label", "expression for " + k);
            const rm = document.createElement("button"); rm.type = "button"; rm.className = "icon-button"; rm.textContent = "✕";
            let key = k;
            ki.addEventListener("change", () => { const val = proj[key]; delete proj[key]; key = ki.value.toUpperCase(); proj[key] = val; data.projections = { ...proj }; onChange(data); draw(); });
            vi.addEventListener("input", () => { proj[key] = vi.value; data.projections = { ...proj }; onChange(data); });
            rm.addEventListener("click", () => { delete proj[key]; data.projections = { ...proj }; onChange(data); draw(); });
            grid.append(ki, vi, rm);
          }
          const add = document.createElement("button");
          add.type = "button"; add.className = "button small"; add.textContent = "+ column";
          add.addEventListener("click", () => { let n = "NEW_COLUMN", i = 1; while (proj[n]) n = "NEW_COLUMN_" + i++; proj[n] = ""; draw(); });
          grid.append(add);
        };
        draw();
        sec.appendChild(grid);
        box.appendChild(sec);
      }
      const more = Object.keys(data).filter((k) => !["v", "formatter", "projections", ...Object.keys(schema.properties)].includes(k));
      if (more.length) {
        const n = document.createElement("p");
        n.className = "nw-hint small";
        n.innerHTML = "Also defines <code>" + more.map(esc).join("</code>, <code>") + "</code> — edit those in the YAML tab.";
        box.appendChild(n);
      }
      return box;
    },
    validateType: "formatter",
  },

  settings: {
    icon: "sliders",
    async load() {
      const d = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/settings");
      return { settings: d, path: d.path, formOnly: true };
    },
    async form(_data, onChange, _ref, loaded) {
      const box = document.createElement("div");
      box.className = "pb-form";
      const groups = {};
      for (const c of loaded.settings.constants) (groups[c.category] = groups[c.category] || []).push(c);
      const changes = {};
      const names = { cdm: "CDM", harvest: "HARVEST network / datamart", warehouse: "Warehouse", datavant: "Datavant", omop_source: "OMOP cdm_source", other: "Other constants" };
      for (const [cat, rows] of Object.entries(groups)) {
        const fs = document.createElement("fieldset");
        fs.className = "nw-id-group";
        fs.innerHTML = "<legend>" + esc(names[cat] || cat) + "</legend>";
        for (const c of rows) {
          const row = document.createElement("label");
          row.className = "pb-param";
          row.innerHTML = "<span>" + esc(c.label || c.key) + ' <code class="nw-id-key">' + esc(c.key) + "</code></span>";
          let input;
          if (c.choices && c.choices.length) {
            input = document.createElement("select");
            input.innerHTML = c.choices.map((x) => '<option' + (x === c.value ? " selected" : "") + ">" + esc(x) + "</option>").join("");
          } else if (c.type === "bool") {
            input = document.createElement("select");
            input.innerHTML = '<option value="true"' + (c.value ? " selected" : "") + '>true</option><option value="false"' + (!c.value ? " selected" : "") + ">false</option>";
          } else {
            input = document.createElement("input");
            input.type = c.type === "int" || c.type === "float" ? "number" : "text";
            input.value = c.value == null ? "" : (typeof c.value === "object" ? JSON.stringify(c.value) : String(c.value));
          }
          input.disabled = !c.editable || !CAN_WRITE;
          if (!c.editable) input.title = c.alias_of ? "= " + c.alias_of + " (edit " + c.alias_of + ")" : "Computed in Python — edit on disk";
          const handler = () => {
            const v = c.type === "bool" ? input.value === "true" : input.value;
            if (String(v) === String(c.value)) delete changes[c.key]; else changes[c.key] = v;
            onChange(changes);
          };
          input.addEventListener("input", handler);
          input.addEventListener("change", handler);
          row.appendChild(input);
          const small = document.createElement("small");
          small.textContent = c.must_match ? "must match: " + c.must_match : !c.editable ? (c.alias_of ? "alias of " + c.alias_of : "computed — read-only") : "";
          if (small.textContent) row.appendChild(small);
          fs.appendChild(row);
        }
        box.appendChild(fs);
      }
      return box;
    },
    async saveForm(changes) {
      return api("PATCH", "/pipeline-packages/" + enc(PIPELINE) + "/settings", { values: changes });
    },
  },
};

/* ------------------------------------------------------------- editor */

class Editor {
  constructor(panel) {
    this.panel = panel;
    this.title = $("[data-editor-title]", panel);
    this.pathEl = $("[data-editor-path]", panel);
    this.tabs = $("[data-editor-tabs]", panel);
    this.body = $("[data-editor-body]", panel);
    this.yamlPane = $('[data-pane="yaml"]', panel);
    this.textarea = $("[data-yaml-source]", panel);
    this.errors = $("[data-editor-errors]", panel);
    this.actions = $("[data-editor-actions]", panel);
    this.status = $("[data-editor-status]", panel);
    this.saveBtn = $("[data-editor-save]", panel);
    this.delBtn = $("[data-editor-delete]", panel);
    this.revertBtn = $("[data-editor-revert]", panel);
    this.cm = null;
    this.tab = "form";
    this.tabs.addEventListener("click", (e) => {
      const b = e.target.closest("[data-tab]");
      if (b && !b.disabled) this.showTab(b.dataset.tab);
    });
    this.saveBtn.addEventListener("click", () => this.save());
    this.revertBtn.addEventListener("click", () => this.open(this.type, this.ref, { force: true }));
    this.delBtn.addEventListener("click", () => this.remove());
    window.addEventListener("beforeunload", (e) => { if (this.dirty) { e.preventDefault(); e.returnValue = ""; } });
  }

  setStatus(text, cls) {
    this.status.textContent = text || "";
    this.status.className = "pb-status small" + (cls ? " " + cls : "");
  }

  showErrors(list) {
    if (!list || !list.length) { this.errors.hidden = true; this.errors.replaceChildren(); return; }
    this.errors.hidden = false;
    this.errors.innerHTML = list.map((e) => "<li>" + esc(typeof e === "string" ? e : e.message) + "</li>").join("");
  }

  async ensureCm() {
    if (this.cm) return this.cm;
    const m = await yamlMod();
    this.cm = m.mountYamlEditor(this.textarea, {
      validate: async (text) => {
        if (!this.def || !this.def.validateType) return [];
        const r = await api("POST", "/pipeline-packages/" + enc(PIPELINE || "_") + "/builder:validate",
          { doc_type: this.def.validateType, raw_text: text });
        return (r.errors || []).map((e) => ({ path: e.path, message: e.message }));
      },
      onChange: (text) => this.fromYaml(text),
    });
    return this.cm;
  }

  async open(type, ref, opts = {}) {
    if (this.dirty && !opts.force && !window.confirm("Discard unsaved changes?")) return false;
    this.type = type;
    this.ref = ref || {};
    this.def = DOCS[type];
    this.dirty = false;
    this.commentsLost = false;
    this.showErrors([]);
    this.setStatus("Loading…");
    this.title.textContent = ref && ref.label ? ref.label : type;
    this.tabs.hidden = false;
    this.actions.hidden = false;
    let loaded;
    try {
      loaded = opts.loaded || await this.def.load(this.ref);
    } catch (err) {
      this.body.innerHTML = '<p class="flash error">' + esc(err.message) + "</p>";
      this.setStatus("");
      return true;
    }
    this.loaded = loaded;
    this.readonly = !!loaded.readonly || !CAN_WRITE;
    this.pathEl.textContent = loaded.path || "";
    this.delBtn.hidden = !(loaded.deletable && CAN_WRITE && this.def.del);
    this.saveBtn.disabled = this.readonly;
    this.saveBtn.hidden = !!loaded.readonly;
    this.revertBtn.hidden = !!loaded.readonly;
    $('[data-tab="yaml"]', this.tabs).disabled = !!loaded.formOnly;
    $('[data-tab="form"]', this.tabs).disabled = !!loaded.code;
    if (loaded.formOnly) {
      this.data = {};
      this.yamlPane.hidden = true;
      await this.renderForm();
      this.showTab("form");
    } else {
      const m = await yamlMod();
      await this.ensureCm();
      this.cm.setValue(loaded.text || "");
      this.hasComments = /(^|\s)#/.test(loaded.text || "");
      try { this.data = m.yamlParse(loaded.text || "") || (this.def.blank ? this.def.blank(this.ref) : {}); } catch (_e) { this.data = null; }
      this.cm.view.dispatch({});  // trigger lint
      await this.renderForm();
      this.showTab(loaded.code || !this.data ? "yaml" : opts.tab || this.tab || "form");
    }
    const notes = [loaded.note, this.readonly && !loaded.readonly ? "Read-only — requires the manage-config capability." : ""].filter(Boolean);
    this.setStatus(notes.join(" "), "");
    return true;
  }

  showTab(tab) {
    this.tab = tab;
    for (const b of $$("[data-tab]", this.tabs)) b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false");
    this.body.hidden = tab !== "form";
    this.yamlPane.hidden = tab !== "yaml" || !!(this.loaded && this.loaded.formOnly);
    if (tab === "form" && this.data && this.formStale) this.renderForm();
    if (tab === "yaml" && this.cm) this.cm.view.requestMeasure();
  }

  async renderForm() {
    this.formStale = false;
    if (!this.def.form) { this.body.innerHTML = ""; return; }
    if (this.data == null) {
      this.body.innerHTML = '<p class="note">The YAML does not parse — fix it in the YAML tab.</p>';
      return;
    }
    const node = await this.def.form(this.data, (d) => this.fromForm(d), this.ref, this.loaded);
    if (this.readonly) $$("input, select, textarea, button", node).forEach((n) => { n.disabled = true; });
    this.body.replaceChildren(node);
  }

  async fromForm(data) {
    if (this.readonly) return;
    this.dirty = true;
    if (this.loaded.formOnly) {
      this.formChanges = data;
      this.setStatus(Object.keys(data).length ? Object.keys(data).length + " change(s) — Save to write pipeline_settings.py" : "", "");
      return;
    }
    this.data = data;
    const m = await yamlMod();
    const text = m.yamlStringify(data, { lineWidth: 0 });
    this.cm.setValue(text);
    if (this.hasComments && !this.commentsLost) {
      this.commentsLost = true;
      this.setStatus("Editing in the form re-serialises the YAML — comments will be dropped on save. Use the YAML tab to keep them.", "is-err");
    } else this.setStatus("Unsaved changes", "");
  }

  async fromYaml(text) {
    this.dirty = true;
    const m = await yamlMod();
    try {
      this.data = m.yamlParse(text);
      this.formStale = true;
      if (this.tab === "form") this.renderForm();
      this.setStatus("Unsaved changes", "");
    } catch (_e) { this.data = null; this.formStale = true; }
  }

  async save() {
    if (this.readonly) return;
    this.saveBtn.disabled = true;
    this.setStatus("Saving…");
    this.showErrors([]);
    try {
      let res;
      if (this.loaded.formOnly) res = await this.def.saveForm(this.formChanges || {});
      else res = await this.def.save(this.ref, this.cm.getValue(), this.data);
      this.dirty = false;
      this.setStatus("Saved (" + (res && res.action || "ok") + ") " + new Date().toLocaleTimeString(), "is-ok");
      document.dispatchEvent(new CustomEvent("ovid2:spec-saved", { detail: { type: this.type, ref: this.ref } }));
      if (this.loaded.formOnly || this.loaded.isNew) await this.open(this.type, this.ref, { force: true, tab: this.tab });
      this.setStatus("Saved " + new Date().toLocaleTimeString(), "is-ok");
    } catch (err) {
      this.setStatus("Not saved", "is-err");
      this.showErrors(String(err.message).split("; "));
    } finally {
      this.saveBtn.disabled = this.readonly;
    }
  }

  async remove() {
    if (!this.def.del || !window.confirm("Delete " + (this.ref.label || this.type) + "?")) return;
    try {
      await this.def.del(this.ref);
      this.dirty = false;
      this.setStatus("Deleted", "is-ok");
      document.dispatchEvent(new CustomEvent("ovid2:spec-saved", { detail: { type: this.type, ref: this.ref, deleted: true } }));
    } catch (err) {
      this.setStatus("Not deleted", "is-err");
      this.showErrors([err.message]);
    }
  }
}

/* ------------------------------------------------------- pipeline page */

async function bootPipeline() {
  let ins = JSON.parse($("#pb-inspection").textContent);
  window.__pbInspect = ins;
  window.__pbTables = ins.tables.map((t) => t.name);
  const editor = new Editor($("#pb-editor-panel"));
  const flowPanel = $("#pb-flow-panel");
  const flow = new FlowGraph($("#pb-flow"), { name: "table", nodeW: 118 });
  let currentTable = "";

  const refresh = async () => {
    ins = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/inspect");
    window.__pbInspect = ins;
    for (const t of ins.tables) {
      const item = $('[data-table="' + CSS.escape(t.name) + '"]');
      if (!item) continue;
      const dots = $$(".pb-tree-dots i", item);
      [t.formatter, t.transforms.length, t.mapper, t.fixers.length].forEach((on, i) => dots[i].className = on ? "on" : "");
      item.classList.toggle("is-unconfigured", !t.configured);
    }
    const st = $("[data-stat-transforms]");
    if (st) st.textContent = ins.summary.with_transforms;
    const sc = $("[data-stat-checks]");
    if (sc) sc.textContent = ins.summary.errors + " / " + ins.summary.warnings;
    if (currentTable) drawFlow(currentTable);
  };

  const drawFlow = (name) => {
    const t = ins.tables.find((x) => x.name === name);
    if (!t) return;
    flowPanel.hidden = false;
    $("[data-flow-title]", flowPanel).textContent = name;
    $("[data-flow-sub]", flowPanel).textContent = [t.is_reference ? "reference table" : "", t.requires_family ? "needs " + t.requires_family : ""].filter(Boolean).join(" · ");
    const errs = ins.bindings.filter((b) => b.status === "error" && (b.where || "").includes(name));
    flow.render({
      caption: ins.cdm + " · " + name,
      nodes: [
        { id: "source", title: "Source", logo: "file", value: t.inputs[0] || (t.formatter ? "computed" : "—"),
          sub: t.auxiliary_tables.length ? "+" + t.auxiliary_tables.length + " aux" : "", state: t.inputs.length || t.formatter ? "done" : "todo",
          tip: t.inputs.join("\n") },
        { id: "formatter", title: "Format", logo: "etl", value: t.formatter === "yaml" ? t.adapter : t.formatter || "none",
          state: t.formatter ? "done" : "todo", active: editor.type === "formatter" },
        { id: "transform", title: "Transform", logo: "pipeline", value: t.transforms.length ? t.transforms.join(", ") : "none",
          state: t.transforms.length ? "done" : "off", badge: t.transforms.length || "", active: editor.type === "assignments" },
        { id: "mapper", title: "Map", logo: "table", value: t.mapper === "yaml" ? t.mapper_ops.length + " ops" : t.mapper || "none",
          sub: t.dictionaries.length ? t.dictionaries.length + " dicts" : "", state: t.mapper ? "done" : "off", active: editor.type === "mapper" },
        { id: "fixers", title: "Fix", logo: "check", value: t.fixers.length ? t.fixers.length + " fixes" : "none",
          state: errs.length ? "error" : t.fixers.length ? "done" : "off", badge: t.fixers.length || "", active: editor.type === "fixers",
          tip: t.fixers.join("\n") },
        { id: "output", title: name, logo: "table", value: t.configured ? "configured" : "skipped", state: t.configured ? "done" : "todo" },
      ],
    });
  };

  const selectTree = (sel) => {
    $$(".pb-tree-item").forEach((b) => b.classList.remove("is-current"));
    const n = $(sel);
    if (n) n.classList.add("is-current");
  };

  const openTableStage = async (name, stage) => {
    currentTable = name;
    selectTree('[data-table="' + CSS.escape(name) + '"]');
    drawFlow(name);
    const label = name + " · " + stage;
    if (stage === "formatter") await editor.open("formatter", { table: name, label });
    else if (stage === "mapper") await editor.open("mapper", { table: name, label });
    else if (stage === "transform") await editor.open("assignments", { table: name, label: name + " · transforms" });
    else if (stage === "fixers") await editor.open("fixers", { table: name, label: name + " · fix chain" });
    drawFlow(name);
    history.replaceState(null, "", "?table=" + enc(name) + "&doc=" + enc(stage));
  };

  const showChecks = () => {
    selectTree('[data-doc="checks"]');
    flowPanel.hidden = true;
    editor.tabs.hidden = true;
    editor.actions.hidden = true;
    editor.yamlPane.hidden = true;
    editor.body.hidden = false;
    editor.title.textContent = "Names that must match other configuration";
    editor.pathEl.textContent = "";
    const order = { error: 0, warn: 1, info: 2, ok: 3 };
    const rows = [...ins.bindings].sort((a, b) => order[a.status] - order[b.status]);
    editor.body.innerHTML = '<ul class="nw-bindings">' + rows.map((b) =>
      '<li class="nw-binding is-' + esc(b.status) + '"><span class="nw-binding-dot"></span><span class="nw-binding-body">' +
      '<span class="nw-binding-label">' + esc(b.label) + " <code>" + esc(b.value) + "</code></span>" +
      '<span class="nw-binding-meta small">' + esc(b.must_match) + (b.detail ? " — <strong>" + esc(b.detail) + "</strong>" : "") +
      (b.where ? ' <span class="nw-muted">(' + esc(b.where) + ")</span>" : "") + "</span></span></li>").join("") + "</ul>";
  };

  const showPrivateTransforms = async () => {
    selectTree('[data-doc="transforms"]');
    flowPanel.hidden = true;
    const list = await api("GET", "/pipeline-packages/" + enc(PIPELINE) + "/transforms");
    editor.tabs.hidden = true; editor.actions.hidden = true; editor.yamlPane.hidden = true; editor.body.hidden = false;
    editor.title.textContent = "Private transform chains";
    editor.pathEl.textContent = "transforms/*.yaml";
    editor.body.innerHTML = '<div class="pb-lib-grid">' + list.map((c) =>
      '<div class="pb-lib-card pl-card" data-scope="pipeline" data-open-private="' + esc(c.name) + '" tabindex="0">' +
      '<div class="pl-card-head"><span class="pl-card-badge">⚡</span><span class="pl-card-title"><span class="pl-card-name mono">' + esc(c.name) + "</span>" +
      '<span class="pl-card-sub">' + c.steps + " steps</span></span></div><p class=\"small nw-muted\">" + esc(c.description) + '</p>' +
      '<div class="pb-lib-ops">' + c.ops.map((o) => '<span class="nw-chip is-tx">' + esc(o) + "</span>").join("") + "</div></div>").join("") +
      "</div>" + (CAN_WRITE ? '<div class="pb-add-row" style="margin-top:.7rem"><input type="text" placeholder="new_chain_name" data-new-private aria-label="New chain name"><button type="button" class="button" data-create-private>+ New private chain</button></div>' : "") +
      '<p class="nw-hint small">Private chains override library chains of the same name for this pipeline only. Shared chains live in the <a href="/transforms">transform library</a>.</p>';
    $$("[data-open-private]", editor.body).forEach((c) => c.addEventListener("click", () =>
      editor.open("transform", { scope: "pipeline", name: c.dataset.openPrivate, label: c.dataset.openPrivate + " (private)" })));
    const btn = $("[data-create-private]", editor.body);
    if (btn) btn.addEventListener("click", () => {
      const name = $("[data-new-private]", editor.body).value.trim();
      if (!/^[a-z][a-z0-9_]*$/.test(name)) { window.alert("Use lower-case letters, digits and _."); return; }
      editor.open("transform", { scope: "pipeline", name, label: name + " (private, new)" },
        { loaded: { text: "v: 1\ndescription: \"\"\nsteps:\n  - op: trim\n    columns: []\n", path: "transforms/" + name + ".yaml", isNew: true } });
    });
  };

  $("#pb-tree").addEventListener("click", async (e) => {
    const t = e.target.closest("[data-table]");
    if (t) {
      const row = ins.tables.find((x) => x.name === t.dataset.table);
      await openTableStage(t.dataset.table, row && row.formatter ? (editor.type && ["formatter", "mapper", "fixers"].includes(editor.type) ? (editor.type === "fixers" ? "fixers" : editor.type) : "formatter") : "formatter");
      return;
    }
    const d = e.target.closest("[data-doc]");
    if (!d) return;
    currentTable = "";
    const doc = d.dataset.doc;
    history.replaceState(null, "", "?doc=" + enc(doc));
    if (doc === "checks") return showChecks();
    if (doc === "transforms") return showPrivateTransforms();
    selectTree('[data-doc="' + doc + '"]');
    flowPanel.hidden = true;
    const labels = { assignments: "Transform assignments", fixers: "Fixer chains", settings: "Settings (pipeline_settings.py)" };
    editor.body.hidden = false;
    await editor.open(doc, { label: labels[doc] || doc });
  });
  const filter = $("[data-tree-filter]");
  filter.addEventListener("input", () => {
    const q = filter.value.toLowerCase();
    $$("[data-table]").forEach((b) => { b.hidden = q && !b.dataset.table.includes(q); });
  });
  document.addEventListener("ovid2:flow-focus", (e) => {
    if (e.detail.graph !== "table" || !currentTable) return;
    const id = e.detail.id;
    if (["formatter", "mapper", "fixers"].includes(id)) openTableStage(currentTable, id);
    else if (id === "transform") openTableStage(currentTable, "transform");
    else if (id === "source") openTableStage(currentTable, "formatter");
  });
  document.addEventListener("ovid2:spec-saved", () => refresh().catch(() => {}));

  const it = root.dataset.initialTable;
  const idoc = root.dataset.initialDoc;
  if (it) await openTableStage(it, idoc || "formatter");
  else if (idoc && idoc !== "transforms" && idoc !== "checks") $('[data-doc="' + CSS.escape(idoc) + '"]').click();
  else if (idoc) $('[data-doc="' + CSS.escape(idoc) + '"]').click();
  else showChecks();
}

/* ------------------------------------------------------- library page */

async function bootLibrary() {
  const editor = new Editor($("#pb-editor-panel"));
  const tree = $("[data-lib-tree]");
  const assign = $("#pb-assign-panel");
  let lib = [];

  const drawTree = () => {
    const q = ($("[data-tree-filter]").value || "").toLowerCase();
    const groups = { user: "My transforms", shipped: "Shipped (read-only)" };
    tree.innerHTML = Object.entries(groups).map(([scope, label]) => {
      const items = lib.filter((c) => c.scope === scope && (!q || c.name.includes(q)));
      return '<p class="pb-tree-group">' + esc(label) + "</p>" + (items.length ? items.map((c) =>
        '<button type="button" class="pb-tree-item" data-lib="' + esc(c.name) + '" data-scope="' + esc(c.scope) + '">⚡<span class="pb-tree-name">' +
        esc(c.name) + '</span><span class="nw-muted small">' + c.steps + "</span></button>").join("") : '<p class="nw-muted small" style="padding:0 .45rem">none</p>');
    }).join("");
  };

  const grid = () => {
    editor.tabs.hidden = true; editor.actions.hidden = true; editor.yamlPane.hidden = true; editor.body.hidden = false;
    editor.title.textContent = "Transforms";
    editor.pathEl.textContent = "";
    assign.hidden = true;
    editor.body.innerHTML = '<div class="pb-lib-grid">' + (lib.length ? lib.map((c) =>
      '<div class="pb-lib-card pl-card" data-scope="' + esc(c.scope) + '" data-open-lib="' + esc(c.name) + '" tabindex="0">' +
      '<div class="pl-card-head"><span class="pl-card-badge">⚡</span><span class="pl-card-title"><span class="pl-card-name mono">' + esc(c.name) +
      '</span><span class="pl-card-sub"><span class="pl-kind-label">' + esc(c.scope) + "</span> · " + c.steps + " steps</span></span></div>" +
      '<p class="small nw-muted">' + esc(c.description || "") + '</p><div class="pb-lib-ops">' +
      c.ops.map((o) => '<span class="nw-chip is-tx">' + esc(o) + "</span>").join("") + "</div></div>").join("")
      : '<div class="pl-empty"><h2>No transforms yet</h2><p>Create a reusable chain and assign it to your pipelines.</p></div>') + "</div>";
    $$("[data-open-lib]", editor.body).forEach((c) => c.addEventListener("click", () => openLib(c.dataset.openLib)));
  };

  const reload = async () => {
    cache.library = null;
    lib = await catalogue("library");
    drawTree();
  };

  const openLib = async (name, loaded) => {
    $$("[data-dup-button]", editor.actions).forEach((b) => b.remove());
    $$(".pb-tree-item").forEach((b) => b.classList.toggle("is-current", b.dataset.lib === name));
    const c = lib.find((x) => x.name === name);
    const ok = await editor.open("transform", { scope: c ? c.scope : "user", name, label: name }, loaded ? { loaded } : {});
    if (!ok) return;
    assign.hidden = !c;
    $("[data-assign-status]", assign).textContent = c && c.scope === "shipped"
      ? "Shipped chains can be assigned as-is, or duplicated (save under a new name) to customise." : "";
    history.replaceState(null, "", "?name=" + enc(name));
    if (c && c.scope === "shipped" && CAN_WRITE) {
      editor.setStatus("Shipped chain — read-only. Duplicate it to customise.", "");
      const dup = document.createElement("button");
      dup.type = "button"; dup.className = "button"; dup.textContent = "Duplicate…";
      dup.addEventListener("click", () => {
        const nn = window.prompt("Name for the copy", name + "_copy");
        if (nn) openLib(nn, { text: editor.cm.getValue(), path: "(new) " + nn + ".yaml", isNew: true });
      });
      dup.dataset.dupButton = "1";
      editor.actions.hidden = false;
      editor.actions.prepend(dup);
    }
  };

  const showCatalogue = async () => {
    $$(".pb-tree-item").forEach((b) => b.classList.toggle("is-current", b.dataset.doc === "catalogue"));
    const ops = await catalogue("transforms");
    editor.tabs.hidden = true; editor.actions.hidden = true; editor.yamlPane.hidden = true; editor.body.hidden = false;
    assign.hidden = true;
    editor.title.textContent = "Op catalogue";
    editor.pathEl.textContent = ops.length + " ops";
    editor.body.innerHTML = '<div class="pb-op-catalogue">' + ops.map((o) =>
      '<div class="pb-op"><strong>' + esc(o.name) + "</strong><span>" + esc(o.summary) + '</span><span class="nw-chip">' + esc(o.category) +
      "</span>" + (o.requires_family ? '<span class="nw-chip is-warn">' + esc(o.requires_family) + " only</span>" : "") + "</div>").join("") + "</div>";
  };

  $("#pb-tree").addEventListener("click", (e) => {
    const b = e.target.closest("[data-lib]");
    if (b) return openLib(b.dataset.lib);
    if (e.target.closest('[data-doc="catalogue"]')) return showCatalogue();
  });
  $("[data-tree-filter]").addEventListener("input", drawTree);
  const newBtn = $("[data-lib-new]");
  if (newBtn) newBtn.addEventListener("click", () => {
    const name = window.prompt("Name for the new transform (lower-case, digits, _)", "my_transform");
    if (!name) return;
    if (!/^[a-z][a-z0-9_]*$/.test(name)) { window.alert("Use lower-case letters, digits and _ (start with a letter)."); return; }
    openLib(name, {
      text: "v: 1\ndescription: \"\"\nsteps:\n  - op: derive_column\n    column: SEX\n    expr: \"coalesce(nullif(upper(trim(SEX)), ''), 'UN')\"\n",
      path: "(new) " + name + ".yaml", isNew: true, deletable: false,
    });
  });
  $("[data-assign-go]", assign).addEventListener("click", async () => {
    const pipeline = $("[data-assign-pipeline]", assign).value;
    const tables = $("[data-assign-tables]", assign).value.split(/[\s,]+/).filter(Boolean);
    const status = $("[data-assign-status]", assign);
    if (!pipeline || !tables.length) { status.textContent = "Pick a pipeline and at least one table (or *)."; return; }
    if (editor.dirty) { status.textContent = "Save the transform first."; return; }
    try {
      await api("POST", "/transform-library/" + enc(editor.ref.name) + "/assign", { pipeline, tables });
      status.className = "pb-status small is-ok";
      status.innerHTML = "Assigned to " + esc(pipeline) + " (" + tables.map(esc).join(", ") + ") — <a href=\"/pipeline-packages/" + enc(pipeline) + "/builder?doc=assignments\">open in the builder</a>.";
    } catch (err) {
      status.className = "pb-status small is-err";
      status.textContent = err.message;
    }
  });
  document.addEventListener("ovid2:spec-saved", async (e) => {
    await reload();
    if (e.detail.deleted) grid();
  });

  await reload();
  const initial = root.dataset.initialName;
  if (initial) openLib(initial); else grid();
}

if (root) {
  (MODE === "library" ? bootLibrary() : bootPipeline()).catch((err) => {
    const panel = $("#pb-editor-panel [data-editor-body]");
    if (panel) panel.innerHTML = '<p class="flash error">Builder failed to start: ' + esc(err.message) + "</p>";
  });
}
