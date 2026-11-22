/*
 * Workflow link condition builder (workflow editor, link inspector).
 *
 * Renders a link's ``when`` tree — ALL / ANY groups (with a NOT toggle) of
 * leaf tests ``{field, op, value}`` — as nested rows of selects, fed only
 * from the server catalogue (GET /api/v1/workflow-conditions, embedded in the
 * page as ``catalog.conditions``): fields, operators per field type, choices.
 * A field that must match a known set is a select (UI rule U2); free text is
 * used only for genuinely open values (table names, numbers, patterns).
 *
 * The builder never mutates state itself: every edit calls ``onChange(tree)``
 * with a fresh tree (or ``null`` for "no condition"), which the canvas
 * commits through its undo stack.
 */
import { esc } from "/static/workflow_graph.js";

const GROUP_LABEL = { all: "ALL of", any: "ANY of" };

export class ConditionBuilder {
  /**
   * @param {HTMLElement} host   Container to render into.
   * @param {object} cat         ``catalog.conditions`` (fields / ops / ops_by_type).
   * @param {(tree: object|null) => void} onChange
   */
  constructor(host, cat, onChange) {
    this.host = host;
    this.cat = cat || { fields: [], ops: {}, ops_by_type: {} };
    this.onChange = onChange;
    this.fields = this.cat.fields || [];
    this.probes = [];
    this.disabled = false;
    host.addEventListener("change", (ev) => this._edit(ev));
    host.addEventListener("click", (ev) => this._click(ev));
  }

  /* ---------------------------------------------------------- field model */

  /** Return ``{spec, param}`` for a field name (parameterised fields too). */
  lookup(name) {
    for (const f of this.fields) if (!f.param && f.name === name) return { spec: f, param: "" };
    for (const f of this.fields) {
      if (!f.param || !String(name || "").startsWith(f.name)) continue;
      let rest = name.slice(f.name.length);
      if (f.suffix) {
        if (!rest.endsWith(f.suffix)) continue;
        rest = rest.slice(0, -f.suffix.length);
      }
      return { spec: f, param: rest };
    }
    return { spec: null, param: "" };
  }

  fieldName(spec, param) {
    return spec.param ? `${spec.name}${param || ""}${spec.suffix || ""}` : spec.name;
  }

  /* ---------------------------------------------------------- render */

  render(tree, { probes = [], disabled = false } = {}) {
    this.tree = tree ? normalise(JSON.parse(JSON.stringify(tree))) : null;
    this.probes = probes;
    this.disabled = disabled;
    this.host.innerHTML = this.tree ? this._node(this._asGroup(this.tree), "") : "";
  }

  /** Normalise the root to a group so it can always take another test. */
  _asGroup(node) {
    if (node.all || node.any) return node;
    if (node.not && (node.not.all || node.not.any)) return node;
    return { all: [node] };
  }

  _node(node, path) {
    if (node.not && (node.not.all || node.not.any)) return this._group(node.not, path, true);
    if (node.all || node.any) return this._group(node, path, false);
    if (node.not) return this._group({ all: [node.not] }, path, true);
    return this._leaf(node, path);
  }

  _group(g, path, negated) {
    const key = g.all ? "all" : "any";
    const items = g[key] || [];
    const dis = this.disabled ? " disabled" : "";
    const rows = items.map((c, i) => this._node(c, `${path}${path ? "." : ""}${key}.${i}`)).join("");
    return `<div class="wf-cond-group${negated ? " is-not" : ""}" data-path="${esc(path)}">` +
      `<div class="wf-cond-group-head">` +
      `<label class="check small"><input type="checkbox" data-cond-act="not"${negated ? " checked" : ""}${dis}> NOT</label>` +
      `<select class="wf-cond-key" data-cond-act="key" aria-label="Combine tests"${dis}>` +
      `<option value="all"${key === "all" ? " selected" : ""}>${GROUP_LABEL.all}</option>` +
      `<option value="any"${key === "any" ? " selected" : ""}>${GROUP_LABEL.any}</option></select>` +
      (path ? `<button type="button" class="icon-button small" data-cond-act="remove" title="Remove group" aria-label="Remove group"${dis}>&times;</button>` : "") +
      `</div><div class="wf-cond-items">${rows}</div>` +
      (this.disabled ? "" :
        `<div class="wf-cond-add"><button type="button" class="button small" data-cond-act="add-test">+ test</button>` +
        `<button type="button" class="button small" data-cond-act="add-group">+ group</button></div>`) +
      `</div>`;
  }

