/*
 * Ovid2 new-pipeline wizard (/pipelines/new).
 *
 * Eight steps — Type → Template → Identity → Source → Engine → Destination
 * → Automation → Review — one visible at a time, with a progress rail
 * whose per-step state comes from a tagged requirement list. Navigation is
 * non-blocking: any step can be opened; Create validates everything.
 *
 * For an ETL pipeline the chosen template is inspected through
 * ``GET /api/v1/pipeline-templates/{id}?name=&engine=`` which returns:
 *
 *   tables    — every CDM table x {format, inputs, map, fix, transform}
 *   identity  — identity constants (current value + clone default)
 *   bindings  — every name that must match other configuration, checked
 *   requires_family — the engine family the template needs
 *
 * and drives the "What it runs" table grid (with an optional table
 * subset), the Identity form (``identity__<KEY>`` fields posted to the
 * scaffolder), the engine compatibility gate, the source-file match
 * preview (formatter patterns vs the chosen folder) and the Review.
 *
 * The right pane is a theme-aware FlowGraph: Source → Format → Transform
 * → Map → Fix → Output (ETL), or Source → <kind> → Output.
 *
 * Without JS the template renders every step stacked and the form posts
 * unchanged.
 */

import { FlowGraph } from "/static/flow_graph.js";

const STORAGE_LOGO = (t) => {
  const k = String(t || "").toLowerCase();
  return ["snowflake", "s3", "azure_blob", "databricks", "gcs", "postgres", "mssql", "disc", "access"].includes(k)
    ? k : "folder";
};

const esc = (s) => String(s == null ? "" : s).replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[c]);

/* Glob → RegExp for the source-file match preview (``*`` / ``?`` only). */
function globRe(glob) {
  const body = String(glob).replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".");
  return new RegExp("^" + body + "$", "i");
}

