/*
 * Ovid2 storage navigator — a lazy, collapsible tree of storage accounts.
 *
 * Powers the Source / Target sections of the /submit job builder. One
 * tree per role (``input`` / ``output``), rooted at an element carrying
 * ``data-tree-role="<role>"``. The server renders the top level (saved
 * addresses + storage accounts, each with its type logo); deeper levels
 * are fetched on expand from
 *
 *     GET /api/v1/storage/{account}/browse?prefix=<path>
 *
 * which returns ``{type, supported, message, entries:[{name, kind, path}]}``
 * (``kind`` is ``dir`` | ``file`` | ``table``). Warehouses
 * (snowflake / postgres / mssql / databricks) list database → schema →
 * table; filesystems list folders + files.
 *
 * Interaction model (no "use" links):
 *
 *   - Clicking a node's **icon** assigns it to the slot (source/target).
 *     The row gets ``is-selected`` (accent ring + check badge).
 *   - Clicking the **name** or chevron expands / collapses the node.
 *     Leaves (files / tables / saved addresses) have nothing to expand, so
 *     clicking their name selects them too.
 *   - Keyboard: ↑/↓ move between rows, → expands, ← collapses (or moves
 *     to the parent), Enter/Space on the icon selects.
 *
 * A selection writes the existing hidden form fields
 * (``<role>_mode`` / ``<role>_address`` / ``<role>_connection`` /
 * ``<role>_location``) so the POST /submit contract is unchanged, then
 * dispatches ``ovid2:slot-change`` on ``document`` with the details.
 *
 * Every address is the *exact* location of its data: the chosen folder /
 * schema is read from (or written to) as-is. The slot detail carries
 * ``batch`` — the complete Data Space path ``<account>/<path>`` of the
 * chosen location — which the builder shows as the run's batch id.
 *
 * Dependency-free ES module.
 */

import { logoSvg, logoForType } from "/static/logos.js";

const WAREHOUSE_TYPES = new Set(["snowflake", "postgres", "mssql", "databricks"]);