  _fieldOptions(cur) {
    const groups = {};
    for (const f of this.fields) (groups[f.group] = groups[f.group] || []).push(f);
    const label = { run: "Run", timeline: "Tables & log", health: "Health checks", output: "Output", analysis: "Row counts", metrics: "Engine", query: "Data probes" };
    return Object.entries(groups).map(([g, fs]) =>
      `<optgroup label="${esc(label[g] || g)}">` +
      fs.map((f) => `<option value="${esc(f.name)}"${cur && cur.name === f.name ? " selected" : ""}>${esc(f.label)}</option>`).join("") +
      `</optgroup>`).join("");
  }

  _leaf(node, path) {
    const { spec, param } = this.lookup(node.field);
    const dis = this.disabled ? " disabled" : "";
    const type = spec ? spec.type : "str";
    const ops = (this.cat.ops_by_type || {})[type] || [];
    const op = ops.includes(node.op) ? node.op : ops[0];
    let paramHtml = "";
    if (spec && spec.param) {
      if (spec.group === "query") {
        const names = this.probes.map((p) => p.name).filter(Boolean);
        if (param && !names.includes(param)) names.push(param);
        paramHtml = `<select data-cond-act="param" aria-label="Probe"${dis}>` +
          (names.length ? "" : `<option value="">(no probes on this step)</option>`) +
          names.map((n) => `<option value="${esc(n)}"${n === param ? " selected" : ""}>${esc(n)}</option>`).join("") + `</select>`;
      } else if (spec.param === "severity") {
        paramHtml = `<select data-cond-act="param" aria-label="Severity"${dis}>` +
          ["blocker", "warning", "info"].map((n) => `<option value="${n}"${n === param ? " selected" : ""}>${n}</option>`).join("") + `</select>`;
      } else {
        paramHtml = `<input type="text" class="mono" data-cond-act="param" data-allow-new placeholder="${esc(spec.param)}" value="${esc(param)}" aria-label="${esc(spec.param)}"${dis}>`;
      }
    }
    const arity = ((this.cat.ops || {})[op] || {}).arity;
    let valueHtml = "";
    const choices = spec && spec.choices && spec.choices.length && (op === "eq" || op === "ne") ? spec.choices : null;
    if (arity === 1 && choices) {
      valueHtml = `<select data-cond-act="value" aria-label="Value"${dis}>` +
        choices.map((c) => `<option value="${esc(c)}"${c === node.value ? " selected" : ""}>${esc(c)}</option>`).join("") + `</select>`;
    } else if (arity === 1) {
      valueHtml = `<input type="${type === "num" ? "number" : "text"}" step="any" class="mono" data-cond-act="value" data-allow-new value="${esc(node.value ?? "")}" aria-label="Value"${dis}>`;
    } else if (arity === 2) {
      const [lo, hi] = Array.isArray(node.value) ? node.value : ["", ""];
      valueHtml = `<input type="number" step="any" class="mono" data-cond-act="lo" value="${esc(lo ?? "")}" aria-label="From"${dis}>` +
        `<span class="small">and</span><input type="number" step="any" class="mono" data-cond-act="hi" value="${esc(hi ?? "")}" aria-label="To"${dis}>`;
    } else if (arity === -1) {
      const v = Array.isArray(node.value) ? node.value.join(", ") : "";
      valueHtml = `<input type="text" class="mono" data-cond-act="list" data-allow-new value="${esc(v)}" placeholder="a, b, c" aria-label="Values"${dis}>`;
    }
    const cs = type !== "num" && type !== "bool" && arity !== 0
      ? `<label class="check small" title="Match case"><input type="checkbox" data-cond-act="cs"${node.case_sensitive ? " checked" : ""}${dis}> Aa</label>` : "";
    const tip = spec && spec.help && window.Ovid2HelpTip ? window.Ovid2HelpTip.html(spec.help, spec.label) : "";
    return `<div class="wf-cond-leaf" data-path="${esc(path)}">` +
      `<select data-cond-act="field" aria-label="Field"${dis}>${this._fieldOptions(spec)}</select>${tip}${paramHtml}` +
      `<select data-cond-act="op" aria-label="Operator"${dis}>` +
      ops.map((o) => `<option value="${o}"${o === op ? " selected" : ""}>${esc(((this.cat.ops || {})[o] || {}).label || o)}</option>`).join("") +
      `</select>${valueHtml}${cs}` +
      (this.disabled ? "" : `<button type="button" class="icon-button small" data-cond-act="remove" title="Remove test" aria-label="Remove test">&times;</button>`) +
      `</div>`;
  }

