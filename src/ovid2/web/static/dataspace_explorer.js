/*
 * Ovid2 Data Space explorer (templates/dataspace.html).
 *
 * Left pane: a lazy, collapsible tree of every storage account the user
 * may see, grouped by storage type (collapsible groups) or flat, sorted
 * by name A→Z / Z→A. Accounts, folders, databases and schemas expand on
 * demand via GET /api/v1/dataspace?path=…  Icons are the submit page's
 * storage-type marks (logos.js) so a type reads the same everywhere.
 *
 * Right pane: the selected node —
 *   - root            → storage-type overview + account cards
 *   - account/folder  → sortable listing (name / kind / size / rows / modified)
 *   - table / file    → detail: stats (name, rows, fields, size, last
 *                       modified…), Fields / Sample / Definition tabs and
 *                       an Export dialog (CSV with delimiter + header case,
 *                       Excel, PDF, Word; whole table as a background job).
 *
 * The URL tracks the selection (/dataspace/<account>/<path…>) with
 * history.pushState, so every view is deep-linkable and Back works.
 * Grouping / sort / open groups persist in localStorage.
 *
 * Dependency-free ES module.
 */

import { logoSvg, logoForType } from "/static/logos.js";

const WAREHOUSE_TYPES = new Set(["snowflake", "databricks", "postgres", "mssql"]);
const TYPE_LABELS = {
  disc: "Local disc",
  s3: "Amazon S3",
  azure_blob: "Azure Blob",
  gcs: "Google Cloud Storage",
  snowflake: "Snowflake",
  databricks: "Databricks",
  postgres: "PostgreSQL",
  mssql: "SQL Server",
  access: "MS Access",
};
const DATA_DIR_RE = /\.(csv|tsv|txt|parquet|json|jsonl)(\.gz)?$/i;
const PREFS_KEY = "ovid2.dataspace.prefs";

// ------------------------------------------------------------------ utils