function esc(s) {
  return String(s == null ? "" : s).replace(/[<>&"']/g, (c) => ({
    "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

export class StorageTree {
  /**
   * @param {HTMLElement} root  element with ``data-tree-role``.
   * @param {HTMLFormElement} form  the owning submit form.
   */
  constructor(root, form) {
    this.root = root;
    this.form = form;
    this.role = root.getAttribute("data-tree-role");
    this.list = root.querySelector("[role=tree]");
    this.bind();
    this.restore();
  }

  field(suffix) {
    return this.form.querySelector('[name="' + this.role + "_" + suffix + '"]');
  }

  setField(suffix, value) {
    const f = this.field(suffix);
    if (f) f.value = value;
  }

  // -------------------------------------------------------------- events

  bind() {
    this.root.addEventListener("click", (evt) => {
      const item = evt.target.closest("[role=treeitem]");
      if (!item || !this.root.contains(item)) return;
      if (evt.target.closest(".tree-icon")) {
        evt.preventDefault();
        this.select(item);
        return;
      }
      if (evt.target.closest(".tree-label, .tree-chevron")) {
        evt.preventDefault();
        if (this.isExpandable(item)) this.toggle(item);
        else this.select(item);
      }
    });
    this.root.addEventListener("keydown", (evt) => this.onKey(evt));
  }

  onKey(evt) {
    const btn = evt.target.closest(".tree-icon, .tree-label");
    if (!btn) return;
    const item = btn.closest("[role=treeitem]");
    const rows = this.visibleRows();
    const idx = rows.indexOf(item);
    const focusRow = (row) => {
      if (!row) return;
      const target = row.querySelector(":scope > .tree-row .tree-label");
      if (target) target.focus();
    };
    switch (evt.key) {
      case "ArrowDown":
        evt.preventDefault();
        focusRow(rows[idx + 1]);
        break;
      case "ArrowUp":
        evt.preventDefault();
        focusRow(rows[idx - 1]);
        break;
      case "ArrowRight":
        evt.preventDefault();
        if (this.isExpandable(item) && item.getAttribute("aria-expanded") !== "true") {
          this.toggle(item, true);
        } else {
          focusRow(rows[idx + 1]);
        }
        break;
      case "ArrowLeft":
        evt.preventDefault();
        if (item.getAttribute("aria-expanded") === "true") {
          this.toggle(item, false);
        } else {
          focusRow(item.parentElement.closest("[role=treeitem]"));
        }
        break;
      default:
        break;
    }
  }

  visibleRows() {
    const out = [];
    const walk = (ul) => {
      for (const li of ul.children) {
        if (li.getAttribute("role") !== "treeitem") continue;
        out.push(li);
        const kids = li.querySelector(":scope > [role=group]");
        if (kids && li.getAttribute("aria-expanded") === "true") walk(kids);
      }
    };
    if (this.list) walk(this.list);
    return out;
  }

  // ------------------------------------------------------- expand / load

  isExpandable(item) {
    const k = item.dataset.kind;
    return k === "group" || k === "account" || k === "dir";
  }

  toggle(item, force) {
    const open = force !== undefined ? force : item.getAttribute("aria-expanded") !== "true";
    item.setAttribute("aria-expanded", open ? "true" : "false");
    const group = item.querySelector(":scope > [role=group]");
    if (group) group.hidden = !open;
    if (open && (item.dataset.kind === "account" || item.dataset.kind === "dir")) {
      if (item.dataset.loaded !== "1") this.load(item);
    }
  }

  ensureGroup(item) {
    let group = item.querySelector(":scope > [role=group]");
    if (!group) {
      group = document.createElement("ul");
      group.setAttribute("role", "group");
      group.className = "tree-group";
      item.appendChild(group);
    }
    return group;
  }

  async load(item) {
    const group = this.ensureGroup(item);
    group.hidden = false;
    group.innerHTML = '<li class="tree-msg">Loading&hellip;</li>';
    item.dataset.loaded = "loading";
    const account = item.dataset.conn;
    const prefix = item.dataset.path || "";
    let url = "/api/v1/storage/" + encodeURIComponent(account) + "/browse";
    if (prefix) url += "?prefix=" + encodeURIComponent(prefix);
    try {
      const r = await fetch(url, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      let body = {};
      try { body = await r.json(); } catch (_e) { body = {}; }
      if (!r.ok) {
        throw new Error((body && body.detail) || "HTTP " + r.status);
      }
      this.renderChildren(item, group, body);
      item.dataset.loaded = "1";
    } catch (err) {
      item.dataset.loaded = "0";
      group.innerHTML =
        '<li class="tree-msg tree-err">Could not browse: ' + esc(err && err.message) + "</li>";
    }
  }

  renderChildren(item, group, body) {
    if (body.supported === false) {
      group.innerHTML =
        '<li class="tree-msg">' +
        esc(body.message || "Browsing is not supported for this storage type.") +
        " Pick the account itself or a saved address.</li>";
      return;
    }
    const type = String(body.type || item.dataset.type || "").toLowerCase();
    const warehouse = WAREHOUSE_TYPES.has(type);
    const entries = body.entries || [];
    const html = [];
    if (!entries.length) html.push('<li class="tree-msg">(empty)</li>');
    for (const e of entries) {
      const isDir = e.kind === "dir";
      const logo = isDir ? (warehouse ? "schema" : "folder") : (e.kind === "table" ? "table" : "file");
      html.push(this.nodeHtml({
        kind: isDir ? "dir" : "leaf",
        conn: item.dataset.conn,
        type,
        path: e.path,
        name: e.name,
        logo,
        title: isDir
          ? "Click the icon to choose this " + (warehouse ? "database/schema" : "folder") + "; click the name to open it"
          : "Click to choose this " + (e.kind === "table" ? "table" : "file"),
      }));
    }
    if (body.message) html.push('<li class="tree-msg">' + esc(body.message) + "</li>");
    group.innerHTML = html.join("");
    this.markSelected();
  }

  nodeHtml(n) {
    const expandable = n.kind === "dir" || n.kind === "account" || n.kind === "group";
    return (
      '<li role="treeitem" class="tree-item tree-' + esc(n.kind) + '"' +
      ' data-kind="' + esc(n.kind) + '"' +
      ' data-conn="' + esc(n.conn || "") + '"' +
      ' data-type="' + esc(n.type || "") + '"' +
      ' data-path="' + esc(n.path || "") + '"' +
      (expandable ? ' aria-expanded="false"' : "") +
      ' aria-selected="false">' +
      '<div class="tree-row">' +
      (expandable
        ? '<span class="tree-chevron" aria-hidden="true">' + logoSvg("chevron", 14) + "</span>"
        : '<span class="tree-chevron tree-chevron-spacer" aria-hidden="true"></span>') +
      '<button type="button" class="tree-icon" title="' + esc(n.title || "Click to choose") + '"' +
      ' aria-label="Choose ' + esc(n.name) + '">' + logoSvg(n.logo, 18) +
      '<span class="tree-check" aria-hidden="true">' + logoSvg("check", 10) + "</span></button>" +
      '<button type="button" class="tree-label">' + esc(n.name) + "</button>" +
      "</div></li>"
    );
  }

  // ---------------------------------------------------------- selection

  select(item) {
    const kind = item.dataset.kind;
    const type = (item.dataset.type || "").toLowerCase();
    if (kind === "group") {
      this.toggle(item);
      return;
    }
    if (kind === "account" && WAREHOUSE_TYPES.has(type)) {
      // A warehouse has no runnable root: open it and ask for a schema.
      this.toggle(item, true);
      this.hint(item, "Choose a database / schema inside this warehouse.");
      return;
    }
    let detail;
    if (kind === "address") {
      detail = {
        role: this.role,
        mode: "address",
        address: item.dataset.path,
        connection: "",
        location: "",
        type,
        label: item.dataset.path,
        nodeKind: kind,
        batch: item.dataset.batch || "",
      };
      this.setField("mode", "address");
      this.setField("address", item.dataset.path);
      this.setField("connection", "");
      this.setField("location", "");
    } else {
      const conn = item.dataset.conn;
      const loc = kind === "account" ? "" : item.dataset.path || "";
      detail = {
        role: this.role,
        mode: "browse",
        address: "",
        connection: conn,
        location: loc,
        type,
        label: conn + (loc ? " / " + loc : ""),
        nodeKind: kind,
        batch: conn + (loc ? "/" + loc.replace(/^\/+|\/+$/g, "") : ""),
      };
      this.setField("mode", "browse");
      this.setField("address", "");
      this.setField("connection", conn);
      this.setField("location", loc);
    }
    this.selected = detail;
    this.markSelected();
    this.hint(null);
    document.dispatchEvent(new CustomEvent("ovid2:slot-change", { detail }));
  }

  clear() {
    this.selected = null;
    this.setField("mode", "address");
    this.setField("address", "");
    this.setField("connection", "");
    this.setField("location", "");
    this.markSelected();
    document.dispatchEvent(new CustomEvent("ovid2:slot-change", {
      detail: { role: this.role, mode: "", label: "" },
    }));
  }

  markSelected() {
    const s = this.selected;
    for (const li of this.root.querySelectorAll("[role=treeitem]")) {
      let on = false;
      if (s) {
        if (s.mode === "address") {
          on = li.dataset.kind === "address" && li.dataset.path === s.address;
        } else if (li.dataset.kind !== "address" && li.dataset.conn === s.connection) {
          on = li.dataset.kind === "account" ? !s.location : li.dataset.path === s.location;
        }
      }
      li.classList.toggle("is-selected", on);
      li.setAttribute("aria-selected", on ? "true" : "false");
    }
  }

  hint(item, msg) {
    for (const h of this.root.querySelectorAll(".tree-hint")) h.remove();
    if (!item || !msg) return;
    const row = item.querySelector(":scope > .tree-row");
    const p = document.createElement("div");
    p.className = "tree-hint small";
    p.textContent = msg;
    row.after(p);
  }

  // ------------------------------------------------------------ restore

  /* Re-apply a preselected address (``?input_address=…``) or a prior
   * browse choice written into the hidden fields by the server. */
  restore() {
    const addr = (this.field("address") || {}).value || "";
    const conn = (this.field("connection") || {}).value || "";
    const loc = (this.field("location") || {}).value || "";
    let node = null;
    if (addr) {
      node = this.root.querySelector('[data-kind="address"][data-path="' + CSS.escape(addr) + '"]');
      if (node) {
        const group = node.closest('[data-kind="group"]');
        if (group) this.toggle(group, true);
        this.select(node);
        return;
      }
      // Address not in the list — still honour it so the POST works.
      this.selected = { role: this.role, mode: "address", address: addr, label: addr, type: "", batch: "" };
      document.dispatchEvent(new CustomEvent("ovid2:slot-change", { detail: this.selected }));
      return;
    }
    if (conn) {
      const type = (this.root.querySelector('[data-kind="account"][data-conn="' + CSS.escape(conn) + '"]') || { dataset: {} }).dataset.type || "";
      this.selected = {
        role: this.role, mode: "browse", address: "", connection: conn, location: loc,
        type, label: conn + (loc ? " / " + loc : ""), nodeKind: loc ? "dir" : "account",
        batch: conn + (loc ? "/" + loc.replace(/^\/+|\/+$/g, "") : ""),
      };
      this.markSelected();
      document.dispatchEvent(new CustomEvent("ovid2:slot-change", { detail: this.selected }));
    }
  }
}

export { logoForType };