  /* ---------------------------------------------------------- edits */

  _root() {
    return this._asGroup(this.tree || { all: [] });
  }

  /** Walk ``path`` ("all.0.any.2") to [parentArray, index, node]. */
  _locate(root, path) {
    if (!path) return [null, -1, root];
    const parts = path.split(".");
    let node = root;
    let arr = null;
    let idx = -1;
    for (let i = 0; i < parts.length; i += 2) {
      const g = node.not && (node.not.all || node.not.any) ? node.not : node;
      arr = g[parts[i]];
      idx = Number(parts[i + 1]);
      node = arr[idx];
    }
    return [arr, idx, node];
  }

  _groupOf(node) {
    return node.not && (node.not.all || node.not.any) ? node.not : node;
  }

  defaultLeaf() {
    return { field: "status", op: "eq", value: "succeeded" };
  }

  _emit(root) {
    const g = this._groupOf(root);
    const items = g.all || g.any || [];
    if (!items.length) { this.onChange(null); return; }
    // A root ALL group holding a single test is stored as the bare test.
    if (!root.not && g.all && items.length === 1) { this.onChange(items[0]); return; }
    this.onChange(root);
  }

  _click(ev) {
    const btn = ev.target.closest("[data-cond-act]");
    if (!btn || btn.tagName !== "BUTTON" || this.disabled) return;
    const act = btn.dataset.condAct;
    const holder = btn.closest("[data-path]");
    const path = holder ? holder.dataset.path : "";
    const root = this._root();
    const [arr, idx, node] = this._locate(root, path);
    if (act === "add-test" || act === "add-group") {
      const g = this._groupOf(node);
      const items = g.all || g.any;
      items.push(act === "add-test" ? this.defaultLeaf() : { any: [this.defaultLeaf()] });
    } else if (act === "remove" && arr) {
      arr.splice(idx, 1);
    }
    this._emit(root);
  }