function debounce(fn, ms) {
  let t = 0;
  return (...a) => { window.clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
}

class PipelineBuilder {
  constructor(root) {
    this.root = root;
    this.form = document.getElementById("new-pipeline-form");
    this.canvas = document.getElementById("builder-canvas");
    this.saveBtn = document.getElementById("builder-save");
    this.reqList = document.getElementById("builder-req-list");
    if (!this.form || !this.canvas) return;
    this.graph = new FlowGraph(document.getElementById("flow-graph"), { name: "new" });
    this.inspection = null;
    this.inspectKey = "";
    this.listing = { key: "", names: [] };
    try { this.prefill = JSON.parse(root.dataset.identityPrefill || "{}"); } catch (_e) { this.prefill = {}; }
    try { this.families = JSON.parse(root.dataset.engineFamilies || "{}"); } catch (_e) { this.families = {}; }
    this.inspectSoon = debounce(() => this.inspect(), 350);
    this.bind();
    this.syncStorageToggles();
    this.setupWizard();
    this.syncKindTiles();
    this.inspect();
    this.render();
  }

  /* ---------------- field access ---------------- */

  field(name) { return this.form.querySelector('[data-builder="' + name + '"]'); }

  checkedRadio(name) {
    return this.form.querySelector('input[type="radio"][data-builder="' + name + '"]:checked');
  }

  value(name) {
    const node = this.field(name);
    if (!node) return "";
    if (node.type === "radio") {
      const c = this.checkedRadio(name);
      return c ? String(c.value || "").trim() : "";
    }
    return String(node.value || "").trim();
  }

  kind() { return this.value("kind") || "etl"; }
  isOpen(role) { return this.value(role + "_open") === "1"; }
  isTablesOpen() { return this.value("tables_open") === "1"; }
  visible(id) { const n = document.getElementById(id); return n ? n.style.display !== "none" : false; }

  connType(role) {
    const sel = this.form.querySelector('[data-storage-picker="' + role + '"] [data-role=storage-select]');
    if (!sel) return "";
    const opt = sel.options[sel.selectedIndex];
    return opt ? String(opt.getAttribute("data-type") || "") : "";
  }

  engineType() {
    const c = this.checkedRadio("engine");
    return c ? String(c.getAttribute("data-type") || "") : "";
  }

  engineFamily() {
    const c = this.checkedRadio("engine");
    return c ? String(c.getAttribute("data-family") || this.families[this.engineType()] || "") : "";
  }

  /* ---------------- events ---------------- */

  bind() {
    const handler = () => this.render();
    this.form.addEventListener("input", (evt) => {
      if (evt.target && evt.target.name === "name") this.inspectSoon();
      handler();
    });
    this.form.addEventListener("change", (evt) => {
      const t = evt.target;
      if (!t) return;
      if (t.matches('[data-storage-toggle]')) this.syncStorageToggles();
      if (t.name === "template" || t.name === "kind" || t.name === "engine" || t.name === "cdm") this.inspectSoon();
      if (t.name === "etl_tables_mode") this.renderTables();
      handler();
    });
    document.addEventListener("ovid2:builder-refresh", handler);
    document.addEventListener("ovid2:flow-focus", (e) => {
      if (e.detail && e.detail.graph === "new" && e.detail.id) this.goTo(e.detail.id);
    });
    // browse.js sets hidden .value directly; poll as a safety net.
    this._snap = "";
    window.setInterval(() => {
      const snap = ["input_connection", "input_location", "output_connection", "output_location"]
        .map((n) => this.value(n)).join("|");
      if (snap !== this._snap) {
        this._snap = snap;
        this.refreshSourceMatch();
        this.render();
      }
    }, 400);
  }

  syncStorageToggles() {
    for (const role of ["input", "output"]) {
      const picker = this.form.querySelector('[data-storage-picker="' + role + '"]');
      if (!picker) continue;
      const open = this.isOpen(role);
      picker.style.display = open ? "none" : "";
      if (open) {
        const c = this.field(role + "_connection");
        const l = this.field(role + "_location");
        if (c) c.value = "";
        if (l) l.value = "";
      }
    }
    const trigger = document.getElementById("trigger_type");
    if (!trigger) return;
    const watch = trigger.querySelector('option[value="watch"]');
    const inputOpen = this.isOpen("input");
    if (watch) {
      watch.disabled = inputOpen;
      watch.title = inputOpen ? "Unavailable while the input is kept open — bind an input to watch it." : "";
    }
    if (inputOpen && trigger.value === "watch") {
      trigger.value = "manual";
      if (typeof window.ovid2_toggle_new_trigger_fields === "function") window.ovid2_toggle_new_trigger_fields();
    }
  }

  /* ---------------- template inspection ---------------- */

  async inspect() {
    const tmpl = this.kind() === "etl" ? this.value("template") : "";
    if (!tmpl) {
      this.inspection = null;
      this.renderProvides();
      this.renderIdentity();
      this.render();
      return;
    }
    const name = this.value("name");
    const engine = this.value("engine");
    const key = [tmpl, name, engine].join("|");
    if (key === this.inspectKey) return;
    this.inspectKey = key;
    const qs = new URLSearchParams();
    if (name) qs.set("name", name);
    if (engine) qs.set("engine", engine);
    try {
      const r = await fetch("/api/v1/pipeline-templates/" + encodeURIComponent(tmpl) + "?" + qs,
        { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const body = await r.json();
      if (key !== this.inspectKey) return;  // a newer request superseded us
      this.inspection = body;
    } catch (err) {
      this.inspection = { error: String(err && err.message || err) };
    }
    this.renderProvides();
    this.renderIdentity();
    this.refreshSourceMatch();
    this.render();
  }

  renderProvides() {
    const statsEl = this.form.querySelector("[data-provides-stats]");
    const ins = this.inspection;
    if (statsEl) {
      if (!ins || ins.error) {
        statsEl.innerHTML = ins && ins.error ? '<p class="flash error">Could not inspect the template: ' + esc(ins.error) + "</p>" : "";
      } else {
        const s = ins.summary || {};
        const stat = (cls, icon, value, label) =>
          '<div class="pl-stat ' + cls + '"><span class="pl-stat-icon">' + icon + '</span><span class="pl-stat-body">' +
          '<span class="pl-stat-value">' + esc(value) + '</span><span class="pl-stat-label">' + esc(label) + "</span></span></div>";
        statsEl.innerHTML =
          stat("pl-stat-total", "&#9638;", (s.configured || 0) + "/" + (s.tables || 0), "tables configured") +
          stat("pl-stat-ok", "&#8644;", s.with_mapper || 0, "with a mapper") +
          stat("pl-stat-auto", "&#9881;", s.with_fixers || 0, "with fix chains") +
          stat("pl-stat-total", "&#8645;", ins.cdm || "?", ins.profile === "legacy_ovid" ? "CDM · legacy port" : "CDM") +
          (ins.requires_family ? stat("pl-stat-auto", "&#9889;", ins.requires_family, "engine family required") : "");
        const cdmSel = document.getElementById("cdm");
        if (cdmSel && cdmSel.options[0]) cdmSel.options[0].textContent = "(auto — " + (ins.cdm || "from the template") + ")";
      }
    }
    this.renderTables();
  }

  pickedTables() {
    const table = this.form.querySelector("[data-provides-tables]");
    if (!table) return [];
    return Array.from(table.querySelectorAll('input[name="etl_tables"]:checked')).map((i) => i.value);
  }

  renderTables() {
    const table = this.form.querySelector("[data-provides-tables]");
    if (!table) return;
    const tbody = table.querySelector("tbody");
    const ins = this.inspection;
    if (!ins || ins.error || !ins.tables) {
      tbody.innerHTML = '<tr><td colspan="7" class="note">Pick a template to see what it provides.</td></tr>';
      return;
    }
    const pick = this.value("etl_tables_mode") === "pick";
    let initial;
    try { initial = new Set(JSON.parse(table.dataset.picked || "[]")); } catch (_e) { initial = new Set(); }
    const previous = new Set(this.pickedTables());
    const chosen = previous.size ? previous : initial;
    const dot = (cls, text) => '<span class="nw-stage ' + cls + '">' + esc(text) + "</span>";
    const fmt = (t) => t.formatter === "yaml" ? dot("is-yaml", t.adapter || "yaml")
      : t.formatter === "custom" || t.formatter === "python" ? dot("is-custom", "custom")
      : dot("is-none", "—");
    const mapper = (t) => t.mapper === "yaml" ? dot("is-yaml", (t.mapper_ops || []).join(", ") || "yaml")
      : t.mapper === "cdm_default" ? dot("is-default", "CDM default")
      : t.mapper === "legacy_class" ? dot("is-default", "legacy class") : dot("is-none", "—");
    const list = (items, cls) => items && items.length
      ? items.map((x) => '<span class="nw-chip ' + cls + '">' + esc(x) + "</span>").join("") : '<span class="nw-muted">—</span>';
    tbody.innerHTML = ins.tables.map((t) => {
      const checked = t.configured && (!pick || !chosen.size || chosen.has(t.name));
      return '<tr class="' + (t.configured ? "" : "is-unconfigured") + (t.requires_family ? " is-gated" : "") + '">' +
        '<td class="nw-col-pick">' + (t.configured
          ? '<input type="checkbox" name="etl_tables" value="' + esc(t.name) + '"' + (checked ? " checked" : "") +
            (pick ? "" : " disabled") + ' aria-label="Run ' + esc(t.name) + '">'
          : "") + "</td>" +
        '<td class="mono"><strong>' + esc(t.name) + "</strong>" +
          (t.is_reference ? ' <span class="nw-chip is-ref" title="Reference table — other tables\' fixers read it">ref</span>' : "") +
          (t.requires_family ? ' <span class="nw-chip is-warn" title="Custom formatter requires a ' + esc(t.requires_family) + ' engine">' + esc(t.requires_family) + "</span>" : "") +
        "</td>" +
        "<td>" + fmt(t) + "</td>" +
        '<td class="mono small">' + list(t.inputs, "is-file") +
          (t.auxiliary_tables && t.auxiliary_tables.length ? '<div class="nw-aux small">+ aux: ' + t.auxiliary_tables.map(esc).join(", ") + "</div>" : "") + "</td>" +
        "<td>" + mapper(t) +
          (t.dictionaries && t.dictionaries.length ? '<div class="nw-aux small" title="' + esc(t.dictionaries.join(", ")) + '">' + t.dictionaries.length + " dictionaries</div>" : "") + "</td>" +
        '<td class="small">' + list(t.fixers, "is-fix") + "</td>" +
        '<td class="small">' + list(t.transforms, "is-tx") + "</td></tr>";
    }).join("");
    tbody.querySelectorAll('input[name="etl_tables"]').forEach((i) => i.addEventListener("change", () => this.render()));
  }

  renderIdentity() {
    const host = document.getElementById("identity-fields");
    const list = document.getElementById("identity-bindings");
    if (!host) return;
    const ins = this.inspection;
    if (!ins || ins.error || !ins.identity) {
      host.innerHTML = '<p class="note">' + (this.kind() === "etl" ? "Pick a template to see the identity fields it declares." : "Identity applies to ETL pipelines only.") + "</p>";
      if (list) list.innerHTML = "";
      return;
    }
    // Keep what the operator typed across re-inspections (name changes).
    const typed = {};
    host.querySelectorAll("[data-identity-key]").forEach((i) => {
      if (i.dataset.dirty === "1") typed[i.dataset.identityKey] = i.value;
    });
    const groups = {};
    for (const row of ins.identity) {
      if (row.key === "cdm") continue;
      (groups[row.category] = groups[row.category] || []).push(row);
    }
    const labels = { harvest: "HARVEST network / datamart", warehouse: "Warehouse", datavant: "Datavant", omop_source: "OMOP cdm_source" };
    host.innerHTML = Object.entries(groups).map(([cat, rows]) =>
      '<fieldset class="nw-id-group"><legend>' + esc(labels[cat] || cat) + "</legend>" +
      rows.map((row) => {
        const value = typed[row.key] != null ? typed[row.key]
          : this.prefill[row.key] != null ? this.prefill[row.key]
          : row.default == null ? "" : row.default;
        const input = row.choices && row.choices.length
          ? '<select name="identity__' + esc(row.key) + '" data-identity-key="' + esc(row.key) + '">' +
            row.choices.map((c) => '<option value="' + esc(c) + '"' + (String(value) === c ? " selected" : "") + ">" + esc(c) + "</option>").join("") + "</select>"
          : '<input type="text" name="identity__' + esc(row.key) + '" data-identity-key="' + esc(row.key) + '" value="' + esc(value) + '" autocomplete="off">';
        const changed = row.current != null && String(row.current) !== String(value);
        return '<div class="form-row nw-id-row"><label>' + esc(row.label) +
          ' <code class="nw-id-key">' + esc(row.key) + "</code></label>" + input +
          '<p class="nw-id-meta small"><span class="nw-muted">must match:</span> ' + esc(row.must_match) +
          (changed ? ' <span class="nw-was">template: <code>' + esc(row.current) + "</code></span>" : "") + "</p></div>";
      }).join("") + "</fieldset>"
    ).join("") || '<p class="note">This template declares no identity constants.</p>';
    host.querySelectorAll("[data-identity-key]").forEach((i) => {
      i.addEventListener("input", () => { i.dataset.dirty = "1"; this.render(); });
      i.addEventListener("change", () => { i.dataset.dirty = "1"; this.render(); });
    });
    if (list) list.innerHTML = this.bindingsHtml(ins.bindings || [], false);
  }

  bindingsHtml(bindings, compact) {
    const order = { error: 0, warn: 1, info: 2, ok: 3 };
    const rows = [...bindings].filter((b) => !b.id.startsWith("identity.") || b.status !== "ok")
      .sort((a, b) => (order[a.status] - order[b.status]) || a.category.localeCompare(b.category));
    if (!rows.length) return '<li class="nw-binding is-ok">Nothing else to match.</li>';
    return rows.filter((b) => !compact || b.status !== "ok").map((b) =>
      '<li class="nw-binding is-' + esc(b.status) + '">' +
      '<span class="nw-binding-dot" aria-label="' + esc(b.status) + '"></span>' +
      '<span class="nw-binding-body"><span class="nw-binding-label">' + esc(b.label) +
      ' <code>' + esc(b.value) + "</code></span>" +
      '<span class="nw-binding-meta small">' + esc(b.must_match) + (b.detail ? " — <strong>" + esc(b.detail) + "</strong>" : "") +
      (b.where ? ' <span class="nw-muted">(' + esc(b.where) + ")</span>" : "") + "</span></span></li>"
    ).join("") || '<li class="nw-binding is-ok">All names match.</li>';
  }

  /* ---------------- source-file match preview ---------------- */

  async refreshSourceMatch() {
    const box = document.getElementById("source-match");
    if (!box) return;
    const ins = this.inspection;
    const conn = this.value("input_connection");
    if (this.kind() !== "etl" || !ins || !ins.tables || this.isOpen("input")) { box.hidden = true; return; }
    const tables = ins.tables.filter((t) => t.configured && t.inputs && t.inputs.length);
    if (!tables.length) { box.hidden = true; return; }
    box.hidden = false;
    const loc = this.value("input_location");
    const key = conn + "|" + loc;
    if (conn && key !== this.listing.key) {
      this.listing = { key, names: null };
      try {
        const qs = loc ? "?prefix=" + encodeURIComponent(loc) : "";
        const r = await fetch("/api/v1/storage/" + encodeURIComponent(conn) + "/browse" + qs,
          { credentials: "same-origin", headers: { Accept: "application/json" } });
        const body = r.ok ? await r.json() : { entries: [] };
        if (this.listing.key === key) {
          this.listing.names = (body.entries || []).map((e) => e.name);
          this.listing.dirs = (body.entries || []).filter((e) => e.kind === "dir").length;
        }
      } catch (_e) { this.listing.names = []; }
    }
    const names = conn ? this.listing.names : null;
    const ul = box.querySelector("[data-match-list]");
    ul.innerHTML = tables.map((t) => {
      const pats = t.inputs;
      let state = "info", note = "choose an input folder to check";
      if (names) {
        const hit = names.filter((n) => pats.some((p) => globRe(p).test(n)));
        if (hit.length) { state = "ok"; note = hit.length + " match" + (hit.length > 1 ? "es" : "") + ": " + hit.slice(0, 2).join(", "); }
        else if (this.listing.dirs) { state = "info"; note = "no match here — expected inside each batch folder"; }
        else { state = "warn"; note = "no matching file in this folder"; }
      }
      return '<li class="nw-binding is-' + state + '"><span class="nw-binding-dot"></span><span class="nw-binding-body">' +
        '<span class="nw-binding-label mono">' + esc(t.name) + " ← " + pats.map((p) => "<code>" + esc(p) + "</code>").join(" ") + "</span>" +
        '<span class="nw-binding-meta small">' + esc(note) + "</span></span></li>";
    }).join("");
  }

  /* ---------------- requirements ---------------- */

  requirements() {
    const kind = this.kind();
    const reqs = [];
    const add = (step, text) => reqs.push({ step, text });
    const name = this.value("name");
    if (!name) add("type", "give the pipeline a name");
    else if (!/^[A-Za-z0-9._-]+$/.test(name)) add("type", "use only letters, numbers, . _ - in the name");
    else if (kind === "etl" && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
      add("type", "an ETL pipeline name must be a Python identifier (letters, digits, _)");
    }
    if (kind === "etl") {
      if (!this.form.querySelector('input[name="template"]:checked')) add("template", "pick an ETL template");
      if (this.value("etl_tables_mode") === "pick" && !this.pickedTables().length) add("template", "pick at least one table");
      const ins = this.inspection;
      if (ins && ins.bindings) {
        for (const b of ins.bindings) {
          if (b.status !== "error") continue;
          if (b.category === "engine") add("engine", b.label.toLowerCase() + ": " + b.detail);
          else if (!b.id.startsWith("identity.")) add("identity", b.label + ": " + b.detail);
        }
      }
      for (const i of this.form.querySelectorAll("[data-identity-key]")) {
        if (!String(i.value || "").trim()) add("identity", "fill in " + i.dataset.identityKey);
      }
    }
    if (kind === "analysis" && !this.value("package")) add("template", "pick a pipeline package to analyse");
    if (kind === "dq_checks" && !this.form.querySelector('input[name="packages"]:checked')) add("template", "pick at least one EDC report cycle");
    if ((kind === "copy" || kind === "move") && !this.isTablesOpen() && !this.value("tables")) {
      add("template", "list the tables to copy, or keep them open");
    }
    if (!this.isOpen("input") && !this.value("input_connection")) add("source", "choose input storage (or keep it open)");
    if (!this.value("engine")) add("engine", "choose an engine");
    if (!this.isOpen("output") && !this.value("output_connection")) add("destination", "choose output storage (or keep it open)");
    const trigger = document.getElementById("trigger_type");
    if (trigger && trigger.value === "watch" && this.isOpen("input")) {
      add("automation", "watch needs a bound input — bind the input or pick another trigger");
    }
    return reqs;
  }

  /* ---------------- wizard ---------------- */

  setupWizard() {
    this.allSteps = Array.from(this.form.querySelectorAll(".wz-step[data-step]"));
    this.visited = new Set();
    this.nav = this.form.querySelector("[data-wz-nav]");
    if (!this.allSteps.length) return;
    this.root.classList.add("wz-js");
    if (this.nav) this.nav.hidden = false;
    const tiles = this.form.querySelector("[data-kind-tiles]");
    if (tiles) tiles.hidden = false;
    this.form.addEventListener("click", (evt) => {
      const t = evt.target.closest ? evt.target : null;
      if (!t) return;
      const go = t.closest("[data-goto]");
      if (go) { this.goTo(go.dataset.goto); return; }
      if (t.closest("[data-wz-prev]")) { this.step(-1); return; }
      if (t.closest("[data-wz-next]")) { this.step(1); return; }
      const tile = t.closest("[data-kind-tile]");
      if (tile) this.pickKind(tile.dataset.kindTile);
    });
    if (tiles) {
      tiles.addEventListener("keydown", (evt) => {
        if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(evt.key)) return;
        const all = Array.from(tiles.querySelectorAll("[data-kind-tile]"));
        const i = all.indexOf(document.activeElement);
        if (i < 0) return;
        evt.preventDefault();
        const next = all[(i + (evt.key === "ArrowRight" || evt.key === "ArrowDown" ? 1 : -1) + all.length) % all.length];
        next.focus();
        this.pickKind(next.dataset.kindTile);
      });
    }
    const reqs = this.requirements();
    const first = reqs.length && this.form.querySelector(".flash.error") ? reqs[0].step : "type";
    this.goTo(first, { focus: false });
  }

  /* Steps applicable to the current kind (Identity is ETL-only). */
  stepKeys() {
    const kind = this.kind();
    return this.allSteps.filter((s) => !s.dataset.onlyKinds || s.dataset.onlyKinds.split(" ").includes(kind))
      .map((s) => s.dataset.step);
  }

  pickKind(kind) {
    const sel = document.getElementById("kind");
    if (!sel || sel.value === kind) { this.syncKindTiles(); return; }
    sel.value = kind;
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    this.syncKindTiles();
  }

  syncKindTiles() {
    const kind = this.kind();
    for (const tile of this.form.querySelectorAll("[data-kind-tile]")) {
      const on = tile.dataset.kindTile === kind;
      tile.setAttribute("aria-checked", on ? "true" : "false");
      tile.tabIndex = on ? 0 : -1;
    }
    const keys = this.stepKeys();
    for (const item of this.form.querySelectorAll("[data-rail]")) item.hidden = !keys.includes(item.dataset.rail);
    if (this.current && !keys.includes(this.current)) this.goTo(keys[0], { focus: false });
  }

  step(delta) {
    const keys = this.stepKeys();
    const i = keys.indexOf(this.current);
    this.goTo(keys[Math.min(Math.max(i + delta, 0), keys.length - 1)]);
  }

  goTo(key, opts) {
    const keys = this.stepKeys();
    if (!keys.includes(key)) return;
    if (this.current) this.visited.add(this.current);
    this.current = key;
    for (const s of this.allSteps) {
      const on = s.dataset.step === key;
      s.classList.toggle("is-current", on);
      s.setAttribute("aria-hidden", on ? "false" : "true");
    }
    const i = keys.indexOf(key);
    const last = i === keys.length - 1;
    if (this.nav) {
      this.nav.querySelector("[data-wz-prev]").disabled = i === 0;
      this.nav.querySelector("[data-wz-next]").hidden = last;
      this.nav.querySelector("[data-wz-create]").hidden = !last;
      const pos = this.nav.querySelector("[data-wz-pos]");
      if (pos) pos.textContent = "Step " + (i + 1) + " of " + keys.length;
    }
    let n = 0;
    for (const item of this.form.querySelectorAll("[data-rail]")) {
      if (!item.hidden) item.querySelector(".wz-rail-num").textContent = String(++n);
    }
    this.canvas.setAttribute("data-focus", key);
    if (key === "source") this.refreshSourceMatch();
    this.render();
    if (!opts || opts.focus !== false) {
      const h = this.allSteps.find((s) => s.dataset.step === key).querySelector("h2");
      if (h) { h.tabIndex = -1; h.focus({ preventScroll: true }); }
      const top = this.form.getBoundingClientRect().top + window.scrollY - 80;
      if (window.scrollY > top) window.scrollTo({ top, behavior: "smooth" });
    }
  }

  renderRail(reqs) {
    const pending = new Set(reqs.map((r) => r.step));
    for (const item of this.form.querySelectorAll("[data-rail]")) {
      const key = item.dataset.rail;
      let state = "done";
      if (key === this.current) state = "current";
      else if (pending.has(key)) state = this.visited.has(key) ? "attention" : "todo";
      else if (key === "review" && reqs.length) state = "todo";
      item.setAttribute("data-state", state);
      const btn = item.querySelector("button");
      if (btn) btn.setAttribute("aria-current", key === this.current ? "step" : "false");
    }
    if (this.nav) this.nav.querySelector("[data-wz-create]").disabled = reqs.length !== 0;
  }

  /* ---------------- engine gating ---------------- */

  renderEngines() {
    const required = this.kind() === "etl" && this.inspection ? this.inspection.requires_family : "";
    const hint = this.form.querySelector("[data-engine-hint]");
    for (const input of this.form.querySelectorAll('input[name="engine"]')) {
      const fam = input.getAttribute("data-family") || "";
      const bad = Boolean(required && fam && fam !== required);
      input.disabled = bad;
      const tile = input.closest(".nw-engine-tile");
      if (tile) {
        tile.classList.toggle("is-disabled", bad);
        tile.title = bad ? "This template requires a " + required + " engine." : "";
      }
      if (bad && input.checked) {
        input.checked = false;
        const ok = this.form.querySelector('input[name="engine"]:not(:disabled)');
        if (ok) { ok.checked = true; this.inspectSoon(); }
      }
    }
    if (hint) {
      hint.hidden = !required;
      hint.textContent = required ? "This template requires a " + required + "-family engine; other engines are disabled." : "";
    }
  }

  /* ---------------- review + canvas ---------------- */

  renderReview(reqs) {
    const dl = document.getElementById("review-list");
    if (!dl) return;
    const kind = this.kind();
    const ins = this.inspection;
    const rows = [["Name", this.value("name") || "—"], ["Type", kind]];
    if (kind === "etl") {
      const t = this.checkedRadio("template");
      rows.push(["Template", t ? t.value : "—"]);
      rows.push(["CDM", this.value("cdm") || (ins && ins.cdm) || "auto"]);
      const pick = this.value("etl_tables_mode") === "pick";
      rows.push(["Tables", pick ? this.pickedTables().join(", ") || "—" : "all configured (" + ((ins && ins.summary && ins.summary.configured) || "?") + ")"]);
      const ids = Array.from(this.form.querySelectorAll("[data-identity-key]")).map((i) => i.dataset.identityKey + " = " + i.value);
      if (ids.length) rows.push(["Identity", ids.join(" · ")]);
    } else if (kind === "analysis") rows.push(["Package", this.value("package") || "—"]);
    rows.push(["Input", this.isOpen("input") ? "open (choose at run time)" : [this.value("input_connection"), this.value("input_location")].filter(Boolean).join(" / ") || "—"]);
    rows.push(["Engine", this.value("engine") || "—"]);
    rows.push(["Output", this.isOpen("output") ? "open (choose at run time)" : [this.value("output_connection"), this.value("output_location")].filter(Boolean).join(" / ") || "—"]);
    const trig = document.getElementById("trigger_type");
    rows.push(["Trigger", trig ? trig.value : "—"]);
    dl.innerHTML = rows.map(([k, v]) => "<dt>" + esc(k) + '</dt><dd class="mono small">' + esc(v) + "</dd>").join("");
    const rb = document.getElementById("review-bindings");
    if (rb) {
      const issues = (ins && ins.bindings ? ins.bindings : []).filter((b) => b.status === "error" || b.status === "warn");
      rb.innerHTML = (reqs.length ? '<p class="flash error">Still needed: ' + reqs.map((r) => esc(r.text)).join("; ") + "</p>" : "") +
        (issues.length ? '<h3 class="wz-sub-title">Names to double-check</h3><ul class="nw-bindings">' + this.bindingsHtml(issues, true) + "</ul>" : "");
    }
  }

  renderCanvas(complete) {
    const kind = this.kind();
    const ins = this.inspection;
    const inOpen = this.isOpen("input");
    const outOpen = this.isOpen("output");
    const inConn = this.value("input_connection");
    const outConn = this.value("output_connection");
    const nodes = [{
      id: "source", title: "Source", logo: inOpen ? "folder" : STORAGE_LOGO(this.connType("input")),
      value: inOpen ? "open · run time" : inConn || "not set", sub: this.value("input_location"),
      state: inOpen || inConn ? "done" : "todo", active: this.current === "source",
    }];
    if (kind === "etl") {
      const s = ins && ins.summary ? ins.summary : null;
      const tmpl = this.value("template");
      const sel = this.value("etl_tables_mode") === "pick" ? this.pickedTables().length : s ? s.configured : 0;
      nodes.push({ id: "template", title: "Format", logo: "etl", value: tmpl || "no template",
        sub: s ? sel + " tables" : "", state: tmpl ? "done" : "todo", badge: s ? sel : "", active: this.current === "template" });
      nodes.push({ id: "template", title: "Transform", logo: "pipeline", value: s && s.with_transforms ? s.with_transforms + " tables" : "none assigned",
        sub: "UDF chains", state: tmpl ? (s && s.with_transforms ? "done" : "off") : "todo" });
      nodes.push({ id: "template", title: "Map", logo: "table", value: s ? s.with_mapper + " mappers" : "—",
        state: tmpl ? "done" : "todo" });
      nodes.push({ id: "identity", title: "Fix", logo: "check", value: s ? s.with_fixers + " chains" : "—",
        sub: ins && ins.cdm ? ins.cdm : "", state: tmpl ? (s && s.errors ? "error" : s && s.warnings ? "warn" : "done") : "todo",
        badge: s && s.errors ? "!" : "", active: this.current === "identity" });
    } else {
      nodes.push({ id: "template", title: kind, logo: kind, value: this.value("package") || this.value("tables") || kind,
        state: "done", active: this.current === "template" || this.current === "type" });
    }
    nodes.push({ id: "engine", title: "Engine", logo: "engine", value: this.value("engine") || "not set",
      sub: this.engineFamily(), state: this.value("engine") ? "done" : "todo", active: this.current === "engine" });
    nodes.push({ id: "destination", title: "Output", logo: outOpen ? "folder" : STORAGE_LOGO(this.connType("output")),
      value: outOpen ? "open · run time" : outConn || "not set", sub: this.value("output_location"),
      state: outOpen || outConn ? "done" : "todo", active: this.current === "destination" });
    this.graph.render({ nodes, caption: this.value("name") || "new pipeline" });

    const name = this.canvas.querySelector("[data-canvas-name]");
    if (name) name.textContent = this.value("name") || "new pipeline";
    const k = this.canvas.querySelector("[data-canvas-kind]");
    if (k) k.textContent = kind;
    const st = this.canvas.querySelector("[data-canvas-status]");
    if (st) {
      st.classList.toggle("is-on", complete);
      st.classList.toggle("is-off", !complete);
      st.querySelector("[data-canvas-status-text]").textContent = complete ? "ready" : "draft";
    }
    const chips = this.canvas.querySelector("[data-canvas-chips]");
    if (chips) {
      const trig = document.getElementById("trigger_type");
      const c = [["automation", "Trigger", trig ? trig.value : ""]];
      if (kind === "etl" && ins && ins.summary) {
        c.push(["identity", "Checks", ins.summary.errors ? ins.summary.errors + " errors" : ins.summary.warnings ? ins.summary.warnings + " warnings" : "ok"]);
      }
      chips.innerHTML = c.map(([slot, label, value]) =>
        '<button type="button" class="graph-chip' + (value === "ok" ? " is-custom" : /error/.test(value) ? " is-missing" : "") +
        '" data-goto="' + slot + '"><span class="graph-chip-label">' + esc(label) + '</span><span class="graph-chip-value">' + esc(value) + "</span></button>").join("");
      chips.querySelectorAll("[data-goto]").forEach((b) => b.addEventListener("click", () => this.goTo(b.dataset.goto)));
    }
  }

  render() {
    const reqs = this.requirements();
    const complete = reqs.length === 0;
    this.renderEngines();
    this.renderRail(reqs);
    this.renderCanvas(complete);
    this.renderReview(reqs);
    const hint = this.form.querySelector("[data-name-hint]");
    if (hint) {
      const n = this.value("name");
      hint.hidden = !(n && this.kind() === "etl");
      hint.innerHTML = n ? "Package <code>ovid2_pipelines." + esc(n) + "</code> · secrets <code>secrets.d/" + esc(n) + ".json</code>" : "";
    }
    this.canvas.classList.toggle("is-complete", complete);
    this.canvas.setAttribute("data-complete", complete ? "true" : "false");
    if (this.saveBtn) this.saveBtn.disabled = !complete;
    if (this.reqList) {
      this.reqList.replaceChildren();
      if (complete) {
        const li = document.createElement("li");
        li.className = "builder-req-done small";
        li.textContent = "All set — you can create this pipeline.";
        this.reqList.appendChild(li);
      } else {
        for (const r of reqs) {
          const li = document.createElement("li");
          li.className = "builder-req small";
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "builder-req-link";
          btn.textContent = r.text;
          btn.addEventListener("click", () => this.goTo(r.step));
          li.appendChild(btn);
          this.reqList.appendChild(li);
        }
      }
    }
  }
}

function boot() {
  const root = document.getElementById("pipeline-builder");
  if (root) new PipelineBuilder(root);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