function esc(v) {
  return String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function typeLabel(t) {
  return TYPE_LABELS[String(t || "").toLowerCase()] || String(t || "unknown");
}

function fmtBytes(n) {
  if (n == null || n === "") return "";
  const v = Number(n);
  if (!isFinite(v)) return "";
  if (v < 1024) return v + " B";
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let x = v / 1024, i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return x.toFixed(x < 10 ? 1 : 0) + " " + units[i];
}

function fmtInt(n) {
  if (n == null || n === "") return "";
  return Number(n).toLocaleString();
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function parentPath(p) {
  const parts = p.split("/").filter(Boolean);
  parts.pop();
  return "/" + parts.join("/");
}

function depthOf(p) { return p.split("/").filter(Boolean).length; }

async function getJson(url) {
  const r = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
  let body = null;
  try { body = await r.json(); } catch (_e) { body = null; }
  if (!r.ok) {
    const d = body && body.detail;
    throw new Error(typeof d === "string" ? d : (d && d.message) || "HTTP " + r.status);
  }
  return body;
}

function nodeLogo(entry, type) {
  const t = String(type || "").toLowerCase();
  if (entry.kind === "account") return logoForType(t);
  if (isTableEntry(entry)) return "table";
  if (entry.kind === "dir") {
    if (t === "access" && /\.(accdb|mdb)$/i.test(entry.name)) return "access";
    return WAREHOUSE_TYPES.has(t) ? "schema" : "folder";
  }
  return "file";
}

/* A leaf the detail page can open as a table. */
function isTableEntry(e) {
  if (e.kind === "table" || e.kind === "file") return true;
  return e.kind === "dir" && DATA_DIR_RE.test(e.name || "");
}

// ------------------------------------------------------------- explorer

class DataSpaceExplorer {
  constructor(boot) {
    this.boot = boot;
    this.accounts = boot.accounts || [];
    this.limits = boot.export_limits || {};
    this.canSubmit = !!boot.can_submit;
    this.tree = document.querySelector("[data-ds-tree]");
    this.main = document.querySelector("[data-ds-main]");
    this.crumbs = document.querySelector("[data-role=ds-crumbs]");
    this.listings = new Map(); // path -> listing body
    this.entries = new Map();  // path -> entry (from a parent listing)
    this.current = null;
    this.detail = null;
    this.listSort = { key: "name", dir: 1 };
    this.prefs = this.loadPrefs();
    for (const a of this.accounts) this.entries.set(a.path.toLowerCase(), { ...a, kind: "account" });
  }

  // ---------------------------------------------------------- prefs

  loadPrefs() {
    const d = { group: "type", sort: "asc", open: [] };
    try {
      const raw = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
      return { ...d, ...raw, open: Array.isArray(raw.open) ? raw.open : [] };
    } catch (_e) { return d; }
  }

  savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(this.prefs)); } catch (_e) { /* private mode */ }
  }

  // ----------------------------------------------------------- init

  init() {
    this.bindToolbar();
    this.renderTree();
    this.tree.addEventListener("click", (ev) => this.onTreeClick(ev));
    this.tree.addEventListener("keydown", (ev) => this.onTreeKey(ev));
    this.main.addEventListener("click", (ev) => this.onMainClick(ev));
    document.addEventListener("click", (ev) => {
      const a = ev.target.closest("a[data-ds-nav]");
      if (!a || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button !== 0) return;
      ev.preventDefault();
      this.navigate(a.getAttribute("data-ds-nav"));
    });
    window.addEventListener("popstate", (ev) => {
      const p = (ev.state && ev.state.dsPath) || this.pathFromLocation();
      this.navigate(p, { push: false });
    });
    this.initExportModal();
    this.navigate(this.boot.path || "/", { push: false, replace: true });
  }

  pathFromLocation() {
    const rest = decodeURIComponent(location.pathname.replace(/^\/dataspace/, ""));
    return rest && rest !== "/" ? rest.replace(/\/+$/, "") : "/";
  }

  bindToolbar() {
    for (const b of document.querySelectorAll("[data-ds-group]")) {
      b.setAttribute("aria-pressed", String(b.dataset.dsGroup === this.prefs.group));
      b.addEventListener("click", () => {
        this.prefs.group = b.dataset.dsGroup;
        this.savePrefs();
        for (const o of document.querySelectorAll("[data-ds-group]")) {
          o.setAttribute("aria-pressed", String(o === b));
        }
        this.renderTree();
        if (this.current === "/") this.renderRoot();
      });
    }
    const sortBtn = document.querySelector("[data-ds-sort]");
    const sortLabel = document.querySelector("[data-role=ds-sort-label]");
    const paintSort = () => { sortLabel.textContent = this.prefs.sort === "asc" ? "A→Z" : "Z→A"; };
    paintSort();
    sortBtn.addEventListener("click", () => {
      this.prefs.sort = this.prefs.sort === "asc" ? "desc" : "asc";
      this.savePrefs();
      paintSort();
      this.renderTree();
      if (this.current === "/") this.renderRoot();
    });
    document.querySelector("[data-ds-expand-all]").addEventListener("click", () => {
      for (const li of this.tree.querySelectorAll(":scope > li.tree-group-node")) this.setOpen(li, true);
    });
    document.querySelector("[data-ds-collapse-all]").addEventListener("click", () => {
      for (const li of this.tree.querySelectorAll("li[aria-expanded=true]")) this.setOpen(li, false);
    });
    const filter = document.querySelector("[data-ds-filter]");
    filter.addEventListener("input", () => this.applyFilter(filter.value));
  }

  sortedAccounts() {
    const dir = this.prefs.sort === "asc" ? 1 : -1;
    return [...this.accounts].sort((a, b) => dir * a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
  }

  groupedAccounts() {
    const groups = new Map();
    for (const a of this.sortedAccounts()) {
      const t = String(a.type || "unknown").toLowerCase();
      if (!groups.has(t)) groups.set(t, []);
      groups.get(t).push(a);
    }
    const dir = this.prefs.sort === "asc" ? 1 : -1;
    return [...groups.entries()].sort((x, y) => dir * typeLabel(x[0]).localeCompare(typeLabel(y[0])));
  }

  // ----------------------------------------------------------- tree

  itemHtml(n) {
    const expandable = !!n.expandable;
    return (
      '<li role="treeitem" class="tree-item ds-item tree-' + esc(n.kind) +
      (n.extraClass ? " " + n.extraClass : "") + '"' +
      ' data-kind="' + esc(n.kind) + '"' +
      ' data-path="' + esc(n.path || "") + '"' +
      ' data-type="' + esc(n.type || "") + '"' +
      ' data-name="' + esc(n.name) + '"' +
      (expandable ? ' aria-expanded="false"' : "") +
      ' aria-selected="false" tabindex="-1">' +
      '<div class="tree-row">' +
      (expandable
        ? '<span class="tree-chevron" data-ds-toggle aria-hidden="true">' + logoSvg("chevron", 14) + "</span>"
        : '<span class="tree-chevron tree-chevron-spacer" aria-hidden="true"></span>') +
      '<span class="ds-node-icon" aria-hidden="true">' + logoSvg(n.logo, 18) + "</span>" +
      '<button type="button" class="tree-label" data-ds-open title="' + esc(n.title || n.name) + '">' +
      esc(n.name) + "</button>" +
      (n.count != null ? '<span class="tree-count">' + esc(n.count) + "</span>" : "") +
      (n.badge ? '<span class="tree-type">' + esc(n.badge) + "</span>" : "") +
      "</div>" +
      (expandable ? '<ul class="tree-group" role="group" hidden></ul>' : "") +
      "</li>"
    );
  }

  accountItem(a, showType) {
    return this.itemHtml({
      kind: "account", path: a.path, type: a.type, name: a.name,
      logo: logoForType(a.type), expandable: true,
      badge: showType ? typeLabel(a.type) : "",
      title: a.name + " — " + typeLabel(a.type),
    });
  }

  renderTree() {
    const html = [];
    if (!this.accounts.length) {
      html.push('<li class="tree-msg">No storage accounts are configured. Add one under Storage accounts.</li>');
    } else if (this.prefs.group === "type") {
      for (const [t, list] of this.groupedAccounts()) {
        html.push(
          '<li role="treeitem" class="tree-item tree-group-node" data-kind="group" data-group="' + esc(t) +
          '" aria-expanded="false" tabindex="-1">' +
          '<div class="tree-row ds-group-row">' +
          '<span class="tree-chevron" data-ds-toggle aria-hidden="true">' + logoSvg("chevron", 14) + "</span>" +
          '<span class="ds-node-icon" aria-hidden="true">' + logoSvg(logoForType(t), 18) + "</span>" +
          '<button type="button" class="tree-label ds-group-label" data-ds-toggle>' + esc(typeLabel(t)) + "</button>" +
          '<span class="tree-count">' + list.length + "</span>" +
          "</div>" +
          '<ul class="tree-group" role="group" hidden>' +
          list.map((a) => this.accountItem(a, false)).join("") +
          "</ul></li>"
        );
      }
    } else {
      for (const a of this.sortedAccounts()) html.push(this.accountItem(a, true));
    }
    this.tree.innerHTML = html.join("");
    if (this.prefs.group === "type") {
      const open = new Set(this.prefs.open);
      const first = !localStorage.getItem(PREFS_KEY);
      for (const li of this.tree.querySelectorAll(":scope > li.tree-group-node")) {
        if (first || open.has(li.dataset.group)) this.setOpen(li, true, { persist: false });
      }
    }
    const filter = document.querySelector("[data-ds-filter]");
    if (filter && filter.value) this.applyFilter(filter.value);
    if (this.current) this.revealPath(this.current, { load: false });
  }

  childGroup(li) { return li.querySelector(":scope > ul.tree-group"); }

  async setOpen(li, open, opts = {}) {
    const group = this.childGroup(li);
    if (!group) return;
    li.setAttribute("aria-expanded", String(open));
    group.hidden = !open;
    if (li.dataset.kind === "group" && opts.persist !== false) {
      const set = new Set(this.prefs.open);
      if (open) set.add(li.dataset.group); else set.delete(li.dataset.group);
      this.prefs.open = [...set];
      this.savePrefs();
    }
    if (open && li.dataset.kind !== "group" && li.dataset.loaded !== "1") {
      await this.loadChildren(li);
    }
  }

  async loadChildren(li) {
    if (li.dataset.loaded === "loading") return li._loading;
    const group = this.childGroup(li);
    li.dataset.loaded = "loading";
    group.innerHTML = '<li class="tree-msg">Loading…</li>';
    li._loading = (async () => {
      try {
        const body = await this.listing(li.dataset.path);
        group.innerHTML = this.childrenHtml(body);
        li.dataset.loaded = "1";
      } catch (err) {
        li.dataset.loaded = "0";
        group.innerHTML = '<li class="tree-msg tree-err">' + esc(err.message) + "</li>";
      }
    })();
    return li._loading;
  }

  childrenHtml(body) {
    if (body.supported === false) {
      return '<li class="tree-msg">' + esc(body.message || "Browsing is not available for this storage type.") + "</li>";
    }
    const entries = body.entries || [];
    if (!entries.length) return '<li class="tree-msg">' + esc(body.message || "(empty)") + "</li>";
    const type = body.type;
    const sorted = [...entries].sort((a, b) => {
      const ka = isTableEntry(a) ? 1 : 0, kb = isTableEntry(b) ? 1 : 0;
      return ka - kb || a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
    });
    return sorted.map((e) => this.itemHtml({
      kind: isTableEntry(e) ? "table" : "dir",
      path: e.path, type, name: e.name,
      logo: nodeLogo(e, type),
      expandable: !isTableEntry(e),
      title: e.name,
    })).join("");
  }

  async listing(path, { refresh = false } = {}) {
    const key = path.toLowerCase();
    if (!refresh && this.listings.has(key)) return this.listings.get(key);
    const body = await getJson("/api/v1/dataspace?path=" + encodeURIComponent(path));
    this.listings.set(key, body);
    for (const e of body.entries || []) this.entries.set(e.path.toLowerCase(), e);
    return body;
  }

  findItem(path) {
    const want = path.toLowerCase();
    for (const li of this.tree.querySelectorAll("li[data-path]")) {
      if ((li.dataset.path || "").toLowerCase() === want) return li;
    }
    return null;
  }

  /* Expand the tree down to ``path`` (lazy-loading each level) and mark it. */
  async revealPath(path, { load = true } = {}) {
    const parts = path.split("/").filter(Boolean);
    if (parts.length) {
      const acct = this.accounts.find((a) => a.name.toLowerCase() === parts[0].toLowerCase());
      if (acct && this.prefs.group === "type") {
        const g = this.tree.querySelector('li.tree-group-node[data-group="' + CSS.escape(String(acct.type || "unknown").toLowerCase()) + '"]');
        if (g && g.getAttribute("aria-expanded") !== "true") await this.setOpen(g, true);
      }
      let acc = "";
      for (let i = 0; i < parts.length - 1; i++) {
        acc += "/" + parts[i];
        const li = this.findItem(acc);
        if (!li) break;
        if (li.getAttribute("aria-expanded") !== "true") {
          if (!load && li.dataset.loaded !== "1") break;
          await this.setOpen(li, true);
        }
      }
    }
    this.markSelected(path);
  }

  markSelected(path) {
    const want = (path || "").toLowerCase();
    let hit = null;
    for (const li of this.tree.querySelectorAll("li[data-path]")) {
      const on = (li.dataset.path || "").toLowerCase() === want;
      li.classList.toggle("is-selected", on);
      li.setAttribute("aria-selected", String(on));
      if (on) hit = li;
    }
    if (hit) {
      hit.tabIndex = 0;
      const row = hit.querySelector(":scope > .tree-row");
      if (row && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    }
  }

  onTreeClick(ev) {
    const li = ev.target.closest("li.tree-item");
    if (!li || !this.tree.contains(li)) return;
    if (ev.target.closest("[data-ds-toggle]")) {
      this.setOpen(li, li.getAttribute("aria-expanded") !== "true");
      return;
    }
    if (ev.target.closest("[data-ds-open]")) {
      if (li.dataset.kind !== "table" && li.getAttribute("aria-expanded") !== "true") this.setOpen(li, true);
      this.navigate(li.dataset.path);
    }
  }

  onTreeKey(ev) {
    const li = ev.target.closest("li.tree-item");
    if (!li) return;
    const visible = [...this.tree.querySelectorAll("li.tree-item")].filter((x) => x.offsetParent !== null);
    const i = visible.indexOf(li);
    const focus = (x) => { if (x) { const b = x.querySelector(":scope > .tree-row .tree-label"); (b || x).focus(); } };
    if (ev.key === "ArrowDown") { ev.preventDefault(); focus(visible[i + 1]); }
    else if (ev.key === "ArrowUp") { ev.preventDefault(); focus(visible[i - 1]); }
    else if (ev.key === "ArrowRight" && li.hasAttribute("aria-expanded")) {
      ev.preventDefault();
      if (li.getAttribute("aria-expanded") !== "true") this.setOpen(li, true); else focus(visible[i + 1]);
    } else if (ev.key === "ArrowLeft") {
      ev.preventDefault();
      if (li.getAttribute("aria-expanded") === "true") this.setOpen(li, false);
      else focus(li.parentElement.closest("li.tree-item"));
    }
  }

  applyFilter(text) {
    const q = String(text || "").trim().toLowerCase();
    const items = [...this.tree.querySelectorAll("li.tree-item")].reverse();
    for (const li of items) {
      if (!q) { li.hidden = false; continue; }
      const own = (li.dataset.name || li.textContent || "").toLowerCase().includes(q) ||
        (li.dataset.kind === "group" && typeLabel(li.dataset.group).toLowerCase().includes(q));
      const kids = [...li.querySelectorAll(":scope > ul > li.tree-item")].some((c) => !c.hidden);
      li.hidden = !(own || kids);
      if (q && kids && li.dataset.kind === "group") {
        li.setAttribute("aria-expanded", "true");
        this.childGroup(li).hidden = false;
      }
    }
  }

  // ----------------------------------------------------- navigation

  async navigate(path, { push = true, replace = false } = {}) {
    path = path && path !== "" ? path : "/";
    this.current = path;
    const url = "/dataspace" + (path === "/" ? "" : path.split("/").map(encodeURIComponent).join("/"));
    if (replace) history.replaceState({ dsPath: path }, "", url);
    else if (push && location.pathname !== url) history.pushState({ dsPath: path }, "", url);
    this.renderCrumbs(path);
    document.title = "Data Space · " + path;
    this.revealPath(path);
    if (path === "/") return this.renderRoot();
    const entry = await this.entryFor(path);
    if (this.current !== path) return undefined;
    if (entry && isTableEntry(entry)) return this.renderTable(path, entry);
    return this.renderFolder(path);
  }

  async entryFor(path) {
    const key = path.toLowerCase();
    if (this.entries.has(key)) return this.entries.get(key);
    if (depthOf(path) < 2) return null;
    try {
      await this.listing(parentPath(path));
    } catch (_e) {
      return null;
    }
    return this.entries.get(key) || null;
  }

  renderCrumbs(path) {
    const parts = path.split("/").filter(Boolean);
    let acc = "";
    let html = '<a href="/dataspace" data-ds-nav="/">Data Space</a>';
    for (const p of parts) {
      acc += "/" + p;
      html += '<span class="ds-crumb-sep">/</span><a href="/dataspace' + esc(acc) + '" data-ds-nav="' + esc(acc) + '">' + esc(p) + "</a>";
    }
    this.crumbs.innerHTML = html;
  }

  // ----------------------------------------------------- root view

  renderRoot() {
    const groups = this.groupedAccounts();
    const total = this.accounts.length;
    const typeCards = groups.map(([t, list]) =>
      '<div class="ds-type-card">' +
      '<span class="ds-type-icon">' + logoSvg(logoForType(t), 28) + "</span>" +
      '<div><div class="ds-type-name">' + esc(typeLabel(t)) + '</div><div class="muted small">' +
      list.length + " account" + (list.length === 1 ? "" : "s") + "</div></div></div>"
    ).join("");
    const card = (a) =>
      '<a class="ds-account-card" href="/dataspace' + esc(a.path) + '" data-ds-nav="' + esc(a.path) + '">' +
      '<span class="ds-type-icon">' + logoSvg(logoForType(a.type), 22) + "</span>" +
      '<span class="ds-account-body"><span class="ds-account-name mono">' + esc(a.name) + "</span>" +
      '<span class="muted small">' + esc(typeLabel(a.type)) + "</span></span></a>";
    let accountsHtml;
    if (this.prefs.group === "type") {
      accountsHtml = groups.map(([t, list]) =>
        '<details class="ds-root-group" open><summary>' + logoSvg(logoForType(t), 16) + " " +
        esc(typeLabel(t)) + ' <span class="tree-count">' + list.length + "</span></summary>" +
        '<div class="ds-account-grid">' + list.map(card).join("") + "</div></details>"
      ).join("");
    } else {
      accountsHtml = '<div class="ds-account-grid">' + this.sortedAccounts().map(card).join("") + "</div>";
    }
    this.main.innerHTML =
      '<div class="card">' +
      '<h2>Storage accounts <span class="tree-count">' + total + "</span></h2>" +
      '<p class="muted small">Every storage account is a tree of folders — disc, object stores and ' +
      "warehouses alike. Open an account to browse its folders or schemas; open a table to see " +
      "its fields, row count, a sample of its data and to export it. A folder's full path is the " +
      "batch id of a run that reads it.</p>" +
      '<div class="ds-type-grid">' + typeCards + "</div></div>" +
      '<div class="card">' + (accountsHtml || '<p class="muted">No storage accounts yet.</p>') + "</div>";
  }

  // --------------------------------------------------- folder view

  async renderFolder(path) {
    this.main.innerHTML = '<div class="card ds-loading">Loading ' + esc(path) + "…</div>";
    let body;
    try {
      body = await this.listing(path);
    } catch (err) {
      if (this.current !== path) return;
      this.main.innerHTML = '<div class="card"><p class="flash error">' + esc(err.message) + "</p></div>";
      return;
    }
    if (this.current !== path) return;
    this.folderBody = body;
    this.paintFolder();
  }

  paintFolder() {
    const body = this.folderBody;
    const type = String(body.type || "").toLowerCase();
    const isAccount = depthOf(body.path) === 1;
    const icon = isAccount ? logoForType(type) : (WAREHOUSE_TYPES.has(type) ? "schema" : "folder");
    const meta = [
      '<span class="ds-chip">' + logoSvg(logoForType(type), 14) + " " + esc(typeLabel(type)) + "</span>",
    ];
    if (body.location) meta.push('<span class="ds-chip mono">' + esc(body.location) + "</span>");
    if (body.batch_id) meta.push('<span class="ds-chip" title="Batch id of a run reading this location">batch <span class="mono">' + esc(body.batch_id) + "</span></span>");
    if (body.address_name) meta.push('<a class="ds-chip" href="/addresses/' + encodeURIComponent(body.address_name) + '">address <span class="mono">' + esc(body.address_name) + "</span></a>");

    let content;
    if (body.supported === false) {
      content = '<p class="muted">' + esc(body.message || "Browsing is not available for this storage type.") + "</p>";
    } else if (!(body.entries || []).length) {
      content = '<p class="muted">' + esc(body.message || "(empty)") + "</p>";
    } else {
      const { key, dir } = this.listSort;
      const val = (e) => key === "size" ? (e.size_bytes ?? -1)
        : key === "rows" ? (e.row_count ?? -1)
        : key === "modified" ? (e.modified_at || "")
        : key === "kind" ? (isTableEntry(e) ? "table" : e.kind) : e.name.toLowerCase();
      const rows = [...body.entries].sort((a, b) => {
        const va = val(a), vb = val(b);
        if (key === "name") {
          const da = isTableEntry(a) ? 1 : 0, db = isTableEntry(b) ? 1 : 0;
          if (da !== db) return da - db;
          return dir * a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });
        }
        return dir * (va < vb ? -1 : va > vb ? 1 : 0);
      });
      const anyRows = rows.some((e) => e.row_count != null);
      const th = (k, label, cls = "") => {
        const on = key === k;
        return '<th class="' + cls + '" aria-sort="' + (on ? (dir > 0 ? "ascending" : "descending") : "none") +
          '"><button type="button" class="ds-sort-btn" data-ds-sort-col="' + k + '">' + label +
          (on ? (dir > 0 ? " ▲" : " ▼") : "") + "</button></th>";
      };
      content =
        '<input type="search" class="ds-filter ds-list-filter" data-ds-list-filter placeholder="Filter this folder…" aria-label="Filter this folder">' +
        '<div class="table-wrap"><table class="data-table ds-list"><thead><tr>' +
        th("name", "Name") + th("kind", "Kind") + th("size", "Size", "num") +
        (anyRows ? th("rows", "Rows", "num") : "") + th("modified", "Last modified") +
        "</tr></thead><tbody>" +
        rows.map((e) => {
          const kind = isTableEntry(e) ? (e.kind === "table" ? "table" : "data file") : (WAREHOUSE_TYPES.has(type) ? "schema" : "folder");
          return '<tr data-ds-row data-name="' + esc(e.name.toLowerCase()) + '">' +
            '<td><a class="ds-row-link" href="/dataspace' + esc(e.path) + '" data-ds-nav="' + esc(e.path) + '">' +
            logoSvg(nodeLogo(e, type), 16) + ' <span class="mono">' + esc(e.name) + "</span></a></td>" +
            "<td>" + esc(kind) + "</td>" +
            '<td class="num">' + esc(fmtBytes(e.size_bytes)) + "</td>" +
            (anyRows ? '<td class="num">' + esc(fmtInt(e.row_count)) + "</td>" : "") +
            '<td class="small">' + esc(fmtDate(e.modified_at)) + "</td></tr>";
        }).join("") +
        "</tbody></table></div>";
    }
    const n = (body.entries || []).length;
    this.main.innerHTML =
      '<div class="card ds-detail-head">' +
      '<div class="ds-title-row"><span class="ds-title-icon">' + logoSvg(icon, 28) + "</span>" +
      '<div class="ds-title-text"><h2 class="mono">' + esc(body.path.split("/").filter(Boolean).pop() || "/") + "</h2>" +
      '<div class="ds-chips">' + meta.join("") + "</div></div>" +
      '<div class="ds-actions"><span class="muted small">' + n + " item" + (n === 1 ? "" : "s") + "</span>" +
      '<button type="button" class="button small" data-ds-refresh-list>Refresh</button></div></div></div>' +
      '<div class="card">' + content + "</div>";
  }

  // ---------------------------------------------------- table view

  async renderTable(path, entry) {
    const type = String(entry.type || "").toLowerCase();
    this.detail = { path, entry, info: null, sample: null, sampleLimit: 100, tab: "fields" };
    this.main.innerHTML =
      '<div class="card ds-detail-head"><div class="ds-title-row"><span class="ds-title-icon">' + logoSvg("table", 28) +
      '</span><div class="ds-title-text"><h2 class="mono">' + esc(entry.name) + '</h2><div class="ds-chips">' +
      '<span class="ds-chip">' + logoSvg(logoForType(type), 14) + " " + esc(typeLabel(type)) + "</span></div></div></div>" +
      '<div class="ds-loading muted">Reading table metadata…</div></div>';
    await this.loadTableInfo(false);
  }

  async loadTableInfo(refresh) {
    const d = this.detail;
    if (!d) return;
    const path = d.path;
    try {
      d.info = await getJson("/api/v1/dataspace/table?path=" + encodeURIComponent(path) + (refresh ? "&refresh=true" : ""));
    } catch (err) {
      if (this.current !== path) return;
      this.main.innerHTML = '<div class="card"><h2 class="mono">' + esc(d.entry.name) + '</h2><p class="flash error">' + esc(err.message) + "</p></div>";
      return;
    }
    if (this.current !== path) return;
    if (refresh) d.sample = null;
    this.paintTable();
  }

  paintTable() {
    const d = this.detail;
    const t = d.info;
    const type = String(t.type || "").toLowerCase();
    const isDb = !!t.ddl || WAREHOUSE_TYPES.has(type) || type === "access";
    const chips = [
      '<span class="ds-chip">' + logoSvg(logoForType(type), 14) + " " + esc(typeLabel(type)) + "</span>",
      '<span class="ds-chip">' + esc(t.kind === "view" ? "view" : t.format) + "</span>",
      '<span class="ds-chip mono" title="Data Space path">' + esc(t.path) + "</span>",
    ];
    if (!t.supported) {
      this.main.innerHTML =
        '<div class="card ds-detail-head"><div class="ds-title-row"><span class="ds-title-icon">' + logoSvg("table", 28) +
        '</span><div class="ds-title-text"><h2 class="mono">' + esc(t.name) + '</h2><div class="ds-chips">' + chips.join("") +
        '</div></div></div><p class="muted">' + esc(t.message || "Table details are not available for this storage type.") + "</p></div>";
      return;
    }
    const rows = t.row_count == null ? "—" : fmtInt(t.row_count);
    const estimate = t.row_count != null && !t.row_count_exact;
    const kpi = (label, value, extra = "") =>
      '<div class="kpi ds-kpi"><div class="kpi-value">' + value + '</div><div class="kpi-label">' + label + "</div>" + extra + "</div>";
    const countBtn = (!t.row_count_exact || t.row_count == null) && t.previewable
      ? '<button type="button" class="button small ds-count-btn" data-ds-count>Count rows</button>' : "";
    const kpis = [
      kpi("Rows" + (estimate ? ' <span class="badge status-queued">estimate</span>' : ""), esc(rows), countBtn),
      kpi("Fields", esc(fmtInt(t.columns.length))),
      kpi("Size", esc(fmtBytes(t.size_bytes) || "—")),
      kpi("Last modified", esc(fmtDate(t.modified_at) || "—")),
    ];
    if (t.created_at) kpis.push(kpi("Created", esc(fmtDate(t.created_at))));
    if (t.parts != null) kpis.push(kpi("Part files", esc(fmtInt(t.parts))));
    if (t.delimiter) kpis.push(kpi("Delimiter", '<span class="mono">' + esc(t.delimiter === "\t" ? "tab" : t.delimiter) + "</span>"));

    const tabs = [["fields", "Fields (" + t.columns.length + ")"]];
    if (t.previewable) tabs.push(["sample", "Sample data"]);
    if (isDb || t.ddl) tabs.push(["ddl", "Definition"]);
    if (!tabs.some(([k]) => k === d.tab)) d.tab = "fields";

    this.main.innerHTML =
      '<div class="card ds-detail-head">' +
      '<div class="ds-title-row"><span class="ds-title-icon">' + logoSvg("table", 28) + "</span>" +
      '<div class="ds-title-text"><h2 class="mono">' + esc(t.name) + '</h2><div class="ds-chips">' + chips.join("") + "</div></div>" +
      '<div class="ds-actions">' +
      '<button type="button" class="button small" data-ds-refresh-table>Refresh</button>' +
      (t.previewable ? '<button type="button" class="button primary small" data-ds-export>Export…</button>' : "") +
      "</div></div>" +
      (t.message ? '<p class="muted small ds-note">' + esc(t.message) + "</p>" : "") +
      '<div class="card-grid ds-kpis">' + kpis.join("") + "</div></div>" +
      '<div class="card"><div class="ds-tabs" role="tablist">' +
      tabs.map(([k, label]) => '<button type="button" role="tab" class="ds-tab" data-ds-tab="' + k +
        '" aria-selected="' + (k === d.tab) + '">' + esc(label) + "</button>").join("") +
      '</div><div class="ds-tab-body" data-ds-tab-body role="tabpanel"></div></div>';
    this.paintTab();
  }

  paintTab() {
    const d = this.detail;
    const t = d.info;
    const bodyEl = this.main.querySelector("[data-ds-tab-body]");
    if (!bodyEl) return;
    for (const b of this.main.querySelectorAll("[data-ds-tab]")) b.setAttribute("aria-selected", String(b.dataset.dsTab === d.tab));
    if (d.tab === "fields") {
      if (!t.columns.length) { bodyEl.innerHTML = '<p class="muted">No fields reported.</p>'; return; }
      const anyNull = t.columns.some((c) => c.nullable != null);
      bodyEl.innerHTML =
        '<div class="table-wrap"><table class="data-table ds-fields"><thead><tr><th class="num">#</th><th>Field</th><th>Type</th>' +
        (anyNull ? "<th>Nullable</th>" : "") + "</tr></thead><tbody>" +
        t.columns.map((c, i) => '<tr><td class="num muted">' + (i + 1) + '</td><td class="mono">' + esc(c.name) +
          '</td><td class="mono small">' + esc(c.type) + "</td>" +
          (anyNull ? "<td>" + (c.nullable == null ? "" : c.nullable ? "yes" : "no") + "</td>" : "") + "</tr>").join("") +
        "</tbody></table></div>";
      return;
    }
    if (d.tab === "ddl") {
      bodyEl.innerHTML = t.ddl
        ? '<div class="ds-ddl-actions"><button type="button" class="button small" data-ds-copy-ddl>Copy</button></div>' +
          '<pre class="ds-ddl mono"><code>' + esc(t.ddl) + "</code></pre>"
        : '<p class="muted">The storage type did not report a table definition.</p>';
      return;
    }
    // sample
    const limitSel =
      '<label class="ds-inline small">Rows <select data-ds-sample-limit>' +
      [25, 100, 500].map((n) => '<option value="' + n + '"' + (n === d.sampleLimit ? " selected" : "") + ">" + n + "</option>").join("") +
      '</select></label><button type="button" class="button small" data-ds-sample-reload>Reload</button>';
    if (!d.sample) {
      bodyEl.innerHTML = '<div class="ds-sample-bar">' + limitSel + '</div><p class="muted ds-loading">Reading a sample…</p>';
      this.loadSample();
      return;
    }
    const s = d.sample;
    if (s.supported === false) {
      bodyEl.innerHTML = '<p class="muted">' + esc(s.message || "Sampling is not available for this storage type.") + "</p>";
      return;
    }
    if (s.error) {
      bodyEl.innerHTML = '<div class="ds-sample-bar">' + limitSel + '</div><p class="flash error">' + esc(s.error) + "</p>";
      return;
    }
    const cell = (v) => v == null ? '<td class="ds-null">NULL</td>' : "<td>" + esc(typeof v === "object" ? JSON.stringify(v) : v) + "</td>";
    bodyEl.innerHTML =
      '<div class="ds-sample-bar">' + limitSel +
      '<span class="muted small">' + s.rows.length + " row" + (s.rows.length === 1 ? "" : "s") +
      (s.truncated ? " shown (more in the table)" : "") + "</span></div>" +
      '<div class="table-wrap ds-sample-wrap"><table class="data-table ds-sample"><thead><tr><th class="num">#</th>' +
      s.columns.map((c) => '<th class="mono">' + esc(c) + "</th>").join("") + "</tr></thead><tbody>" +
      s.rows.map((r, i) => '<tr><td class="num muted">' + (i + 1) + "</td>" + r.map(cell).join("") + "</tr>").join("") +
      "</tbody></table></div>";
  }

  async loadSample() {
    const d = this.detail;
    const path = d.path;
    const limit = d.sampleLimit;
    if (d._sampleInflight === path + "#" + limit) return;
    d._sampleInflight = path + "#" + limit;
    try {
      d.sample = await getJson("/api/v1/dataspace/table/sample?path=" + encodeURIComponent(path) + "&limit=" + limit);
    } catch (err) {
      d.sample = { error: err.message };
    }
    d._sampleInflight = null;
    if (this.current === path && this.detail === d && d.tab === "sample") this.paintTab();
  }

  async countRows(btn) {
    const d = this.detail;
    const path = d.path;
    btn.disabled = true;
    btn.textContent = "Counting…";
    try {
      const r = await getJson("/api/v1/dataspace/table/count?path=" + encodeURIComponent(path));
      if (r.supported === false) throw new Error(r.message || "Counting is not available here.");
      if (this.detail !== d) return;
      d.info.row_count = r.row_count;
      d.info.row_count_exact = true;
      d.info.message = null;
      this.paintTable();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "Count rows";
      btn.title = err.message;
      btn.insertAdjacentHTML("afterend", '<div class="flash error small">' + esc(err.message) + "</div>");
    }
  }

  // ------------------------------------------------ main-pane events

  onMainClick(ev) {
    const t = ev.target;
    const sortCol = t.closest("[data-ds-sort-col]");
    if (sortCol) {
      const k = sortCol.dataset.dsSortCol;
      this.listSort = { key: k, dir: this.listSort.key === k ? -this.listSort.dir : 1 };
      this.paintFolder();
      return;
    }
    if (t.closest("[data-ds-refresh-list]")) {
      this.listings.delete(this.current.toLowerCase());
      const li = this.findItem(this.current);
      if (li) { li.dataset.loaded = "0"; if (li.getAttribute("aria-expanded") === "true") this.loadChildren(li); }
      this.renderFolder(this.current);
      return;
    }
    if (t.closest("[data-ds-refresh-table]")) { this.loadTableInfo(true); return; }
    const tab = t.closest("[data-ds-tab]");
    if (tab) { this.detail.tab = tab.dataset.dsTab; this.paintTab(); return; }
    if (t.closest("[data-ds-sample-reload]")) { this.detail.sample = null; this.paintTab(); return; }
    const countBtn = t.closest("[data-ds-count]");
    if (countBtn) { this.countRows(countBtn); return; }
    if (t.closest("[data-ds-export]")) { this.openExport(); return; }
    const copy = t.closest("[data-ds-copy-ddl]");
    if (copy) {
      navigator.clipboard.writeText(this.detail.info.ddl || "").then(
        () => { copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1500); },
        () => { copy.textContent = "Copy failed"; },
      );
    }
  }

  // ---------------------------------------------------------- export

  initExportModal() {
    this.modal = document.querySelector("[data-ds-export-modal]");
    this.form = document.querySelector("[data-ds-export-form]");
    this.main.addEventListener("change", (ev) => {
      if (ev.target.matches("[data-ds-sample-limit]")) {
        this.detail.sampleLimit = Number(ev.target.value);
        this.detail.sample = null;
        this.paintTab();
      }
    });
    this.main.addEventListener("input", (ev) => {
      if (!ev.target.matches("[data-ds-list-filter]")) return;
      const q = ev.target.value.trim().toLowerCase();
      for (const tr of this.main.querySelectorAll("[data-ds-row]")) tr.hidden = !!q && !tr.dataset.name.includes(q);
    });
    for (const b of this.modal.querySelectorAll("[data-ds-export-close]")) b.addEventListener("click", () => this.closeExport());
    this.modal.addEventListener("click", (ev) => { if (ev.target === this.modal) this.closeExport(); });
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && !this.modal.hidden) this.closeExport(); });
    this.form.addEventListener("change", () => this.syncExportForm());
    this.form.addEventListener("submit", (ev) => { ev.preventDefault(); this.runExport(); });
  }

  openExport() {
    const t = this.detail.info;
    this.modal.querySelector("[data-role=ds-export-name]").textContent = t.name;
    this.form.reset();
    this.hideExportMessages();
    this.modal.hidden = false;
    document.body.classList.add("modal-open");
    this.syncExportForm(true);
    this.form.querySelector("input[name=format]").focus();
  }

  closeExport() {
    this.modal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  hideExportMessages() {
    for (const r of ["ds-export-error", "ds-export-ok"]) {
      const el = this.modal.querySelector("[data-role=" + r + "]");
      el.hidden = true;
      el.innerHTML = "";
    }
  }

  syncExportForm(reset = false) {
    const f = this.form;
    const fmt = f.elements.format.value;
    const t = this.detail.info;
    const cap = (t.export_limits && t.export_limits[fmt]) || this.limits[fmt] || 100000;
    const asJobBox = f.querySelector("[data-ds-as-job]");
    const jobRow = f.querySelector("[data-ds-job-row]");
    const jobOk = fmt === "csv" || fmt === "xlsx";
    if (jobRow) {
      jobRow.hidden = !jobOk;
      if (!jobOk && asJobBox) asJobBox.checked = false;
    }
    const asJob = !!(asJobBox && asJobBox.checked);
    f.querySelector("[data-ds-csv-only]").hidden = fmt !== "csv";
    const custom = f.elements.delimiter_custom;
    custom.hidden = f.elements.delimiter.value !== "custom";
    const limit = f.querySelector("[data-ds-limit]");
    limit.max = String(cap);
    limit.disabled = asJob;
    const known = t.row_count != null ? Number(t.row_count) : null;
    if (reset || !limit.value || Number(limit.value) > cap) limit.value = String(known != null ? Math.max(1, Math.min(known, cap)) : cap);
    const note = f.querySelector("[data-role=ds-limit-note]");
    note.textContent = asJob
      ? "(whole table — runs as a job)"
      : "(max " + fmtInt(cap) + " for " + fmt.toUpperCase() + (known != null && known > cap ? "; table has " + fmtInt(known) : "") + ")";
  }

  exportOptions() {
    const f = this.form;
    let delimiter = f.elements.delimiter.value;
    if (delimiter === "custom") delimiter = f.elements.delimiter_custom.value;
    return {
      format: f.elements.format.value,
      delimiter,
      header_case: f.elements.header_case.value,
      limit: Number(f.querySelector("[data-ds-limit]").value) || null,
      asJob: !!(f.querySelector("[data-ds-as-job]") && f.querySelector("[data-ds-as-job]").checked),
    };
  }

  async runExport() {
    const o = this.exportOptions();
    const path = this.detail.path;
    const errEl = this.modal.querySelector("[data-role=ds-export-error]");
    const okEl = this.modal.querySelector("[data-role=ds-export-ok]");
    this.hideExportMessages();
    if (o.format === "csv" && (!o.delimiter || o.delimiter.length !== 1) && o.delimiter !== "tab") {
      errEl.textContent = "The delimiter must be a single character.";
      errEl.hidden = false;
      return;
    }
    const go = this.form.querySelector("[data-ds-export-go]");
    go.disabled = true;
    const label = go.textContent;
    go.textContent = o.asJob ? "Queueing…" : "Exporting…";
    try {
      if (o.asJob) {
        const r = await fetch("/api/v1/dataspace/table/export-jobs", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ path, format: o.format, delimiter: o.delimiter, header_case: o.header_case }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((body && body.detail) || "HTTP " + r.status);
        okEl.innerHTML = "Export job queued. Follow it on <a href=\"/runs/" + encodeURIComponent(body.instance_id) +
          '">its run page</a>; the download link appears there when it finishes.';
        okEl.hidden = false;
      } else {
        const q = new URLSearchParams({ path, format: o.format, delimiter: o.delimiter, header_case: o.header_case });
        if (o.limit) q.set("limit", String(o.limit));
        const r = await fetch("/api/v1/dataspace/table/export?" + q.toString(), { credentials: "same-origin" });
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          throw new Error((body && body.detail) || "HTTP " + r.status);
        }
        const blob = await r.blob();
        const cd = r.headers.get("Content-Disposition") || "";
        const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
        const name = m ? decodeURIComponent(m[1]) : "export." + o.format;
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
        const n = r.headers.get("X-Ovid2-Export-Rows");
        okEl.textContent = "Downloaded " + name + (n ? " (" + fmtInt(n) + " rows)" : "") + ".";
        okEl.hidden = false;
      }
    } catch (err) {
      errEl.textContent = err.message;
      errEl.hidden = false;
    } finally {
      go.disabled = false;
      go.textContent = label;
    }
  }
}

// ------------------------------------------------------------------ boot

function boot() {
  const el = document.getElementById("ds-boot");
  if (!el) return;
  let data = {};
  try { data = JSON.parse(el.textContent || "{}"); } catch (_e) { data = {}; }
  const explorer = new DataSpaceExplorer(data);
  explorer.init();
  window.ovid2DataSpace = explorer;
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