  _edit(ev) {
    const el = ev.target.closest("[data-cond-act]");
    if (!el || this.disabled) return;
    const act = el.dataset.condAct;
    const holder = el.closest("[data-path]");
    const path = holder ? holder.dataset.path : "";
    const root = this._root();
    let [arr, idx, node] = this._locate(root, path);
    if (act === "not" || act === "key") {
      const g = this._groupOf(node);
      const key = g.all ? "all" : "any";
      let next = act === "key" ? { [el.value]: g[key] } : g;
      const negated = act === "not" ? el.checked : !!(node.not && (node.not.all || node.not.any));
      next = negated ? { not: next } : next;
      if (arr) arr[idx] = next; else { this._emit(next); return; }
      this._emit(root);
      return;
    }
    const leaf = node;
    const { spec, param } = this.lookup(leaf.field);
    if (act === "field") {
      const f = this.fields.find((x) => x.name === el.value);
      const name = f.param ? this.fieldName(f, f.group === "query" ? ((this.probes[0] || {}).name || "") : f.param === "severity" ? "blocker" : "") : f.name;
      const ops = (this.cat.ops_by_type || {})[f.type] || [];
      const next = { field: name, op: ops[0] };
      if (f.choices && f.choices.length) next.value = f.choices[0];
      else if (f.type === "num") next.value = 0;
      else if (f.type !== "bool") next.value = "";
      if (((this.cat.ops || {})[next.op] || {}).arity === 0) delete next.value;
      arr[idx] = next;
    } else if (act === "param") {
      leaf.field = this.fieldName(spec, el.value.trim());
    } else if (act === "op") {
      leaf.op = el.value;
      const arity = ((this.cat.ops || {})[el.value] || {}).arity;
      if (arity === 0) delete leaf.value;
      else if (arity === 2) leaf.value = Array.isArray(leaf.value) && leaf.value.length === 2 ? leaf.value : [0, 0];
      else if (arity === -1) leaf.value = Array.isArray(leaf.value) ? leaf.value : (leaf.value !== undefined && leaf.value !== "" ? [leaf.value] : []);
      else if (Array.isArray(leaf.value)) leaf.value = leaf.value[0] ?? "";
      else if (leaf.value === undefined) leaf.value = spec && spec.type === "num" ? 0 : "";
    } else if (act === "value") {
      leaf.value = spec && spec.type === "num" && el.value !== "" ? Number(el.value) : el.value;
    } else if (act === "lo" || act === "hi") {
      const v = Array.isArray(leaf.value) ? leaf.value.slice() : [0, 0];
      v[act === "lo" ? 0 : 1] = el.value === "" ? "" : Number(el.value);
      leaf.value = v;
    } else if (act === "list") {
      leaf.value = el.value.split(",").map((x) => x.trim()).filter(Boolean)
        .map((x) => (spec && spec.type === "num" && !Number.isNaN(Number(x)) ? Number(x) : x));
    } else if (act === "cs") {
      if (el.checked) leaf.case_sensitive = true; else delete leaf.case_sensitive;
    }
    void param;
    this._emit(root);
  }
}

/** Rewrite ``{not: leaf}`` as ``{not: {all: [leaf]}}`` so every NOT wraps a group. */
function normalise(node) {
  if (!node || typeof node !== "object") return node;
  if (node.all) return { all: node.all.map(normalise) };
  if (node.any) return { any: node.any.map(normalise) };
  if (node.not) {
    const inner = normalise(node.not);
    return { not: inner.all || inner.any ? inner : { all: [inner] } };
  }
  return node;
}

/** Short human text of a condition (canvas chip / summary line). */
export function summarise(cond, cat, limit = 0) {
  const ops = (cat && cat.ops) || {};
  const val = (v) => (Array.isArray(v) ? `[${v.join(", ")}]` : typeof v === "string" && (v === "" || /\s/.test(v)) ? JSON.stringify(v) : String(v));
  const text = (n) => {
    if (!n || typeof n !== "object") return "?";
    if (n.all) return `(${n.all.map(text).join(" AND ")})`;
    if (n.any) return `(${n.any.map(text).join(" OR ")})`;
    if (n.not) return `NOT ${text(n.not)}`;
    const o = ops[n.op] || {};
    return `${n.field} ${o.label || n.op}${o.arity === 0 ? "" : " " + val(n.value)}`;
  };
  let out = cond ? text(cond) : "";
  if (cond && (cond.all || cond.any)) out = out.slice(1, -1);
  if (limit && out.length > limit) out = out.slice(0, limit - 1) + "\u2026";
  return out;
}
