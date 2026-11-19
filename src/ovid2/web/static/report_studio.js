/* Ovid2 Report Studio — editor / viewer (/reports/{id}).
 *
 * State: a *working copy* of the layout (plus undo / redo stacks). Every
 * change is an edit op from the closed vocabulary in
 * ovid2.reports.layout.OPS. The server validates the op batch against the
 * chart library and the report source (POST /reports/{id}/ops save=false
 * render=true) and returns the new layout + render model, so the browser
 * never needs its own copy of the validation rules. Save PUTs the working
 * copy (new version; 409 on a stale version).
 *
 * The canvas is drawn by report_render.js in the report theme; this file
 * never reads the page theme for the canvas. It is interactive: every text
 * (report title / subtitle / intro, section titles / descriptions, block
 * titles / captions / bodies, info rows, checklist items) is edited in
 * place and committed as one op; a floating toolbar moves / hides /
 * duplicates / removes the selected block; "+" points insert blocks.
 * Two views: continuous and pages (sheets at the report's page size).
 *
 * Private notes (only their author sees them) are NOT layout blocks: they
 * live in web_report_notes and are edited through /reports/{id}/notes;
 * the server merges them into the render model flagged ``private``.
 *
 * window.Ovid2ReportStudio.{outline, applyOps, info} is the hook the
 * assistant's edit_report_layout UI action uses (llm_panel.js): the ops are
 * re-validated by the same server endpoint and are undoable here. The
 * assistant never sees private notes.
 */
(function () {
  "use strict";

  var root = document.getElementById("report-studio");
  if (!root) return;
  var RID = root.getAttribute("data-report-id");
  var canEdit = root.getAttribute("data-can-edit") === "true";
  var isOwner = root.getAttribute("data-is-owner") === "true";
  var OPS = ["set_report", "add_section", "update_section", "move_section", "remove_section",
             "add_block", "update_block", "move_block", "remove_block", "set_visibility"];
  var FREE_TYPES = ["text", "callout", "note", "info", "checklist", "divider", "spacer"];
  var VIEW_KEY = "ovid2.reports.view";
  var ZOOM_KEY = "ovid2.reports.zoom";

  var S = {
    report: null, layout: null, saved: null, model: null, catalogue: null, source: null,
    undo: [], redo: [], sel: { kind: "report", id: null }, busy: false, version: 0,
    // True once the assistant edited the working copy: autosave then waits
    // for a human Save (the assistant never saves on its own).
    agentEdits: false,
    view: "continuous", zoom: "1", showHidden: false, showPrivate: true,
    notes: [], tab: "content", editing: false
  };

  /* ---------------- helpers ---------------- */

  function $(sel, el) { return (el || root).querySelector(sel); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

  function api(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch("/api/v1" + path, opts).then(function (r) {
      return r.text().then(function (t) {
        var data; try { data = t ? JSON.parse(t) : null; } catch (e) { data = t; }
        if (!r.ok) {
          var d = data && data.detail;
          var msg = typeof d === "string" ? d : (d && d.message) || ("HTTP " + r.status);
          var err = new Error(msg); err.status = r.status; err.errors = (d && d.errors) || []; err.detail = d;
          throw err;
        }
        return data;
      });
    });
  }

  var noticeTimer = null;
  function notice(text, kind, sticky) {
    var n = $("[data-notice]");
    n.textContent = text || "";
    n.className = "rs-notice" + (kind ? " rs-notice-" + kind : "");
    n.hidden = !text;
    clearTimeout(noticeTimer);
    if (text && !sticky && kind !== "error") noticeTimer = setTimeout(function () { n.hidden = true; }, 4000);
  }

  function showErrors(list) {
    var ul = $("[data-errors]");
    ul.innerHTML = "";
    (list || []).forEach(function (m) { ul.appendChild(el("li", null, m)); });
    ul.hidden = !(list && list.length);
  }

  function chart(name) {
    return ((S.catalogue && S.catalogue.charts) || []).filter(function (c) { return c.name === name; })[0] || null;
  }
  function theme(name) {
    return ((S.catalogue && S.catalogue.themes) || []).filter(function (t) { return t.name === name; })[0] || null;
  }
  function findSection(id) {
    return (S.layout.sections || []).filter(function (s) { return s.id === id; })[0] || null;
  }
  function findBlock(id) {
    var out = null;
    (S.layout.sections || []).forEach(function (s) {
      (s.blocks || []).forEach(function (b) { if (b.id === id) out = { section: s, block: b }; });
    });
    return out;
  }
  function dataset(name) { return S.source && S.source.datasets ? S.source.datasets[name] : null; }

  /* ---------------- core: apply ops ---------------- */

  function dirty() { return !same(S.layout, S.saved); }

  function refreshChrome() {
    var d = dirty();
    $("[data-dirty]").hidden = !d;
    var save = $("[data-save]"); if (save) save.disabled = !d || S.busy;
    if (d) scheduleAutosave();
    var u = $("[data-undo]"); if (u) u.disabled = !S.undo.length || S.busy;
    var r = $("[data-redo]"); if (r) r.disabled = !S.redo.length || S.busy;
    var t = theme(S.layout && S.layout.theme);
    $("[data-theme-label]").textContent = "Theme: " + (t ? t.label : (S.layout && S.layout.theme) || "publication");
  }

  /* Validate + apply ``ops`` on the server against the working copy.
   * Resolves to the change notes; rejects with the validation errors. */
  function applyOps(ops, opts) {
    opts = opts || {};
    if (!canEdit) return Promise.reject(new Error("You have view-only access to this report."));
    if (!Array.isArray(ops) || !ops.length) return Promise.reject(new Error("No changes."));
    if (S.busy) return Promise.reject(new Error("Busy — try again in a moment."));
    S.busy = true; refreshChrome();
    return api("POST", "/reports/" + encodeURIComponent(RID) + "/ops",
               { ops: ops, save: false, render: true, layout: S.layout,
                 mode: renderMode(), private_notes: S.showPrivate })
      .then(function (r) {
        S.undo.push(clone(S.layout));
        if (S.undo.length > 100) S.undo.shift();
        S.redo = [];
        S.layout = r.layout; S.model = r.model;
        if (opts.select) S.sel = opts.select;
        showErrors([]);
        renderAll();
        return r.notes || [];
      })
      .catch(function (e) { showErrors(e.errors && e.errors.length ? e.errors : [e.message]); throw e; })
      .then(function (v) { S.busy = false; refreshChrome(); return v; },
            function (e) { S.busy = false; refreshChrome(); throw e; });
  }

  function renderMode() { return canEdit && S.showHidden ? "edit" : "view"; }

  function rerender() {
    return api("POST", "/reports/" + encodeURIComponent(RID) + "/preview",
               { layout: S.layout, mode: renderMode(), private_notes: S.showPrivate })
      .then(function (m) { S.model = m; renderCanvas(); });
  }

  function undo() {
    if (!S.undo.length || S.busy) return;
    S.redo.push(clone(S.layout)); S.layout = S.undo.pop();
    afterHistory();
  }
  function redo() {
    if (!S.redo.length || S.busy) return;
    S.undo.push(clone(S.layout)); S.layout = S.redo.pop();
    afterHistory();
  }
  function afterHistory() {
    renderOutline(); renderInspector(); refreshChrome();
    if (canEdit) rerender().catch(function (e) { notice(e.message, "error"); });
  }

  /* ``opts.auto``: an autosave — quiet on success, noted "Autosave" in
   * History, and a conflict pauses autosave instead of retrying. */
  function save(opts) {
    opts = opts || {};
    clearTimeout(autosaveTimer); autosaveTimer = null;
    if (!dirty() || S.busy) return Promise.resolve();
    S.busy = true; refreshChrome();
    var title = $("[data-title]").value.trim() || S.layout.title;
    var lay = clone(S.layout); lay.title = title;
    var body = { layout: lay, expected_version: S.version };
    if (opts.auto) body.note = "Autosave";
    setSaveState(opts.auto ? "Saving…" : "");
    return api("PUT", "/reports/" + encodeURIComponent(RID), body)
      .then(function (rep) {
        // ``S.busy`` blocks edits while the PUT is in flight, so the
        // server copy is exactly the working copy.
        S.report = rep; S.version = rep.version; S.layout = clone(rep.layout); S.saved = clone(rep.layout);
        S.agentEdits = false;
        $("[data-meta]").textContent = "v" + rep.version + " · " + rep.permission + (rep.folder ? " · " + rep.folder : "");
        var db = $("[data-draft-badge]"); if (db) db.remove();
        if (opts.auto) setSaveState("Autosaved v" + rep.version);
        else { setSaveState(""); notice("Saved version " + rep.version + ".", "ok"); }
        renderOutline();
      })
      .catch(function (e) {
        setSaveState("");
        // A failed autosave pauses autosave for this page (no retry loop);
        // the remembered preference is unchanged.
        var paused = opts.auto || autosaveOn();
        if (paused) setAutosave(false, true);
        if (e.status === 409) notice("Someone saved this report since you opened it (now v" + (e.detail && e.detail.current_version) + "). Reload to see their changes — your edits are still here." + (paused ? " Autosave is paused." : ""), "error", true);
        else {
          showErrors(e.errors && e.errors.length ? e.errors : [e.message]);
          if (paused) notice("Save failed — autosave is paused. Fix the problem and press Save.", "error", true);
        }
      })
      .then(function () { S.busy = false; refreshChrome(); });
  }

  /* ---------------- autosave ---------------- */

  var AUTOSAVE_KEY = "ovid2.reports.autosave";
  var AUTOSAVE_DELAY_MS = 2500;
  var autosaveTimer = null;

  function autosaveBox() { return $("[data-autosave]"); }
  function autosaveOn() { var b = autosaveBox(); return !!(b && b.checked && canEdit); }
  function setSaveState(text) {
    var n = $("[data-save-state]"); if (!n) return;
    n.textContent = text || ""; n.hidden = !text;
  }
  function readAutosavePref() {
    try { return window.localStorage.getItem(AUTOSAVE_KEY) === "on"; } catch (e) { return false; }
  }
  /* ``transient``: pause for this page only (e.g. after a conflict) without
   * changing the remembered preference. */
  function setAutosave(on, transient) {
    var b = autosaveBox(); if (!b) return;
    b.checked = !!on;
    if (!transient) { try { window.localStorage.setItem(AUTOSAVE_KEY, on ? "on" : "off"); } catch (e) { /* private mode */ } }
    if (!on) { clearTimeout(autosaveTimer); autosaveTimer = null; setSaveState(""); }
    if (S.layout) refreshChrome();
  }
  function scheduleAutosave() {
    if (!autosaveOn()) return;
    if (S.agentEdits) { setSaveState("Assistant edits — press Save"); return; }
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(function tick() {
      autosaveTimer = null;
      if (!autosaveOn() || !dirty() || S.agentEdits) return;
      if (S.busy) { autosaveTimer = setTimeout(tick, AUTOSAVE_DELAY_MS); return; }
      save({ auto: true });
    }, AUTOSAVE_DELAY_MS);
  }
  function flushAutosave() {
    if (autosaveOn() && S.layout && dirty() && !S.busy && !S.agentEdits) save({ auto: true });
  }

  /* ---------------- canvas ---------------- */

  function isPrivateId(id) { return /^pn_[0-9a-f]{12}$/.test(String(id || "")); }
  function privateNote(id) { return S.notes.filter(function (n) { return n.note_id === id; })[0] || null; }

  function applyZoom() {
    var box = $("[data-canvas]");
    var rep = box.querySelector(".ovr-report");
    if (!rep) return;
    var z = S.zoom;
    if (z === "fit") {
      var w = S.view === "pages" ? window.Ovid2ReportRender.pageBox(S.model.page).width : rep.scrollWidth;
      z = Math.min(1, Math.max(0.4, (box.clientWidth - 56) / Math.max(w, 1)));
    } else z = parseFloat(z) || 1;
    rep.style.zoom = String(z);
  }

  function renderCanvas() {
    var box = $("[data-canvas]");
    if (!S.model) { box.textContent = "Loading…"; return; }
    if (!window.Ovid2ReportRender) { box.textContent = "Renderer failed to load."; return; }
    box.setAttribute("data-view", S.view);
    var scroll = box.scrollTop;
    hideToolbar();
    window.Ovid2ReportRender.render(box, S.model, {
      actions: true,
      editable: canEdit,
      pages: S.view === "pages",
      onHead: wireHead,
      onBlock: wireBlock,
      onSection: wireSection,
      onGrid: wireGrid,
      onPaged: function () { applyZoom(); markSelection(); box.scrollTop = scroll; }
    });
    applyZoom();
    markSelection();
    box.scrollTop = scroll;
  }

  /* Contenteditable inline editing. ``opts.multiline`` keeps newlines
   * (markdown bodies); Enter commits otherwise. ``commit`` receives the new
   * plain text; Esc / unchanged text restores the rendered node. */
  function inline(node, value, commit, opts) {
    opts = opts || {};
    if (!canEdit && !opts.always) return;
    node.classList.add("rs-editable");
    if (opts.placeholder) node.setAttribute("data-placeholder", opts.placeholder);
    node.setAttribute("tabindex", "0");
    node.addEventListener("click", function (ev) {
      if (node.isContentEditable) { ev.stopPropagation(); return; }
      if (opts.selectFirst) opts.selectFirst();
      ev.stopPropagation();
      start();
    });
    node.addEventListener("keydown", function (ev) {
      if (!node.isContentEditable && (ev.key === "Enter" || ev.key === "F2")) { ev.preventDefault(); start(); }
    });
    function start() {
      var original = node.innerHTML;
      S.editing = true;
      node.classList.remove("ovr-placeholder");
      node.classList.add("rs-editing");
      node.textContent = value == null ? "" : value;
      node.contentEditable = "plaintext-only";
      if (node.contentEditable !== "plaintext-only") node.contentEditable = "true";
      node.focus();
      var sel = window.getSelection(), rng = document.createRange();
      rng.selectNodeContents(node); rng.collapse(false); sel.removeAllRanges(); sel.addRange(rng);
      if (opts.multiline) showMdBar(node);
      var done = false;
      function finish(save) {
        if (done) return; done = true;
        S.editing = false;
        hideMdBar();
        var text = node.innerText.replace(/\u00a0/g, " ");
        if (!opts.multiline) text = text.replace(/\s*\n\s*/g, " ").trim();
        else text = text.replace(/\n{3,}/g, "\n\n").replace(/\s+$/, "");
        node.contentEditable = "false";
        node.classList.remove("rs-editing");
        node.removeEventListener("keydown", onKey);
        node.removeEventListener("blur", onBlur);
        if (!save || text === (value == null ? "" : String(value))) { node.innerHTML = original; return; }
        if (opts.required && !text) { node.innerHTML = original; notice(opts.required, "error"); return; }
        Promise.resolve(commit(text)).catch(function () { node.innerHTML = original; });
      }
      function onKey(ev) {
        ev.stopPropagation();
        if (ev.key === "Escape") { ev.preventDefault(); finish(false); node.blur(); }
        else if (ev.key === "Enter" && (!opts.multiline || ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); finish(true); }
        else if (opts.onKey) opts.onKey(ev, node, finish);
      }
      function onBlur() { setTimeout(function () { if (!mdBarHasFocus()) finish(true); }, 120); }
      node.addEventListener("keydown", onKey);
      node.addEventListener("blur", onBlur);
    }
  }

  /* Markdown helper bar for multi-line inline edits: inserts markdown
   * syntax around the selection (the server renders it, raw HTML off). */
  var mdBar = null, mdTarget = null;
  function mdBarHasFocus() { return mdBar && mdBar.contains(document.activeElement); }
  function showMdBar(node) {
    if (!mdBar) {
      mdBar = el("div", "rs-mdbar"); mdBar.setAttribute("role", "toolbar"); mdBar.setAttribute("aria-label", "Formatting");
      [["B", "**", "**", "Bold (Ctrl+B)"], ["I", "_", "_", "Italic (Ctrl+I)"], ["H", "### ", "", "Heading"],
       ["\u2022", "- ", "", "Bulleted list"], ["1.", "1. ", "", "Numbered list"], ["\u201C", "> ", "", "Quote"],
       ["</>", "`", "`", "Code"], ["\u{1F517}", "[", "](https://)", "Link"]].forEach(function (d) {
        var b = el("button", "rs-mdbtn", d[0]); b.type = "button"; b.title = d[3]; b.setAttribute("aria-label", d[3]);
        b.addEventListener("mousedown", function (ev) { ev.preventDefault(); wrapSel(d[1], d[2], !d[2]); });
        mdBar.appendChild(b);
      });
      var hint = el("span", "rs-mdhint", "Ctrl+Enter to save · Esc to cancel");
      mdBar.appendChild(hint);
      document.body.appendChild(mdBar);
    }
    mdTarget = node;
    var r = node.getBoundingClientRect();
    mdBar.style.top = Math.max(8, r.top + window.scrollY - 40) + "px";
    mdBar.style.left = Math.max(8, r.left + window.scrollX) + "px";
    mdBar.hidden = false;
    node.addEventListener("keydown", mdKeys);
  }
  function mdKeys(ev) {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    var k = ev.key.toLowerCase();
    if (k === "b") { ev.preventDefault(); wrapSel("**", "**"); }
    else if (k === "i") { ev.preventDefault(); wrapSel("_", "_"); }
  }
  function hideMdBar() {
    if (mdBar) mdBar.hidden = true;
    if (mdTarget) mdTarget.removeEventListener("keydown", mdKeys);
    mdTarget = null;
  }
  function wrapSel(pre, post, linePrefix) {
    if (!mdTarget) return;
    mdTarget.focus();
    var sel = window.getSelection();
    if (!sel.rangeCount) return;
    var rng = sel.getRangeAt(0);
    var text = rng.toString();
    if (linePrefix) {
      text = (text || "").split("\n").map(function (l) { return pre + l; }).join("\n") || pre;
      document.execCommand("insertText", false, text);
    } else {
      document.execCommand("insertText", false, pre + (text || "text") + post);
    }
  }

  function upd(id, set, extra) {
    var op = { op: "update_block", block_id: id, set: set };
    if (extra) Object.keys(extra).forEach(function (k) { op[k] = extra[k]; });
    return applyOps([op]);
  }

  function wireHead(head) {
    var L = S.layout;
    head.classList.add("rs-pickable");
    head.addEventListener("click", function () { select("report", null); });
    inline(head.querySelector(".ovr-title"), L.title, function (v) {
      $("[data-title]").value = v;
      return applyOps([{ op: "set_report", set: { title: v } }]);
    }, { required: "A report needs a title.", placeholder: "Report title" });
    var sub = head.querySelector(".ovr-subtitle");
    if (sub) inline(sub, L.subtitle || "", function (v) { return applyOps([{ op: "set_report", set: { subtitle: v || null } }]); },
                    { placeholder: "Add a subtitle" });
    var intro = head.querySelector(".ovr-intro");
    if (intro) inline(intro, L.description || "", function (v) { return applyOps([{ op: "set_report", set: { description: v || null } }]); },
                      { multiline: true, placeholder: "Add an introduction…" });
  }

  function wireSection(sec, s) {
    if (s.private) { sec.classList.add("rs-private-section"); return; }
    var head = sec.querySelector(".ovr-section-head");
    if (head) {
      head.classList.add("rs-pickable");
      head.addEventListener("click", function () { select("section", s.id); });
      inline(head.querySelector(".ovr-section-title"), s.title, function (v) {
        return applyOps([{ op: "update_section", section_id: s.id, set: { title: v } }]);
      }, { required: "A section needs a title.", selectFirst: function () { select("section", s.id); } });
    }
    var desc = sec.querySelector(".ovr-section-desc");
    if (desc) inline(desc, s.description || "", function (v) {
      return applyOps([{ op: "update_section", section_id: s.id, set: { description: v || null } }]);
    }, { multiline: true, placeholder: "Add a section description…", selectFirst: function () { select("section", s.id); } });
    if (s.visible === false) sec.appendChild(el("span", "rs-badge rs-badge-hidden", "Hidden"));
  }

  /* "+" insertion points between blocks (and at the end of a section). */
  function wireGrid(grid, s) {
    if (s.private || !canEdit && !s.blocks.length) return;
    var cards = Array.prototype.slice.call(grid.children);
    cards.forEach(function (card, i) {
      grid.insertBefore(addPoint(s, i), card);
    });
    var tail = addPoint(s, cards.length);
    tail.classList.add("rs-addpoint-tail");
    grid.appendChild(tail);
  }

  function addPoint(s, index) {
    var w = el("div", "rs-addpoint");
    var b = el("button", "rs-addpoint-btn"); b.type = "button";
    b.setAttribute("aria-label", "Add a block here");
    b.title = "Add a block here";
    b.textContent = "+";
    b.addEventListener("click", function (ev) { ev.stopPropagation(); openAddMenu(b, s, index); });
    w.appendChild(b);
    return w;
  }

  function wireBlock(card, b) {
    card.classList.add("rs-pickable");
    var priv = !!b.private;
    if (priv) card.setAttribute("data-private", "true");
    card.addEventListener("click", function (ev) {
      if (ev.target.closest(".vega-embed details, a, .rs-editing")) return;
      select(priv ? "note" : "block", b.id);
    });
    if (b.visible === false) card.appendChild(el("span", "rs-badge rs-badge-hidden", "Hidden"));
    else if (b.export === false && !priv) card.appendChild(el("span", "rs-badge", "Not exported"));
    if (priv) card.appendChild(el("span", "rs-badge rs-badge-private", "\uD83D\uDD12 Only you"));
    var editable = priv || canEdit;
    if (!editable) return;
    var commit = priv
      ? function (set) { return saveNote(b.id, set); }
      : function (set) { return upd(b.id, set); };
    var opts0 = { always: priv, selectFirst: function () { select(priv ? "note" : "block", b.id); } };
    var t = card.querySelector(".ovr-block-title");
    if (t) inline(t, b.title || "", function (v) { return commit({ title: v || null }); },
                  Object.assign({ placeholder: "Add a title" }, opts0));
    var cap = card.querySelector(".ovr-caption");
    if (cap) inline(cap, b.caption || "", function (v) { return commit({ caption: v || null }); },
                    Object.assign({ placeholder: "Add a caption" }, opts0));
    if (b.renderer === "text" || b.renderer === "callout" || b.renderer === "note") {
      var body = card.querySelector(b.renderer === "note" ? ".ovr-note-body" : ".ovr-body");
      inline(body, b.markdown || "", function (v) {
        if (!v && b.renderer !== "note") { notice("Text can't be empty — remove the block instead.", "error"); return Promise.reject(); }
        var set = { text: v };
        if (b.renderer !== "note" && !priv) set.text_ref = null;
        return commit(set);
      }, Object.assign({ multiline: true, placeholder: "Write here…" }, opts0));
    } else if (b.renderer === "info") wireInfo(card, b, commit, opts0);
    else if (b.renderer === "checklist") wireChecklist(card, b, commit, opts0);
  }

  function wireInfo(card, b, commit, opts0) {
    var rows = clone(b.rows || []);
    var dl = card.querySelector(".ovr-info");
    function save() { return commit({ rows: rows }); }
    Array.prototype.forEach.call(dl.querySelectorAll("dt, dd"), function (cell) {
      var i = parseInt(cell.getAttribute("data-row"), 10), key = cell.tagName === "DT" ? "key" : "value";
      inline(cell, rows[i][key], function (v) { rows[i][key] = v; return save(); },
             Object.assign({ placeholder: key === "key" ? "Label" : "Value",
                             onKey: function (ev, node, finish) {
                               if (ev.key === "Tab") { ev.preventDefault(); finish(true); }
                             } }, opts0));
      if (key === "value") {
        var x = el("button", "rs-row-x", "\u00D7"); x.type = "button"; x.title = "Remove row"; x.setAttribute("aria-label", "Remove row");
        x.addEventListener("click", function (ev) { ev.stopPropagation(); rows.splice(i, 1); save(); });
        cell.appendChild(x);
      }
    });
    var add = el("button", "rs-inline-add", "+ Row"); add.type = "button";
    add.addEventListener("click", function (ev) { ev.stopPropagation(); rows.push({ key: "Label", value: "" }); save(); });
    card.appendChild(add);
  }

  function wireChecklist(card, b, commit, opts0) {
    var items = clone(b.items || []);
    function save() { return commit({ items: items }); }
    Array.prototype.forEach.call(card.querySelectorAll(".ovr-checklist li"), function (li) {
      var i = parseInt(li.getAttribute("data-item"), 10);
      var box = li.querySelector(".ovr-check");
      box.setAttribute("role", "checkbox"); box.setAttribute("aria-checked", String(!!items[i].done)); box.tabIndex = 0;
      box.classList.add("rs-editable");
      function toggle(ev) { ev.stopPropagation(); items[i].done = !items[i].done; save(); }
      box.addEventListener("click", toggle);
      box.addEventListener("keydown", function (ev) { if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); toggle(ev); } });
      inline(li.querySelector(".ovr-check-text"), items[i].text, function (v) {
        if (!v) items.splice(i, 1); else items[i].text = v;
        return save();
      }, Object.assign({ placeholder: "Item", onKey: function (ev, node, finish) {
        if (ev.key === "Enter") { ev.preventDefault(); items.splice(i + 1, 0, { text: "New item", done: false }); items[i].text = node.innerText.trim() || items[i].text; finish(false); save(); }
      } }, opts0));
    });
    var add = el("button", "rs-inline-add", "+ Item"); add.type = "button";
    add.addEventListener("click", function (ev) { ev.stopPropagation(); items.push({ text: "New item", done: false }); save(); });
    card.appendChild(add);
  }

  /* ---------------- floating block toolbar ---------------- */

  var toolbar = null;
  function hideToolbar() { if (toolbar) toolbar.hidden = true; }

  function tbButton(label, title, fn, cls) {
    var b = el("button", "rs-tb-btn" + (cls ? " " + cls : ""), label); b.type = "button";
    b.title = title; b.setAttribute("aria-label", title);
    b.addEventListener("click", function (ev) { ev.stopPropagation(); fn(); });
    return b;
  }

  function showToolbar() {
    var card = selectedNode();
    if (!card || S.editing || (S.sel.kind !== "block" && S.sel.kind !== "section" && S.sel.kind !== "note")) { hideToolbar(); return; }
    if (!toolbar) {
      toolbar = el("div", "rs-toolbar-float"); toolbar.setAttribute("role", "toolbar"); toolbar.setAttribute("aria-label", "Block actions");
      $("[data-canvas]").appendChild(toolbar);
    }
    if (toolbar.parentNode !== $("[data-canvas]")) $("[data-canvas]").appendChild(toolbar);
    toolbar.innerHTML = "";
    var kind = S.sel.kind, id = S.sel.id;
    if (kind === "note") {
      toolbar.appendChild(el("span", "rs-tb-label", "Private note"));
      toolbar.appendChild(tbButton("\u2191", "Move up", function () { moveNote(id, -1); }));
      toolbar.appendChild(tbButton("\u2193", "Move down", function () { moveNote(id, 1); }));
      var n = privateNote(id);
      toolbar.appendChild(tbButton(n && n.block.export ? "\u2399" : "\u2399\u0338", n && n.block.export ? "Included in your exports — click to exclude" : "Not exported — click to include in your exports",
        function () { saveNote(id, { export: !(n && n.block.export) }); }, n && n.block.export ? "on" : ""));
      if (canEdit) toolbar.appendChild(tbButton("Share", "Make shared — everyone with access will see it", function () { shareNote(id); }));
      toolbar.appendChild(tbButton("\u2715", "Delete note", function () { deleteNote(id); }, "danger"));
    } else if (canEdit && kind === "block") {
      var f = findBlock(id); if (!f) { hideToolbar(); return; }
      var idx = f.section.blocks.map(function (x) { return x.id; }).indexOf(id);
      toolbar.appendChild(el("span", "rs-tb-label", blockLabel(f.block)));
      var up = tbButton("\u2191", "Move up (Alt+\u2191)", function () { moveSel(-1); }); up.disabled = idx === 0 && sectionIndex(f.section.id) === 0;
      var dn = tbButton("\u2193", "Move down (Alt+\u2193)", function () { moveSel(1); });
      dn.disabled = idx === f.section.blocks.length - 1 && sectionIndex(f.section.id) === S.layout.sections.length - 1;
      toolbar.appendChild(up); toolbar.appendChild(dn);
      var widths = [["full", "Full"], ["two_thirds", "\u2154"], ["half", "\u00BD"], ["third", "\u2153"]];
      var ws = el("select", "rs-tb-select"); ws.setAttribute("aria-label", "Width"); ws.title = "Width";
      widths.forEach(function (w) { var o = el("option", null, w[1]); o.value = w[0]; ws.appendChild(o); });
      ws.value = f.block.width || "full";
      ws.addEventListener("click", function (ev) { ev.stopPropagation(); });
      ws.addEventListener("change", function () { upd(id, { width: ws.value }).catch(function () {}); });
      toolbar.appendChild(ws);
      toolbar.appendChild(tbButton(f.block.visible === false ? "\u25CC" : "\u25C9", f.block.visible === false ? "Show (H)" : "Hide (H)", function () { toggleHidden(); }));
      if (FREE_TYPES.indexOf(f.block.type) !== -1) {
        var ex = f.block.export !== false;
        toolbar.appendChild(tbButton(ex ? "\u2399" : "\u2399\u0338", ex ? "In exports — click to keep it on screen only" : "Screen only — click to include in exports",
          function () { upd(id, { export: ex ? false : null }).catch(function () {}); }, ex ? "on" : ""));
      }
      toolbar.appendChild(tbButton("\u29C9", "Duplicate (Ctrl+D)", duplicateSel));
      if (f.block.type === "note") toolbar.appendChild(tbButton("\uD83D\uDD12", "Make private — only you will see it", function () { privatiseBlock(id); }));
      toolbar.appendChild(tbButton("\u2715", "Remove (Delete)", removeSel, "danger"));
    } else if (canEdit && kind === "section") {
      var s = findSection(id); if (!s) { hideToolbar(); return; }
      var si = sectionIndex(id);
      toolbar.appendChild(el("span", "rs-tb-label", s.kind === "notes" ? "Notes section" : "Section"));
      var su = tbButton("\u2191", "Move section up", function () { moveSel(-1); }); su.disabled = si === 0;
      var sd = tbButton("\u2193", "Move section down", function () { moveSel(1); }); sd.disabled = si === S.layout.sections.length - 1;
      toolbar.appendChild(su); toolbar.appendChild(sd);
      toolbar.appendChild(tbButton(s.visible === false ? "\u25CC" : "\u25C9", s.visible === false ? "Show section" : "Hide section", toggleHidden));
      toolbar.appendChild(tbButton("+", "Add a block at the end", function () { openAddMenu(toolbar, s, (s.blocks || []).length); }));
      toolbar.appendChild(tbButton("\u2715", "Remove section", removeSel, "danger"));
    } else { hideToolbar(); return; }
    toolbar.hidden = false;
    positionToolbar();
  }

  function positionToolbar() {
    var card = selectedNode();
    if (!toolbar || toolbar.hidden || !card) return;
    var canvas = $("[data-canvas]");
    var cr = canvas.getBoundingClientRect(), r = card.getBoundingClientRect();
    var top = r.top - cr.top + canvas.scrollTop - toolbar.offsetHeight - 8;
    if (top < canvas.scrollTop + 4) top = r.bottom - cr.top + canvas.scrollTop + 8;
    var left = Math.min(Math.max(8, r.left - cr.left + canvas.scrollLeft), canvas.scrollWidth - toolbar.offsetWidth - 8);
    toolbar.style.top = top + "px"; toolbar.style.left = left + "px";
  }

  function sectionIndex(id) { return (S.layout.sections || []).map(function (s) { return s.id; }).indexOf(id); }

  function selectedNode() {
    var id = S.sel.id;
    if (S.sel.kind === "block" || S.sel.kind === "note") return root.querySelector('.ovr-block[data-block-id="' + id + '"]');
    if (S.sel.kind === "section") return root.querySelector('.ovr-section[data-section-id="' + id + '"]:not(.ovr-section-cont)');
    return null;
  }

  function markSelection() {
    Array.prototype.forEach.call(root.querySelectorAll(".rs-selected"), function (n) { n.classList.remove("rs-selected"); });
    var t = selectedNode();
    if (t) t.classList.add("rs-selected");
    showToolbar();
  }

  function select(kind, id) {
    S.sel = { kind: kind, id: id };
    if (kind !== S.lastKind) S.tab = "content";
    S.lastKind = kind;
    renderOutline(); renderInspector(); markSelection();
    var t = selectedNode();
    if (t && !S.editing) {
      var cr = $("[data-canvas]").getBoundingClientRect(), r = t.getBoundingClientRect();
      if (r.top < cr.top || r.bottom > cr.bottom) t.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  /* ---------------- block actions ---------------- */

  function moveSel(dir) {
    if (!canEdit) return;
    if (S.sel.kind === "note") { moveNote(S.sel.id, dir); return; }
    if (S.sel.kind === "section") {
      var si = sectionIndex(S.sel.id), to = si + dir;
      if (to < 0 || to >= S.layout.sections.length) return;
      applyOps([{ op: "move_section", section_id: S.sel.id, index: to }]).catch(function () {});
      return;
    }
    if (S.sel.kind !== "block") return;
    var f = findBlock(S.sel.id); if (!f) return;
    var ids = f.section.blocks.map(function (x) { return x.id; });
    var i = ids.indexOf(S.sel.id) + dir;
    var op;
    if (i >= 0 && i < ids.length) op = { op: "move_block", block_id: S.sel.id, index: i };
    else {
      // Cross into the neighbouring section (end of the previous / start of the next).
      var ns = S.layout.sections[sectionIndex(f.section.id) + dir];
      if (!ns) return;
      op = { op: "move_block", block_id: S.sel.id, section_id: ns.id, index: dir < 0 ? -1 : 0 };
    }
    applyOps([op]).catch(function () {});
  }

  function toggleHidden() {
    if (!canEdit) return;
    var node = S.sel.kind === "block" ? (findBlock(S.sel.id) || {}).block : S.sel.kind === "section" ? findSection(S.sel.id) : null;
    if (!node) return;
    var hide = node.visible !== false;
    applyOps([{ op: "set_visibility", target_id: node.id, visible: !hide }]).then(function () {
      if (hide && !S.showHidden) notice("Hidden — turn on “Hidden” in the canvas bar to see hidden items.", "ok");
    }).catch(function () {});
  }

  function duplicateSel() {
    if (!canEdit || S.sel.kind !== "block") return;
    var f = findBlock(S.sel.id); if (!f) return;
    var b = f.block, s = f.section;
    var copy = clone(b); delete copy.id; delete copy.author; delete copy.updated_at;
    if (copy.title) copy.title = copy.title + " (copy)";
    var idx = s.blocks.map(function (x) { return x.id; }).indexOf(b.id) + 1;
    applyOps([{ op: "add_block", section_id: s.id, index: idx, block: copy }]).then(function () { select("block", findSection(s.id).blocks[idx].id); }).catch(function () {});
  }

  function removeSel() {
    if (S.sel.kind === "note") { deleteNote(S.sel.id); return; }
    if (!canEdit) return;
    if (S.sel.kind === "section") {
      var s = findSection(S.sel.id); if (!s) return;
      if (!window.confirm("Remove the section “" + s.title + "” and its " + (s.blocks || []).length + " block(s)? (Undo restores it.)")) return;
      applyOps([{ op: "remove_section", section_id: s.id }]).then(function () { select("report", null); }).catch(function () {});
    } else if (S.sel.kind === "block") {
      var f = findBlock(S.sel.id); if (!f) return;
      applyOps([{ op: "remove_block", block_id: f.block.id }]).then(function () { select("section", f.section.id); }).catch(function () {});
    }
  }

  /* Add-block menu (popover) anchored at ``anchor``; inserts at ``index``. */
  var addMenu = null;
  function closeAddMenu() { if (addMenu) { addMenu.remove(); addMenu = null; } }

  var FREE_PRESETS = [
    ["text", "Text", "Paragraph (markdown)", function () { return { type: "text", text: "Write here…" }; }],
    ["callout", "Callout", "Highlighted message", function () { return { type: "callout", tone: "info", text: "**Note:** …" }; }],
    ["note", "Note", "Shared note with author and date", function () { return { type: "note", text: "", tone: "note" }; }],
    ["info", "Info", "Labels and values (e.g. Reviewer, Date)", function () { return { type: "info", title: "Details", rows: [{ key: "Reviewer", value: "" }, { key: "Date", value: "" }] }; }],
    ["checklist", "Checklist", "Tick-box action items", function () { return { type: "checklist", title: "Action items", items: [{ text: "First item", done: false }] }; }],
    ["divider", "Divider", "Horizontal rule", function () { return { type: "divider" }; }],
    ["spacer", "Spacer", "Vertical space", function () { return { type: "spacer", size: "md" }; }]
  ];

  function openAddMenu(anchor, s, index) {
    closeAddMenu();
    addMenu = el("div", "rs-addmenu"); addMenu.setAttribute("role", "menu");
    addMenu.addEventListener("click", function (ev) { ev.stopPropagation(); });
    if (canEdit) {
      addMenu.appendChild(el("h4", null, "Free content"));
      var g = el("div", "rs-addmenu-grid");
      FREE_PRESETS.forEach(function (p) {
        var b = el("button", "rs-addmenu-item"); b.type = "button"; b.setAttribute("role", "menuitem");
        b.appendChild(el("span", "rs-addmenu-ico rs-ico-" + p[0]));
        var t = el("span", "rs-addmenu-text"); t.appendChild(el("strong", null, p[1])); t.appendChild(el("span", null, p[2]));
        b.appendChild(t);
        b.addEventListener("click", function () { closeAddMenu(); insertBlock(s, index, p[3]()); });
        g.appendChild(b);
      });
      addMenu.appendChild(g);
    }
    addMenu.appendChild(el("h4", null, "Only you"));
    var pn = el("button", "rs-addmenu-item rs-addmenu-wide"); pn.type = "button"; pn.setAttribute("role", "menuitem");
    pn.appendChild(el("span", "rs-addmenu-ico rs-ico-private"));
    var pt = el("span", "rs-addmenu-text"); pt.appendChild(el("strong", null, "Private note")); pt.appendChild(el("span", null, "Visible to you only, never shared"));
    pn.appendChild(pt);
    pn.addEventListener("click", function () {
      closeAddMenu();
      var after = index > 0 && s.blocks[index - 1] ? s.blocks[index - 1].id : null;
      createNote(s.id, after);
    });
    addMenu.appendChild(pn);
    if (canEdit) {
      var more = el("button", "rs-addmenu-more", "Chart, table or KPI…"); more.type = "button";
      more.addEventListener("click", function () { closeAddMenu(); S.sel = { kind: "new-block", id: s.id, index: index }; renderOutline(); renderInspector(); markSelection(); });
      addMenu.appendChild(more);
    }
    document.body.appendChild(addMenu);
    var r = anchor.getBoundingClientRect();
    var top = r.bottom + window.scrollY + 6, left = r.left + window.scrollX - 8;
    left = Math.min(left, window.scrollX + window.innerWidth - addMenu.offsetWidth - 12);
    if (r.bottom + addMenu.offsetHeight + 12 > window.innerHeight) top = r.top + window.scrollY - addMenu.offsetHeight - 6;
    addMenu.style.top = Math.max(8, top) + "px"; addMenu.style.left = Math.max(8, left) + "px";
    var first = addMenu.querySelector("button"); if (first) first.focus();
  }

  function insertBlock(s, index, block) {
    // ``s`` may be a render-model section; resolve its id against the layout.
    var sec = findSection(s.id); if (!sec) return;
    // Model indices skip hidden blocks in view mode — map to layout order.
    var msec = (S.model.sections.filter(function (x) { return x.id === s.id; })[0] || { blocks: [] });
    var shown = msec.blocks.filter(function (b) { return !b.private; });
    var priorPrivate = msec.blocks.slice(0, Math.min(index, msec.blocks.length)).filter(function (b) { return b.private; }).length;
    var mi = index - priorPrivate;
    var at = mi >= shown.length ? sec.blocks.length
           : sec.blocks.map(function (b) { return b.id; }).indexOf(shown[mi].id);
    if (at < 0) at = sec.blocks.length;
    applyOps([{ op: "add_block", section_id: sec.id, index: at, block: block }]).then(function () {
      var nb = findSection(sec.id).blocks[at];
      if (nb) select("block", nb.id);
    }).catch(function () {});
  }

  /* ---------------- private notes ---------------- */

  function notesPath(id) { return "/reports/" + encodeURIComponent(RID) + "/notes" + (id ? "/" + encodeURIComponent(id) : ""); }
  function loadNotes() {
    return api("GET", notesPath()).then(function (rows) { S.notes = rows || []; }).catch(function () { S.notes = []; });
  }
  function refreshNotes(selectId) {
    return loadNotes().then(function () { return rerender(); }).then(function () {
      renderOutline();
      if (selectId) select("note", selectId); else renderInspector();
    });
  }
  function createNote(sectionId, afterId) {
    if (!S.showPrivate) { S.showPrivate = true; var t = $("[data-toggle-private]"); if (t) t.checked = true; }
    return api("POST", notesPath(), { section_id: sectionId, after_block_id: afterId,
                                      block: { type: "note", text: "", tone: "note", export: false } })
      .then(function (n) { notice("Private note added — only you can see it.", "ok"); return refreshNotes(n.note_id); })
      .catch(function (e) { notice(e.message, "error"); });
  }
  function saveNote(id, set) {
    return api("PUT", notesPath(id), { block: set })
      .then(function () { return refreshNotes(id); })
      .catch(function (e) { notice(e.message, "error"); throw e; });
  }
  function deleteNote(id) {
    if (!window.confirm("Delete this private note? This cannot be undone.")) return;
    api("DELETE", notesPath(id)).then(function () { S.sel = { kind: "report", id: null }; return refreshNotes(null); })
      .catch(function (e) { notice(e.message, "error"); });
  }
  /* Move a private note one slot up / down among the *visible* blocks of its
   * section (anchors are "after block X"). */
  function moveNote(id, dir) {
    var n = privateNote(id); if (!n) return;
    var sec = S.model.sections.filter(function (s) { return s.blocks.some(function (b) { return b.id === id; }); })[0];
    if (!sec) return;
    var ids = sec.blocks.map(function (b) { return b.id; });
    var i = ids.indexOf(id);
    var others = ids.filter(function (x) { return x !== id; });
    var to = Math.max(0, Math.min(others.length, i + dir));
    var after = to === 0 ? null : others[to - 1];
    var sid = findSection(sec.id) ? sec.id : n.section_id;
    if (after && isPrivateId(after)) {
      // Anchor to the shared block that the private neighbour follows.
      var nb = privateNote(after); after = nb ? nb.after_block_id : null;
    }
    api("PUT", notesPath(id), { move: true, section_id: sid, after_block_id: after })
      .then(function () { return refreshNotes(id); }).catch(function (e) { notice(e.message, "error"); });
  }
  function shareNote(id) {
    var n = privateNote(id); if (!n || !canEdit) return;
    if (!window.confirm("Make this note shared? Everyone with access to the report will see it. (It moves into the report; Undo removes it again.)")) return;
    var sec = findSection(n.section_id);
    var block = clone(n.block); delete block.id; delete block.author; delete block.updated_at; block.export = true;
    var at = 0;
    if (sec && n.after_block_id) at = sec.blocks.map(function (b) { return b.id; }).indexOf(n.after_block_id) + 1;
    var ops = sec ? [{ op: "add_block", section_id: sec.id, index: at, block: block }]
                  : [{ op: "add_section", title: "Notes", kind: "notes", section_id: "notes_" + id.slice(3, 9) },
                     { op: "add_block", section_id: "notes_" + id.slice(3, 9), block: block }];
    applyOps(ops).then(function () { return api("DELETE", notesPath(id)); })
      .then(function () { return refreshNotes(null); })
      .then(function () { notice("Note is now shared — press Save to keep it.", "ok"); })
      .catch(function (e) { if (e && e.message) notice(e.message, "error"); });
  }
  function privatiseBlock(id) {
    var f = findBlock(id); if (!f) return;
    if (!window.confirm("Make this note private? It is removed from the shared report (older versions in History still contain it).")) return;
    var idx = f.section.blocks.map(function (b) { return b.id; }).indexOf(id);
    var after = idx > 0 ? f.section.blocks[idx - 1].id : null;
    var block = clone(f.block); delete block.id; delete block.author; delete block.updated_at;
    api("POST", notesPath(), { section_id: f.section.id, after_block_id: after, block: block })
      .then(function (n) {
        return applyOps([{ op: "remove_block", block_id: id }]).then(function () { return refreshNotes(n.note_id); });
      })
      .then(function () { notice("Note is now private — press Save to remove it from the shared report.", "ok"); })
      .catch(function (e) { if (e && e.message) notice(e.message, "error"); });
  }

  /* ---------------- outline ---------------- */

  var drag = null;

  function eyeButton(node, isSection) {
    var b = el("button", "rs-eye" + (node.visible === false ? " off" : ""));
    b.type = "button";
    b.title = node.visible === false ? "Hidden — click to show" : "Shown — click to hide";
    b.setAttribute("aria-pressed", node.visible === false ? "false" : "true");
    b.setAttribute("aria-label", (node.visible === false ? "Show " : "Hide ") + (node.title || node.id));
    b.textContent = node.visible === false ? "\u25CC" : "\u25C9";
    b.disabled = !canEdit;
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      applyOps([{ op: "set_visibility", target_id: node.id, visible: node.visible === false }]).catch(function () {});
    });
    return b;
  }

  function moveButtons(kind, id, index, length) {
    var wrap = el("span", "rs-move");
    [["\u2191", -1, "Move up"], ["\u2193", 1, "Move down"]].forEach(function (d) {
      var b = el("button", "rs-move-btn", d[0]); b.type = "button"; b.title = d[2]; b.setAttribute("aria-label", d[2]);
      var to = index + d[1];
      b.disabled = !canEdit || to < 0 || to >= length;
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var op = kind === "section" ? { op: "move_section", section_id: id, index: to }
                                    : { op: "move_block", block_id: id, index: to };
        applyOps([op]).catch(function () {});
      });
      wrap.appendChild(b);
    });
    return wrap;
  }

  var TYPE_LABEL = { text: "Text", callout: "Callout", note: "Note", info: "Info", checklist: "Checklist",
                     divider: "Divider", spacer: "Spacer" };
  function blockLabel(b) {
    if (b.title) return b.title;
    if (b.type === "chart") { var c = chart(b.chart); return (c ? c.label : b.chart) + (b.dataset ? " · " + b.dataset : ""); }
    if ((b.type === "note" || b.type === "text") && b.text) return String(b.text).replace(/[#*_`>\-]/g, "").trim().slice(0, 40) || TYPE_LABEL[b.type];
    return TYPE_LABEL[b.type] || b.type;
  }

  function privateIn(sectionId) {
    var sec = (S.model && S.model.sections || []).filter(function (s) { return s.id === sectionId; })[0];
    return sec ? sec.blocks.filter(function (b) { return b.private; }) : [];
  }

  function noteRow(b) {
    var li = el("li", "rs-node rs-node-block rs-node-private");
    var row = el("div", "rs-row" + (S.sel.kind === "note" && S.sel.id === b.id ? " active" : ""));
    row.appendChild(el("span", "rs-lock", "\uD83D\uDD12"));
    row.appendChild(el("span", "rs-kind", "private"));
    row.appendChild(el("span", "rs-label", b.title || (b.markdown || "").slice(0, 40) || "Private note"));
    row.addEventListener("click", function () { select("note", b.id); });
    li.appendChild(row);
    return li;
  }

  function renderOutline() {
    var ol = $("[data-outline]");
    ol.innerHTML = "";
    var rb = $("[data-select-report]");
    rb.classList.toggle("active", S.sel.kind === "report");
    var sections = S.layout.sections || [];
    if (!sections.length) ol.appendChild(el("li", "note small", "No sections yet."));
    sections.forEach(function (s, si) {
      var li = el("li", "rs-node rs-node-section" + (s.visible === false ? " rs-off" : "") + (s.kind === "notes" ? " rs-node-notes" : ""));
      li.setAttribute("data-kind", "section"); li.setAttribute("data-id", s.id);
      var row = el("div", "rs-row" + (S.sel.kind === "section" && S.sel.id === s.id ? " active" : ""));
      row.draggable = canEdit;
      row.appendChild(el("span", "rs-grip", "\u2807"));
      if (s.kind === "notes") row.appendChild(el("span", "rs-kind", "notes"));
      row.appendChild(el("span", "rs-label", s.title));
      row.appendChild(moveButtons("section", s.id, si, sections.length));
      row.appendChild(eyeButton(s, true));
      row.addEventListener("click", function () { select("section", s.id); });
      wireDrag(row, "section", s.id, null);
      li.appendChild(row);
      var bl = el("ol", "rs-tree-blocks");
      var priv = S.showPrivate ? privateIn(s.id) : [];
      (s.blocks || []).forEach(function (b, bi) {
        priv.filter(function (p) { var n = privateNote(p.id); return n && (n.after_block_id || null) === (bi === 0 ? null : s.blocks[bi - 1].id); })
          .forEach(function (p) { bl.appendChild(noteRow(p)); });
        var bli = el("li", "rs-node rs-node-block" + (b.visible === false ? " rs-off" : "") + (b.export === false ? " rs-noexport" : ""));
        var brow = el("div", "rs-row" + (S.sel.kind === "block" && S.sel.id === b.id ? " active" : ""));
        brow.draggable = canEdit;
        brow.appendChild(el("span", "rs-grip", "\u2807"));
        brow.appendChild(el("span", "rs-kind", b.type === "chart" ? (b.chart || "chart") : b.type));
        brow.appendChild(el("span", "rs-label", blockLabel(b)));
        brow.appendChild(moveButtons("block", b.id, bi, s.blocks.length));
        brow.appendChild(eyeButton(b, false));
        brow.addEventListener("click", function () { select("block", b.id); });
        wireDrag(brow, "block", b.id, s.id);
        bli.appendChild(brow);
        bl.appendChild(bli);
      });
      var last = (s.blocks || []).length ? s.blocks[s.blocks.length - 1].id : null;
      priv.filter(function (p) { var n = privateNote(p.id); return n && (n.after_block_id || null) === last && (last !== null || !(s.blocks || []).length); })
        .forEach(function (p) { bl.appendChild(noteRow(p)); });
      var add = el("li", "rs-add-block");
      var ab = el("button", "rs-add-btn", "+ Add"); ab.type = "button";
      ab.addEventListener("click", function (ev) { ev.stopPropagation(); openAddMenu(ab, s, (s.blocks || []).length); });
      add.appendChild(ab); bl.appendChild(add);
      li.appendChild(bl);
      ol.appendChild(li);
    });
    // Private notes whose section no longer exists ("My notes" tray).
    var trays = (S.model && S.model.sections || []).filter(function (s) { return s.private; });
    trays.forEach(function (s) {
      var li = el("li", "rs-node rs-node-section rs-node-notes");
      var row = el("div", "rs-row");
      row.appendChild(el("span", "rs-lock", "\uD83D\uDD12"));
      row.appendChild(el("span", "rs-label", "Unplaced notes"));
      li.appendChild(row);
      var bl = el("ol", "rs-tree-blocks");
      s.blocks.forEach(function (b) { bl.appendChild(noteRow(b)); });
      li.appendChild(bl); ol.appendChild(li);
    });
  }

  function wireDrag(row, kind, id, sectionId) {
    if (!canEdit) return;
    row.addEventListener("dragstart", function (ev) {
      drag = { kind: kind, id: id, section: sectionId };
      ev.dataTransfer.effectAllowed = "move";
      try { ev.dataTransfer.setData("text/plain", id); } catch (e) { /* ignore */ }
      row.classList.add("rs-dragging");
    });
    row.addEventListener("dragend", function () { row.classList.remove("rs-dragging"); drag = null; clearDrop(); });
    row.addEventListener("dragover", function (ev) {
      if (!drag) return;
      if (drag.kind === "section" && kind !== "section") return;
      ev.preventDefault();
      clearDrop();
      var r = row.getBoundingClientRect();
      row.classList.add(ev.clientY < r.top + r.height / 2 ? "rs-drop-before" : "rs-drop-after");
    });
    row.addEventListener("dragleave", function () { row.classList.remove("rs-drop-before", "rs-drop-after"); });
    row.addEventListener("drop", function (ev) {
      if (!drag) return;
      ev.preventDefault();
      var after = row.classList.contains("rs-drop-after");
      clearDrop();
      var op = null;
      if (drag.kind === "section" && kind === "section") {
        var order = S.layout.sections.map(function (s) { return s.id; });
        var from = order.indexOf(drag.id), to = order.indexOf(id) + (after ? 1 : 0);
        if (from < to) to -= 1;
        if (from !== to) op = { op: "move_section", section_id: drag.id, index: to };
      } else if (drag.kind === "block") {
        if (kind === "section") {
          op = { op: "move_block", block_id: drag.id, section_id: id, index: after ? -1 : 0 };
        } else {
          var sec = findSection(sectionId);
          var ids = sec.blocks.map(function (b) { return b.id; });
          var t = ids.indexOf(id) + (after ? 1 : 0);
          if (drag.section === sectionId) {
            var f = ids.indexOf(drag.id);
            if (f < t) t -= 1;
            if (f === t) t = null;
          }
          if (t !== null) op = { op: "move_block", block_id: drag.id, section_id: sectionId, index: t };
        }
      }
      drag = null;
      if (op) applyOps([op]).catch(function () {});
    });
  }
  function clearDrop() {
    Array.prototype.forEach.call(root.querySelectorAll(".rs-drop-before, .rs-drop-after"), function (n) {
      n.classList.remove("rs-drop-before", "rs-drop-after");
    });
  }

  /* ---------------- inspector ---------------- */

  function field(label, input, hint) {
    var w = el("label", "rs-field");
    var lab = el("span", "rs-field-label", label);
    // UI rule U3: the field's description hangs off a "?" tip.
    if (hint && window.Ovid2HelpTip) { lab.appendChild(document.createTextNode(" ")); lab.appendChild(window.Ovid2HelpTip.tip(hint)); }
    w.appendChild(lab);
    w.appendChild(input);
    return w;
  }
  function textInput(value, onCommit, opts) {
    opts = opts || {};
    var i = el(opts.multiline ? "textarea" : "input");
    if (!opts.multiline) i.type = opts.type || "text";
    if (opts.multiline) i.rows = opts.rows || 4;
    i.value = value == null ? "" : value;
    if (opts.max) i.maxLength = opts.max;
    if (opts.placeholder) i.placeholder = opts.placeholder;
    i.disabled = !canEdit;
    i.addEventListener("change", function () { onCommit(i.value); });
    return i;
  }
  function selectInput(options, value, onChange, allowEmpty) {
    var s = el("select");
    if (allowEmpty) { var o0 = el("option", null, allowEmpty); o0.value = ""; s.appendChild(o0); }
    options.forEach(function (o) {
      var opt = el("option", null, o.label != null ? o.label : String(o.value));
      opt.value = o.value; s.appendChild(opt);
    });
    s.value = value == null ? "" : String(value);
    s.disabled = !canEdit;
    s.addEventListener("change", function () { onChange(s.value); });
    return s;
  }
  function checkInput(value, onChange) {
    var c = el("input"); c.type = "checkbox"; c.checked = !!value; c.disabled = !canEdit;
    c.addEventListener("change", function () { onChange(c.checked); });
    return c;
  }

  /* Tabs: Content / Format (/ Data for charts). */
  function renderTabs(tabs) {
    var bar = $("[data-inspector-tabs]");
    bar.innerHTML = "";
    bar.hidden = !tabs || tabs.length < 2;
    if (bar.hidden) return;
    if (tabs.indexOf(S.tab) === -1) S.tab = tabs[0];
    tabs.forEach(function (t) {
      var b = el("button", "rs-tab", t.charAt(0).toUpperCase() + t.slice(1)); b.type = "button";
      b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(S.tab === t));
      b.addEventListener("click", function () { S.tab = t; renderInspector(); });
      bar.appendChild(b);
    });
  }

  function renderInspector() {
    var box = $("[data-inspector]");
    box.innerHTML = "";
    var t = $("[data-inspector-title]");
    if (S.sel.kind === "section" && findSection(S.sel.id)) {
      var s = findSection(S.sel.id);
      t.textContent = s.kind === "notes" ? "Notes section" : "Section";
      renderTabs(["content", "format"]);
      if (S.tab === "format") formatPanel(box, s, "section"); else inspectSection(box, s);
    } else if (S.sel.kind === "block" && findBlock(S.sel.id)) {
      var f = findBlock(S.sel.id);
      t.textContent = f.block.type === "chart" ? "Chart" : (TYPE_LABEL[f.block.type] || "Block");
      renderTabs(f.block.type === "chart" ? ["content", "format", "data"] : ["content", "format"]);
      if (S.tab === "format") formatPanel(box, f.block, "block");
      else if (S.tab === "data") inspectData(box, f);
      else inspectBlock(box, f);
    } else if (S.sel.kind === "note" && privateNote(S.sel.id)) {
      t.textContent = "Private note"; renderTabs(null); inspectNote(box, privateNote(S.sel.id));
    } else if (S.sel.kind === "new-block" && findSection(S.sel.id)) {
      t.textContent = "Add a block"; renderTabs(null); gallery(box, findSection(S.sel.id), null);
    } else { S.sel = { kind: "report", id: null }; t.textContent = "Report"; renderTabs(null); inspectReport(box); }
  }

  /* ---------------- format panel ---------------- */

  var COLOR_ROLES = [["accent", "Accent"], ["text", "Text"], ["muted", "Muted"], ["surface", "Surface"],
                     ["background", "Paper"], ["border", "Border"]];

  function currentTheme() { return theme(S.layout.theme || S.catalogue.default_theme) || (S.catalogue.themes || [])[0] || { colors: {}, palette: [], status: {} }; }

  function resolveRef(ref) {
    var th = currentTheme();
    if (!ref) return null;
    if (ref.charAt(0) === "#") return ref;
    if (ref.indexOf("palette:") === 0) { var p = th.palette || []; return p.length ? p[parseInt(ref.slice(8), 10) % p.length] : null; }
    if (ref.indexOf("status:") === 0) return (th.status || {})[ref.slice(7)] || null;
    return (th.colors || {})[ref] || null;
  }

  function refLabel(ref) {
    if (!ref) return "";
    if (ref.charAt(0) === "#") return ref + " (fixed)";
    if (ref.indexOf("palette:") === 0) return "Palette " + (parseInt(ref.slice(8), 10) + 1);
    if (ref.indexOf("status:") === 0) return "Status · " + ref.slice(7).replace(/_/g, " ");
    var hit = COLOR_ROLES.filter(function (r) { return r[0] === ref; })[0];
    return hit ? "Theme " + hit[1].toLowerCase() : ref;
  }

  /* One colour row: current swatch + token, a picker of theme swatches
   * (palette / roles / status) or a custom hex, and a reset. */
  function colorField(label, key, style, inherit, onSet, hint) {
    var row = el("div", "rs-color-row");
    var head = el("div", "rs-color-head");
    var lab = el("span", "rs-field-label", label);
    if (hint && window.Ovid2HelpTip) { lab.appendChild(document.createTextNode(" ")); lab.appendChild(window.Ovid2HelpTip.tip(hint)); }
    head.appendChild(lab);
    var ref = style[key];
    var cur = el("span", "rs-color-cur");
    var sw = el("span", "rs-swatch-dot"); sw.style.background = resolveRef(ref) || inherit.hex || "transparent";
    cur.appendChild(sw);
    cur.appendChild(el("span", "rs-color-token", ref ? refLabel(ref) : "Theme default" + (inherit.label ? " · " + inherit.label : "")));
    head.appendChild(cur);
    if (ref && canEdit) {
      var rs = el("button", "rs-reset", "\u21BA"); rs.type = "button"; rs.title = "Reset to theme default"; rs.setAttribute("aria-label", "Reset " + label);
      rs.addEventListener("click", function () { var o = {}; o[key] = null; onSet(o); });
      head.appendChild(rs);
    }
    row.appendChild(head);
    if (!canEdit) return row;
    var th = currentTheme();
    var grid = el("div", "rs-swatch-grid"); grid.setAttribute("role", "radiogroup"); grid.setAttribute("aria-label", label);
    function opt(value, hex, title) {
      var b = el("button", "rs-swatch-pick" + (ref === value ? " active" : "")); b.type = "button";
      b.style.background = hex; b.title = title; b.setAttribute("aria-label", title);
      b.setAttribute("role", "radio"); b.setAttribute("aria-checked", String(ref === value));
      b.addEventListener("click", function () { var o = {}; o[key] = value; onSet(o); });
      grid.appendChild(b);
    }
    COLOR_ROLES.forEach(function (r) { if ((th.colors || {})[r[0]]) opt(r[0], th.colors[r[0]], "Theme " + r[1].toLowerCase()); });
    (th.palette || []).slice(0, 10).forEach(function (hex, i) { opt("palette:" + i, hex, "Palette " + (i + 1)); });
    ["passed", "warning", "exception"].forEach(function (k) { if ((th.status || {})[k]) opt("status:" + k, th.status[k], "Status · " + k); });
    var custom = el("label", "rs-swatch-custom"); custom.title = "Custom colour (fixed — does not follow theme changes)";
    var inp = el("input"); inp.type = "color"; inp.value = (ref && ref.charAt(0) === "#") ? ref : (resolveRef(ref) || inherit.hex || "#1f6feb");
    inp.setAttribute("aria-label", label + " custom colour");
    inp.addEventListener("change", function () { var o = {}; o[key] = inp.value.toLowerCase(); onSet(o); });
    custom.appendChild(inp);
    grid.appendChild(custom);
    row.appendChild(grid);
    return row;
  }

  function segmented(label, key, options, style, onSet, dflt) {
    var w = el("div", "rs-field");
    var head = el("div", "rs-color-head");
    head.appendChild(el("span", "rs-field-label", label));
    if (style[key] && canEdit) {
      var rs = el("button", "rs-reset", "\u21BA"); rs.type = "button"; rs.title = "Reset to theme default"; rs.setAttribute("aria-label", "Reset " + label);
      rs.addEventListener("click", function () { var o = {}; o[key] = null; onSet(o); });
      head.appendChild(rs);
    }
    w.appendChild(head);
    var seg = el("div", "rs-seg rs-seg-sm"); seg.setAttribute("role", "radiogroup"); seg.setAttribute("aria-label", label);
    var cur = style[key] || dflt;
    options.forEach(function (o) {
      var b = el("button", null, o[1]); b.type = "button"; b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(cur === o[0])); b.disabled = !canEdit;
      if (o[2]) b.title = o[2];
      b.addEventListener("click", function () { var x = {}; x[key] = o[0] === dflt && !style[key] ? null : o[0]; onSet(x); });
      seg.appendChild(b);
    });
    w.appendChild(seg);
    return w;
  }

  function formatPanel(box, node, kind) {
    var th = currentTheme();
    var style = node.style || {};
    var c = th.colors || {};
    function onSet(o) {
      var op = kind === "section" ? { op: "update_section", section_id: node.id, set: { style: o }, merge_style: true }
                                  : { op: "update_block", block_id: node.id, set: { style: o }, merge_style: true };
      return applyOps([op]).then(function () { renderInspector(); }).catch(function () {});
    }
    // Effective formatting summary (what the selection looks like now).
    var eff = el("div", "rs-effective");
    eff.appendChild(el("h3", "rs-group-title", "Effective formatting"));
    var fonts = th.fonts || {}, sizes = th.sizes || {};
    var scale = { sm: 0.88, md: 1, lg: 1.18 }[style.font_scale || "md"];
    var dl = el("dl", "rs-eff-list");
    function row(k, v, hex) {
      dl.appendChild(el("dt", null, k));
      var dd = el("dd");
      if (hex) { var d = el("span", "rs-swatch-dot"); d.style.background = hex; dd.appendChild(d); }
      dd.appendChild(document.createTextNode(v));
      dl.appendChild(dd);
    }
    row("Theme", th.label || th.name || "");
    row("Font", String(fonts[kind === "section" ? "heading" : "body"] || "").split(",")[0].replace(/['"]/g, ""));
    row("Size", Math.round((kind === "section" ? sizes.heading || 15 : sizes.base || 11) * scale * 10) / 10 + " pt");
    row("Text", resolveRef(style.text_color) || c.text || "", resolveRef(style.text_color) || c.text);
    row("Accent", resolveRef(style.accent) || c.accent || "", resolveRef(style.accent) || c.accent);
    row("Fill", resolveRef(style.background) || (style.emphasis === "filled" ? c.surface : "none"), resolveRef(style.background) || (style.emphasis === "filled" ? c.surface : null));
    eff.appendChild(dl);
    box.appendChild(eff);

    box.appendChild(el("h3", "rs-group-title", "Colour"));
    box.appendChild(colorField("Text colour", "text_color", style, { hex: c.text, label: "text" }, onSet,
      "Theme colours follow the report theme when you switch it; a custom colour stays fixed."));
    box.appendChild(colorField("Accent", "accent", style, { hex: c.accent, label: "accent" }, onSet,
      "Used for chart marks, the callout / note edge, outlines, dividers and ticked boxes."));
    box.appendChild(colorField("Background", "background", style, { hex: null, label: "none" }, onSet));

    box.appendChild(el("h3", "rs-group-title", "Text"));
    box.appendChild(segmented("Alignment", "align", [["left", "Left"], ["center", "Centre"], ["right", "Right"]], style, onSet, "left"));
    box.appendChild(segmented("Size", "font_scale", [["sm", "Small"], ["md", "Normal"], ["lg", "Large"]], style, onSet, "md"));

    if (kind === "block") {
      box.appendChild(el("h3", "rs-group-title", "Box"));
      box.appendChild(segmented("Emphasis", "emphasis", [["plain", "Plain"], ["card", "Card"], ["outlined", "Outline"], ["filled", "Filled"]], style, onSet, "plain"));
      box.appendChild(segmented("Border", "border", [["none", "None"], ["subtle", "Subtle"], ["strong", "Strong"]], style, onSet, ""));
      box.appendChild(segmented("Padding", "padding", [["compact", "Compact"], ["normal", "Normal"], ["roomy", "Roomy"]], style, onSet, "normal"));
    }
    if (Object.keys(style).length && canEdit) {
      var acts = el("div", "rs-actions");
      var clr = el("button", "button small", "Reset all formatting"); clr.type = "button";
      clr.addEventListener("click", function () {
        var op = kind === "section" ? { op: "update_section", section_id: node.id, set: { style: null } }
                                    : { op: "update_block", block_id: node.id, set: { style: null } };
        applyOps([op]).then(function () { renderInspector(); }).catch(function () {});
      });
      acts.appendChild(clr); box.appendChild(acts);
    }
  }

  function inspectNote(box, n) {
    var b = n.block;
    var badge = el("p", "rs-private-banner", "\uD83D\uDD12 Only you can see this note.");
    box.appendChild(badge);
    function set(o) { return saveNote(n.note_id, o).catch(function () {}); }
    box.appendChild(field("Title", textInputAny(b.title, function (v) { set({ title: v || null }); }, { max: 200 })));
    box.appendChild(field("Colour", selectAny(["note", "info", "success", "warning", "danger"].map(function (x) { return { value: x, label: x.charAt(0).toUpperCase() + x.slice(1) }; }),
      b.tone || "note", function (v) { set({ tone: v }); })));
    box.appendChild(field("Text (markdown)", textInputAny(b.text, function (v) { set({ text: v }); }, { multiline: true, rows: 8, max: 20000 })));
    var ex = el("label", "rs-check"); ex.appendChild(checkAny(!!b.export, function (v) { set({ export: v }); }));
    ex.appendChild(document.createTextNode(" Include in my exports"));
    box.appendChild(ex);
    var acts = el("div", "rs-actions");
    if (canEdit) {
      var sh = el("button", "button small", "Make shared…"); sh.type = "button";
      sh.addEventListener("click", function () { shareNote(n.note_id); });
      acts.appendChild(sh);
    }
    var del = el("button", "button small danger", "Delete"); del.type = "button";
    del.addEventListener("click", function () { deleteNote(n.note_id); });
    acts.appendChild(del);
    box.appendChild(acts);
  }

  /* Inputs that stay enabled for viewers (private notes are theirs). */
  function textInputAny(value, onCommit, opts) { var i = textInput(value, onCommit, opts); i.disabled = false; return i; }
  function selectAny(options, value, onChange) { var s = selectInput(options, value, onChange); s.disabled = false; return s; }
  function checkAny(value, onChange) { var c = checkInput(value, onChange); c.disabled = false; return c; }

  function inspectReport(box) {
    var L = S.layout;
    function set(k, v) { var o = {}; o[k] = v; return applyOps([{ op: "set_report", set: o }]).catch(function () {}); }
    box.appendChild(field("Subtitle", textInput(L.subtitle, function (v) { set("subtitle", v); }, { max: 300 })));
    box.appendChild(field("Introduction (markdown)", textInput(L.description, function (v) { set("description", v); }, { multiline: true, max: 4000 })));
    var themes = (S.catalogue.themes || []).map(function (th) { return { value: th.name, label: th.label }; });
    box.appendChild(field("Report theme", selectInput(themes, L.theme || S.catalogue.default_theme, function (v) { set("theme", v); }),
      "Applies to the canvas and every export; the page theme is not used."));
    var swatches = el("div", "rs-theme-swatches");
    (S.catalogue.themes || []).forEach(function (th) {
      var b = el("button", "rs-swatch" + ((L.theme || S.catalogue.default_theme) === th.name ? " active" : ""));
      b.type = "button"; b.title = th.label + " — " + th.description; b.disabled = !canEdit;
      b.style.background = th.colors.background; b.style.borderColor = th.colors.border;
      (th.palette || []).slice(0, 4).forEach(function (c) { var d = el("span"); d.style.background = c; b.appendChild(d); });
      b.addEventListener("click", function () { set("theme", th.name); });
      swatches.appendChild(b);
    });
    box.appendChild(swatches);
    var page = L.page || {};
    box.appendChild(field("Page size", selectInput([{ value: "letter", label: "US Letter" }, { value: "a4", label: "A4" }, { value: "legal", label: "US Legal" }],
      page.size || "letter", function (v) { set("page", { size: v }); })));
    box.appendChild(field("Orientation", selectInput([{ value: "portrait", label: "Portrait" }, { value: "landscape", label: "Landscape" }],
      page.orientation || "portrait", function (v) { set("page", { orientation: v }); })));
    var opt = L.options || {};
    var row = el("div", "rs-checks");
    var toc = el("label", "rs-check"); toc.appendChild(checkInput(opt.show_toc, function (v) { set("options", { show_toc: v }); })); toc.appendChild(document.createTextNode(" Table of contents"));
    var prov = el("label", "rs-check"); prov.appendChild(checkInput(opt.show_provenance !== false, function (v) { set("options", { show_provenance: v }); })); prov.appendChild(document.createTextNode(" Provenance footer"));
    row.appendChild(toc); row.appendChild(prov); box.appendChild(row);
    var sc = textInput(opt.small_cell_threshold || 0, function (v) {
      var n = parseInt(v, 10); set("options", { small_cell_threshold: isNaN(n) ? 0 : Math.max(0, Math.min(100, n)) });
    }, { type: "number" });
    sc.min = 0; sc.max = 100;
    box.appendChild(field("Small-cell suppression", sc, "Counts between 1 and this value are shown as suppressed everywhere (0 = off). E.g. 11 for CMS-style rules."));
    var src = el("div", "rs-source-card");
    src.appendChild(el("h3", null, "Source"));
    src.appendChild(el("p", null, S.source ? S.source.title : S.report.source_id));
    src.appendChild(el("p", "mono small note", S.report.source_id));
    if (S.source) {
      var ul = el("ul", "rs-ds-list");
      Object.keys(S.source.datasets || {}).forEach(function (k) {
        var d = S.source.datasets[k];
        ul.appendChild(el("li", null, k + " — " + d.title + " (" + d.row_count + " rows)"));
      });
      src.appendChild(ul);
      if (S.source.row_values_redacted) src.appendChild(el("p", "note small", "Raw source values are masked for your role."));
    }
    box.appendChild(src);
  }

  function inspectSection(box, s) {
    function upd(o) { return applyOps([{ op: "update_section", section_id: s.id, set: o }]).catch(function () {}); }
    box.appendChild(field("Title", textInput(s.title, function (v) { if (v.trim()) upd({ title: v.trim() }); }, { max: 200 })));
    box.appendChild(field("Description (markdown)", textInput(s.description, function (v) { upd({ description: v }); }, { multiline: true, max: 4000 })));
    var row = el("div", "rs-checks");
    var vis = el("label", "rs-check"); vis.appendChild(checkInput(s.visible !== false, function (v) { upd({ visible: v }); })); vis.appendChild(document.createTextNode(" Visible"));
    var pb = el("label", "rs-check"); pb.appendChild(checkInput(s.page_break_before, function (v) { upd({ page_break_before: v }); })); pb.appendChild(document.createTextNode(" Start on a new page"));
    var nk = el("label", "rs-check"); nk.appendChild(checkInput(s.kind === "notes", function (v) { upd({ kind: v ? "notes" : null }); }));
    nk.appendChild(document.createTextNode(" Additional-information section"));
    row.appendChild(vis); row.appendChild(pb); row.appendChild(nk); box.appendChild(row);
    if (canEdit) {
      var acts = el("div", "rs-actions");
      var add = el("button", "button small", "+ Block"); add.type = "button";
      add.addEventListener("click", function () { S.sel = { kind: "new-block", id: s.id }; renderInspector(); });
      var del = el("button", "button small danger", "Remove section"); del.type = "button";
      del.addEventListener("click", function () {
        if (!window.confirm("Remove the section “" + s.title + "” and its " + (s.blocks || []).length + " block(s)? (Undo restores it.)")) return;
        applyOps([{ op: "remove_section", section_id: s.id }]).then(function () { select("report", null); }).catch(function () {});
      });
      acts.appendChild(add); acts.appendChild(del); box.appendChild(acts);
    }
  }

  /* Chart gallery. With ``current`` (a block), picking a kind switches that
   * block's chart; otherwise it adds a new block to ``section``. */
  function gallery(box, section, current) {
    var cats = S.catalogue.categories || {};
    var wrap = el("div", "rs-gallery");
    if (!current) {
      var basics = el("div", "rs-gallery-row");
      FREE_PRESETS.forEach(function (p) {
        var b = el("button", "rs-tile"); b.type = "button"; b.title = p[2];
        b.appendChild(el("span", "rs-tile-name", p[1]));
        b.addEventListener("click", function () { insertAt(section, p[3]()); });
        basics.appendChild(b);
      });
      wrap.appendChild(el("h3", "rs-gallery-cat", "Free content"));
      wrap.appendChild(basics);
    }
    var dsNames = Object.keys((S.source && S.source.datasets) || {});
    Object.keys(cats).forEach(function (cat) {
      var kinds = (S.catalogue.charts || []).filter(function (c) { return c.category === cat; });
      if (!kinds.length) return;
      wrap.appendChild(el("h3", "rs-gallery-cat", cats[cat]));
      var row = el("div", "rs-gallery-row");
      kinds.forEach(function (c) {
        var b = el("button", "rs-tile" + (current && current.chart === c.name ? " active" : "")); b.type = "button";
        b.title = c.summary;
        b.appendChild(el("span", "rs-tile-name", c.label));
        b.addEventListener("click", function () { pickChart(c, section, current, dsNames); });
        row.appendChild(b);
      });
      wrap.appendChild(row);
    });
    box.appendChild(wrap);
  }

  /* Guess params for chart kind ``c`` from dataset ``ds`` columns. */
  function guessParams(c, ds, prev) {
    var props = (c.params && c.params.properties) || {};
    var req = (c.params && c.params.required) || [];
    var cols = (ds && ds.columns) || [];
    var used = {};
    var out = {};
    req.forEach(function (p) {
      var spec = props[p] || {};
      var allowed = spec["x-field"] || spec["x-fields"];
      if (!allowed) return;
      if (prev && prev[p] && cols.some(function (col) { return col.name === prev[p]; })) { out[p] = prev[p]; used[prev[p]] = true; return; }
      var hit = cols.filter(function (col) { return !used[col.name] && (!allowed.length || allowed.indexOf(col.type) !== -1); })[0];
      if (hit) { used[hit.name] = true; out[p] = spec["x-fields"] ? [hit.name] : hit.name; }
    });
    return out;
  }

  function bestDataset(c, names, prefer) {
    var order = prefer ? [prefer].concat(names.filter(function (n) { return n !== prefer; })) : names;
    for (var i = 0; i < order.length; i++) {
      var ds = dataset(order[i]); if (!ds) continue;
      var g = guessParams(c, ds, null);
      var req = (c.params.required || []).filter(function (p) { var s = c.params.properties[p] || {}; return s["x-field"] || s["x-fields"]; });
      if (req.every(function (p) { return p in g; })) return order[i];
    }
    return order[0];
  }

  function pickChart(c, section, current, names) {
    var dsName = bestDataset(c, names, current && current.dataset);
    var params = guessParams(c, dataset(dsName), current && current.params);
    if (current) {
      applyOps([{ op: "update_block", block_id: current.id, set: { type: "chart", chart: c.name, dataset: dsName, params: params } }])
        .then(function () { renderInspector(); }).catch(function () {});
    } else {
      insertAt(section, { type: "chart", chart: c.name, dataset: dsName, params: params, title: c.label });
    }
  }

  /* Insert at the "+" point that opened the gallery (S.sel.index, a
   * render-model index) or at the end of the section. */
  function insertAt(section, block) {
    var idx = S.sel.kind === "new-block" && typeof S.sel.index === "number" ? S.sel.index : Infinity;
    insertBlock(section, idx, block);
  }

  function inspectBlock(box, found) {
    var b = found.block, s = found.section;
    function upd(o, merge) {
      var op = { op: "update_block", block_id: b.id, set: o };
      if (merge) op.merge_params = true;
      return applyOps([op]).then(function () { renderInspector(); }).catch(function () {});
    }
    var layoutAid = b.type === "divider" || b.type === "spacer";
    if (!layoutAid) box.appendChild(field("Title", textInput(b.title, function (v) { upd({ title: v || null }); }, { max: 200 })));
    box.appendChild(field("Width", selectInput([{ value: "full", label: "Full" }, { value: "two_thirds", label: "Two thirds" }, { value: "half", label: "Half" }, { value: "third", label: "Third" }],
      b.width || "full", function (v) { upd({ width: v }); })));
    var checks = el("div", "rs-checks");
    var vis = el("label", "rs-check"); vis.appendChild(checkInput(b.visible !== false, function (v) { upd({ visible: v }); })); vis.appendChild(document.createTextNode(" Visible"));
    checks.appendChild(vis);
    if (FREE_TYPES.indexOf(b.type) !== -1) {
      var ex = el("label", "rs-check"); ex.appendChild(checkInput(b.export !== false, function (v) { upd({ export: v ? null : false }); }));
      ex.appendChild(document.createTextNode(" Include in exports"));
      ex.appendChild(document.createTextNode(" "));
      if (window.Ovid2HelpTip) ex.appendChild(window.Ovid2HelpTip.tip("Off = a working note: shown in the Studio to everyone with access, but left out of PDF / Word / PowerPoint / HTML exports."));
      checks.appendChild(ex);
    }
    box.appendChild(checks);

    var TONES = ["info", "success", "warning", "danger", "note"].map(function (x) { return { value: x, label: x.charAt(0).toUpperCase() + x.slice(1) }; });
    if (b.type === "text" || b.type === "callout" || b.type === "note") {
      if (b.type !== "text") box.appendChild(field(b.type === "note" ? "Colour" : "Tone", selectInput(TONES, b.tone || (b.type === "note" ? "note" : "info"), function (v) { upd({ tone: v }); })));
      if (b.text_ref) box.appendChild(el("p", "note small", "Generated text “" + b.text_ref + "”."));
      box.appendChild(field("Text (markdown)", textInput(b.text, function (v) {
        if (b.type === "note") upd({ text: v });
        else upd(v ? { text: v, text_ref: null } : { text: null });
      }, { multiline: true, rows: 8, max: 20000 }), "You can also click the text on the canvas and type in place."));
      if (b.type === "note" && (b.author || b.updated_at)) {
        box.appendChild(el("p", "note small", "Last edited by " + (b.author || "?") + (b.updated_at ? " · " + new Date(b.updated_at).toLocaleString() : "")));
      }
    } else if (b.type === "info") {
      box.appendChild(rowsEditor(b.rows || [], ["key", "value"], ["Label", "Value"], function (rows) { upd({ rows: rows }); }));
    } else if (b.type === "checklist") {
      box.appendChild(itemsEditor(b.items || [], function (items) { upd({ items: items }); }));
    } else if (b.type === "spacer") {
      box.appendChild(field("Height", selectInput([{ value: "sm", label: "Small" }, { value: "md", label: "Medium" }, { value: "lg", label: "Large" }], b.size || "md", function (v) { upd({ size: v }); })));
    } else if (b.type === "chart") {
      box.appendChild(field("Caption", textInput(b.caption, function (v) { upd({ caption: v || null }); }, { multiline: true, rows: 2, max: 2000 })));
      var c = chart(b.chart);
      var kind = el("p", "rs-kind-row");
      kind.appendChild(el("span", "rs-field-label", "Chart"));
      kind.appendChild(el("strong", null, c ? c.label : b.chart));
      var go = el("button", "button small", "Data & options…"); go.type = "button";
      go.addEventListener("click", function () { S.tab = "data"; renderInspector(); });
      kind.appendChild(go);
      box.appendChild(kind);
      if (c && c.renderer === "vega") {
        var dl = el("div", "rs-actions");
        ["svg", "png"].forEach(function (f) {
          var eb = el("button", "button small", "Download " + f.toUpperCase()); eb.type = "button";
          eb.addEventListener("click", function () { exportAs(f, b.id); });
          dl.appendChild(eb);
        });
        box.appendChild(dl);
      }
    }
    if (canEdit) {
      var acts = el("div", "rs-actions");
      var dup = el("button", "button small", "Duplicate"); dup.type = "button";
      dup.addEventListener("click", duplicateSel);
      var mv = selectInput((S.layout.sections || []).map(function (x) { return { value: x.id, label: "Move to: " + x.title }; }), s.id, function (v) {
        if (v !== s.id) applyOps([{ op: "move_block", block_id: b.id, section_id: v, index: -1 }]).catch(function () {});
      });
      var del = el("button", "button small danger", "Remove"); del.type = "button";
      del.addEventListener("click", removeSel);
      acts.appendChild(dup); acts.appendChild(mv); acts.appendChild(del); box.appendChild(acts);
    }
  }

  function inspectData(box, found) {
    var b = found.block, s = found.section;
    function upd(o, merge) {
      var op = { op: "update_block", block_id: b.id, set: o };
      if (merge) op.merge_params = true;
      return applyOps([op]).then(function () { renderInspector(); }).catch(function () {});
    }
    var c = chart(b.chart);
    var names = Object.keys((S.source && S.source.datasets) || {});
    box.appendChild(field("Dataset", selectInput(names.map(function (n) { var d = dataset(n); return { value: n, label: n + (d ? " — " + d.title : "") }; }), b.dataset, function (v) {
      upd({ dataset: v, params: guessParams(c, dataset(v), b.params), filters: [] });
    })));
    var kindRow = el("div", "rs-kind-row");
    kindRow.appendChild(el("span", "rs-field-label", "Chart"));
    kindRow.appendChild(el("strong", null, c ? c.label : b.chart));
    if (canEdit) {
      var sw = el("button", "button small", "Change…"); sw.type = "button";
      var gbox = el("div");
      sw.addEventListener("click", function () { gbox.innerHTML = ""; gallery(gbox, s, b); sw.hidden = true; });
      kindRow.appendChild(sw);
      box.appendChild(kindRow); box.appendChild(gbox);
    } else box.appendChild(kindRow);
    if (c) box.appendChild(paramsForm(c, b, upd));
    box.appendChild(filtersForm(b, upd));
  }

  /* Small list editors for info rows / checklist items (inspector side). */
  function rowsEditor(rows, keys, labels, onSave) {
    var wrap = el("fieldset", "rs-params");
    wrap.appendChild(el("legend", null, "Rows"));
    var list = clone(rows);
    list.forEach(function (r, i) {
      var line = el("div", "rs-kv-row");
      keys.forEach(function (k, j) {
        var inp = textInput(r[k], function (v) { list[i][k] = v; onSave(list); }, { max: k === "key" ? 200 : 500, placeholder: labels[j] });
        inp.setAttribute("aria-label", labels[j] + " " + (i + 1));
        line.appendChild(inp);
      });
      if (canEdit) {
        var x = el("button", "rs-move-btn", "\u00D7"); x.type = "button"; x.title = "Remove row"; x.setAttribute("aria-label", "Remove row " + (i + 1));
        x.addEventListener("click", function () { list.splice(i, 1); onSave(list); });
        line.appendChild(x);
      }
      wrap.appendChild(line);
    });
    if (canEdit && list.length < 50) {
      var add = el("button", "button small", "+ Row"); add.type = "button";
      add.addEventListener("click", function () { list.push({ key: "Label", value: "" }); onSave(list); });
      wrap.appendChild(add);
    }
    return wrap;
  }

  function itemsEditor(items, onSave) {
    var wrap = el("fieldset", "rs-params");
    wrap.appendChild(el("legend", null, "Items"));
    var list = clone(items);
    list.forEach(function (it, i) {
      var line = el("div", "rs-kv-row");
      var cb = checkInput(it.done, function (v) { list[i].done = v; onSave(list); }); cb.setAttribute("aria-label", "Done " + (i + 1));
      line.appendChild(cb);
      var inp = textInput(it.text, function (v) { list[i].text = v; onSave(list); }, { max: 500 });
      inp.setAttribute("aria-label", "Item " + (i + 1));
      line.appendChild(inp);
      if (canEdit) {
        var x = el("button", "rs-move-btn", "\u00D7"); x.type = "button"; x.title = "Remove item"; x.setAttribute("aria-label", "Remove item " + (i + 1));
        x.addEventListener("click", function () { list.splice(i, 1); onSave(list); });
        line.appendChild(x);
      }
      wrap.appendChild(line);
    });
    if (canEdit && list.length < 100) {
      var add = el("button", "button small", "+ Item"); add.type = "button";
      add.addEventListener("click", function () { list.push({ text: "New item", done: false }); onSave(list); });
      wrap.appendChild(add);
    }
    return wrap;
  }

  function paramsForm(c, b, upd) {
    var wrap = el("fieldset", "rs-params");
    wrap.appendChild(el("legend", null, "Options"));
    var props = (c.params && c.params.properties) || {};
    var req = (c.params && c.params.required) || [];
    var cols = (dataset(b.dataset) || {}).columns || [];
    var p = b.params || {};
    var keys = Object.keys(props).sort(function (a, z) {
      var ra = req.indexOf(a) !== -1, rz = req.indexOf(z) !== -1;
      var fa = !!(props[a]["x-field"] || props[a]["x-fields"]), fz = !!(props[z]["x-field"] || props[z]["x-fields"]);
      return (rz - ra) || (fz - fa);
    });
    function setParam(k, v) {
      var o = {}; o[k] = (v === "" || v === undefined) ? null : v;
      upd({ params: o }, true);
    }
    keys.forEach(function (k) {
      var spec = props[k], label = k.replace(/_/g, " ") + (req.indexOf(k) !== -1 ? " *" : "");
      var allowed = spec["x-field"] || spec["x-fields"];
      var input;
      if (allowed) {
        var opts = cols.filter(function (col) { return !allowed.length || allowed.indexOf(col.type) !== -1; })
          .map(function (col) { return { value: col.name, label: (col.title || col.name) + " (" + col.type + ")" }; });
        if (spec["x-fields"]) {
          input = el("select"); input.multiple = true; input.size = Math.min(6, Math.max(2, opts.length)); input.disabled = !canEdit;
          var cur = Array.isArray(p[k]) ? p[k] : [];
          opts.forEach(function (o) { var op = el("option", null, o.label); op.value = o.value; op.selected = cur.indexOf(o.value) !== -1; input.appendChild(op); });
          input.addEventListener("change", function () {
            var vals = Array.prototype.filter.call(input.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
            setParam(k, vals.length ? vals : null);
          });
        } else {
          input = selectInput(opts, p[k], function (v) { setParam(k, v || null); }, req.indexOf(k) !== -1 ? "— pick a column —" : "— none —");
        }
      } else if (spec.enum) {
        input = selectInput(spec.enum.map(function (v) { return { value: v }; }), p[k] != null ? p[k] : spec["default"], function (v) { setParam(k, v); });
      } else if (spec.type === "boolean") {
        input = checkInput(p[k] != null ? p[k] : spec["default"], function (v) { setParam(k, v); });
      } else if (spec.type === "integer" || spec.type === "number") {
        input = textInput(p[k] != null ? p[k] : "", function (v) {
          if (v === "") return setParam(k, null);
          var n = spec.type === "integer" ? parseInt(v, 10) : parseFloat(v);
          if (!isNaN(n)) setParam(k, n);
        }, { type: "number", placeholder: spec["default"] != null ? String(spec["default"]) : "" });
        if (spec.minimum != null) input.min = spec.minimum;
        if (spec.maximum != null) input.max = spec.maximum;
      } else if (spec.type === "string") {
        input = textInput(p[k], function (v) { setParam(k, v); }, { max: spec.maxLength || 200 });
      } else return;
      var f = field(label, input, spec.description || "");
      if (spec.type === "boolean") f.classList.add("rs-field-inline");
      wrap.appendChild(f);
    });
    return wrap;
  }

  function filtersForm(b, upd) {
    var wrap = el("fieldset", "rs-params");
    wrap.appendChild(el("legend", null, "Filter & top-N"));
    var cols = (dataset(b.dataset) || {}).columns || [];
    var filters = (b.filters || []).slice();
    var list = el("ul", "rs-filters");
    filters.forEach(function (f, i) {
      var li = el("li");
      li.appendChild(el("span", "mono small", f.field + " " + f.op + (f.value !== undefined ? " " + JSON.stringify(f.value) : "")));
      if (canEdit) {
        var x = el("button", "rs-move-btn", "\u00D7"); x.type = "button"; x.title = "Remove filter";
        x.addEventListener("click", function () { filters.splice(i, 1); upd({ filters: filters }); });
        li.appendChild(x);
      }
      list.appendChild(li);
    });
    wrap.appendChild(list);
    if (canEdit) {
      var row = el("div", "rs-filter-add");
      var fsel = el("select"); cols.forEach(function (c) { var o = el("option", null, c.name); o.value = c.name; fsel.appendChild(o); });
      var osel = el("select"); ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "is_null", "not_null"].forEach(function (o) { var op = el("option", null, o); op.value = o; osel.appendChild(op); });
      var val = el("input"); val.type = "text"; val.placeholder = "value (a,b for in)";
      var add = el("button", "button small", "Add"); add.type = "button";
      add.addEventListener("click", function () {
        var col = cols.filter(function (c) { return c.name === fsel.value; })[0] || {};
        var f = { field: fsel.value, op: osel.value };
        if (["is_null", "not_null"].indexOf(osel.value) === -1) {
          var raw = val.value.trim();
          var conv = function (x) { return col.type === "quantitative" && x !== "" && !isNaN(Number(x)) ? Number(x) : x; };
          f.value = ["in", "not_in"].indexOf(osel.value) !== -1 ? raw.split(",").map(function (x) { return conv(x.trim()); }).filter(function (x) { return x !== ""; }) : conv(raw);
        }
        filters.push(f); upd({ filters: filters });
      });
      row.appendChild(fsel); row.appendChild(osel); row.appendChild(val); row.appendChild(add);
      wrap.appendChild(row);
      var qcols = cols.filter(function (c) { return c.type === "quantitative"; });
      if (qcols.length) {
        var top = b.top_n || null;
        var trow = el("div", "rs-filter-add");
        var n = el("input"); n.type = "number"; n.min = 1; n.max = 500; n.placeholder = "Top N"; n.value = top ? top.n : "";
        var by = el("select"); qcols.forEach(function (c) { var o = el("option", null, "by " + c.name); o.value = c.name; by.appendChild(o); });
        if (top) by.value = top.by;
        var ord = el("select"); [["descending", "largest"], ["ascending", "smallest"]].forEach(function (x) { var o = el("option", null, x[1]); o.value = x[0]; ord.appendChild(o); });
        if (top && top.order) ord.value = top.order;
        var apply = el("button", "button small", top ? "Update" : "Keep top N"); apply.type = "button";
        apply.addEventListener("click", function () {
          var k = parseInt(n.value, 10);
          upd({ top_n: isNaN(k) ? null : { n: Math.max(1, Math.min(500, k)), by: by.value, order: ord.value } });
        });
        trow.appendChild(n); trow.appendChild(by); trow.appendChild(ord); trow.appendChild(apply);
        if (top) {
          var clr = el("button", "button small", "Clear"); clr.type = "button";
          clr.addEventListener("click", function () { upd({ top_n: null }); });
          trow.appendChild(clr);
        }
        wrap.appendChild(trow);
      }
    }
    return wrap;
  }

  /* ---------------- export ---------------- */

  function exportAs(fmt, blockId, withNotes) {
    var body = { format: fmt };
    if (blockId) body.block_id = blockId;
    if (dirty()) body.layout = S.layout;
    if (withNotes) body.include_private_notes = true;
    notice("Rendering " + fmt.toUpperCase() + "…", null, true);
    return api("POST", "/reports/" + encodeURIComponent(RID) + "/exports", body).then(function (r) {
      notice("Exported " + r.filename + " (" + Math.max(1, Math.round(r.size / 1024)) + " KB)" + (dirty() ? " — includes unsaved edits." : "."), "ok");
      var a = document.createElement("a");
      a.href = r.download_url; a.download = r.filename; document.body.appendChild(a); a.click(); a.remove();
    }).catch(function (e) { notice("Export failed: " + e.message, "error"); });
  }

  function renderExportMenu() {
    var m = $("[data-export-menu]");
    m.innerHTML = "";
    (S.catalogue.export_formats || []).forEach(function (f) {
      var b = el("button", null); b.type = "button"; b.setAttribute("role", "menuitem");
      b.appendChild(el("strong", null, f.format.toUpperCase()));
      b.appendChild(el("span", "note small", " " + f.label));
      b.addEventListener("click", function () {
        m.hidden = true;
        var exportable = S.notes.filter(function (n) { return n.block.export; }).length;
        if (!exportable || f.format === "json" || f.format === "svg" || f.format === "png") { exportAs(f.format); return; }
        // Ask whether to add the caller's own "in export" private notes.
        var dlg = $("[data-export-dialog]");
        $("[data-export-format]").textContent = f.format.toUpperCase();
        $("[data-export-private]").checked = true;
        $("[data-export-go]").onclick = function () { dlg.close(); exportAs(f.format, null, $("[data-export-private]").checked); };
        dlg.showModal();
      });
      m.appendChild(b);
    });
  }

  /* ---------------- dialogs ---------------- */

  function openVersions() {
    var dlg = $("[data-versions-dialog]"), ol = $("[data-versions-list]");
    ol.innerHTML = "";
    api("GET", "/reports/" + encodeURIComponent(RID) + "/versions").then(function (vs) {
      vs.forEach(function (v) {
        var li = el("li");
        li.appendChild(el("strong", null, "v" + v.version));
        li.appendChild(el("span", "note small", " " + new Date(v.saved_at).toLocaleString() + " · " + v.saved_by));
        if (v.note) li.appendChild(el("div", "small", v.note));
        if (canEdit && v.version !== S.version) {
          var b = el("button", "button small", "Restore"); b.type = "button";
          b.addEventListener("click", function () {
            if (dirty() && !window.confirm("Discard your unsaved edits and restore v" + v.version + "?")) return;
            api("POST", "/reports/" + encodeURIComponent(RID) + "/versions/" + v.version + "/restore", {}).then(function () {
              window.location.reload();
            }).catch(function (e) { notice(e.message, "error"); });
          });
          li.appendChild(b);
        } else if (v.version === S.version) li.appendChild(el("span", "badge status-ok", "current"));
        ol.appendChild(li);
      });
      dlg.showModal();
    }).catch(function (e) { notice(e.message, "error"); });
  }

  function renderShares(rows) {
    var tb = $("[data-share-rows]");
    tb.innerHTML = "";
    if (!rows.length) { var tr = el("tr"); var td = el("td", "note small", "Only you can see this report."); td.colSpan = 3; tr.appendChild(td); tb.appendChild(tr); }
    rows.forEach(function (s) {
      var tr = el("tr");
      tr.appendChild(el("td", null, (s.principal_kind === "role" ? "Role: " : "") + s.principal));
      tr.appendChild(el("td", null, s.permission === "edit" ? "can edit" : "can view"));
      var td = el("td"), x = el("button", "button small", "Remove"); x.type = "button";
      x.addEventListener("click", function () {
        api("DELETE", "/reports/" + encodeURIComponent(RID) + "/shares/" + encodeURIComponent(s.principal_kind) + "/" + encodeURIComponent(s.principal))
          .then(renderShares).catch(function (e) { notice(e.message, "error"); });
      });
      td.appendChild(x); tr.appendChild(td); tb.appendChild(tr);
    });
  }

  function openShare() {
    $("[data-share-title]").textContent = S.report.title;
    api("GET", "/reports/" + encodeURIComponent(RID) + "/shares").then(function (rows) {
      renderShares(rows); $("[data-share-errors]").hidden = true; $("[data-share-dialog]").showModal();
    }).catch(function (e) { notice(e.message, "error"); });
  }

  function formErrors(sel, e) {
    var ul = $(sel); ul.innerHTML = "";
    (e.errors && e.errors.length ? e.errors : [e.message]).forEach(function (m) { ul.appendChild(el("li", null, m)); });
    ul.hidden = false;
  }

  /* ---------------- render all ---------------- */

  function renderAll() { renderOutline(); renderCanvas(); renderInspector(); refreshChrome(); }

  /* ---------------- wiring ---------------- */

  function closeMenus(except) {
    ["[data-export-menu]", "[data-more-menu]"].forEach(function (s) { var m = $(s); if (m !== except) m.hidden = true; });
  }

  root.addEventListener("click", function (ev) {
    var t = ev.target.closest("button");
    if (!t || !root.contains(t)) {
      closeMenus(null);
      // A click on empty canvas (the desk) deselects.
      if (ev.target.closest("[data-canvas]") && !ev.target.closest(".ovr-block, .ovr-section-head, .ovr-head, .rs-toolbar-float")) {
        if (S.sel.kind !== "report") select("report", null);
      }
      return;
    }
    if (t.hasAttribute("data-export-toggle")) { var m = $("[data-export-menu]"); closeMenus(m); m.hidden = !m.hidden; t.setAttribute("aria-expanded", String(!m.hidden)); return; }
    if (t.hasAttribute("data-more-toggle")) { var mm = $("[data-more-menu]"); closeMenus(mm); mm.hidden = !mm.hidden; t.setAttribute("aria-expanded", String(!mm.hidden)); return; }
    closeMenus(null);
    if (t.hasAttribute("data-save")) save();
    else if (t.hasAttribute("data-undo")) undo();
    else if (t.hasAttribute("data-redo")) redo();
    else if (t.hasAttribute("data-select-report")) select("report", null);
    else if (t.hasAttribute("data-view")) setView(t.getAttribute("data-view"));
    else if (t.hasAttribute("data-add-section")) {
      applyOps([{ op: "add_section", title: "New section" }]).then(function () {
        var s = S.layout.sections[S.layout.sections.length - 1]; select("section", s.id);
      }).catch(function () {});
    }
    else if (t.hasAttribute("data-add-notes-section")) {
      var sid = "notes_" + Math.random().toString(16).slice(2, 8);
      applyOps([{ op: "add_section", title: "Notes", kind: "notes", section_id: sid },
                { op: "add_block", section_id: sid, block: { type: "note", text: "", tone: "note" } }]).then(function () {
        select("section", sid);
      }).catch(function () {});
    }
    else if (t.hasAttribute("data-versions")) openVersions();
    else if (t.hasAttribute("data-share")) openShare();
    else if (t.hasAttribute("data-duplicate")) {
      api("POST", "/reports/" + encodeURIComponent(RID) + "/duplicate", {}).then(function (r) {
        window.location.assign("/reports/" + encodeURIComponent(r.report_id));
      }).catch(function (e) { notice(e.message, "error"); });
    }
    else if (t.hasAttribute("data-save-template")) { $("[data-template-errors]").hidden = true; $("[data-template-dialog]").showModal(); }
    else if (t.hasAttribute("data-organise")) { $("[data-organise-errors]").hidden = true; $("[data-organise-dialog]").showModal(); }
    else if (t.hasAttribute("data-archive")) {
      if (!window.confirm("Archive this report? You can restore it from Archived in the library.")) return;
      api("POST", "/reports/" + encodeURIComponent(RID) + "/archive", {}).then(function () { window.location.assign("/reports"); })
        .catch(function (e) { notice(e.message, "error"); });
    }
    else if (t.hasAttribute("data-close")) { var d = t.closest("dialog"); if (d) d.close(); }
  });

  $("[data-title]").addEventListener("change", function (e) {
    var v = e.target.value.trim();
    if (!v) { e.target.value = S.layout.title; return; }
    applyOps([{ op: "set_report", set: { title: v } }]).catch(function () { e.target.value = S.layout.title; });
  });

  $("[data-share-form]").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = ev.target;
    api("PUT", "/reports/" + encodeURIComponent(RID) + "/shares",
        { principal_kind: f.principal_kind.value, principal: activePrincipal().value.trim(), permission: f.permission.value })
      .then(function (rows) { if (activePrincipal().tagName === "INPUT") activePrincipal().value = ""; $("[data-share-errors]").hidden = true; renderShares(rows); })
      .catch(function (e) { formErrors("[data-share-errors]", e); });
  });
  // UI rule U2: role → <select> of roles; user → free id / e-mail.
  function activePrincipal() {
    var kind = $("[data-share-form]").principal_kind.value;
    return $('[data-share-form] [data-principal="' + kind + '"]');
  }
  $("[data-share-form]").principal_kind.addEventListener("change", function (e) {
    Array.prototype.forEach.call(root.querySelectorAll('[data-share-form] [data-principal]'), function (n) {
      var on = n.getAttribute("data-principal") === e.target.value;
      n.hidden = !on; n.disabled = !on;
    });
  });

  $("[data-template-form]").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = ev.target;
    var go = dirty() ? save() : Promise.resolve();
    go.then(function () {
      return api("POST", "/reports/" + encodeURIComponent(RID) + "/save-as-template",
                 { name: f.name.value.trim(), description: f.description.value, shared: f.shared.checked });
    }).then(function (t) { $("[data-template-dialog]").close(); notice("Saved template “" + t.name + "”.", "ok"); })
      .catch(function (e) { formErrors("[data-template-errors]", e); });
  });

  $("[data-organise-form]").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var f = ev.target;
    var tags = f.tags.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean);
    api("PUT", "/reports/" + encodeURIComponent(RID), { folder: f.folder.value.trim(), tags: tags, description: f.description.value })
      .then(function (rep) {
        S.report.folder = rep.folder; S.report.tags = rep.tags;
        $("[data-meta]").textContent = "v" + S.version + " · " + rep.permission + (rep.folder ? " · " + rep.folder : "");
        $("[data-organise-dialog]").close(); notice("Saved.", "ok");
      }).catch(function (e) { formErrors("[data-organise-errors]", e); });
  });

  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) || "";
    if (/INPUT|TEXTAREA|SELECT/.test(tag) || (ev.target && ev.target.isContentEditable)) return;
    if (document.querySelector("dialog[open]")) return;
    var k = ev.key, mod = ev.ctrlKey || ev.metaKey;
    if (mod && k.toLowerCase() === "z") { ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); }
    else if (mod && k.toLowerCase() === "s") { ev.preventDefault(); save(); }
    else if (mod && k.toLowerCase() === "d" && S.sel.kind === "block") { ev.preventDefault(); duplicateSel(); }
    else if (ev.altKey && (k === "ArrowUp" || k === "ArrowDown")) { ev.preventDefault(); moveSel(k === "ArrowUp" ? -1 : 1); }
    else if (!mod && !ev.altKey && (k === "h" || k === "H") && (S.sel.kind === "block" || S.sel.kind === "section")) { ev.preventDefault(); toggleHidden(); }
    else if ((k === "Delete" || k === "Backspace") && (S.sel.kind === "block" || S.sel.kind === "note")) {
      ev.preventDefault();
      if (S.sel.kind === "note" || window.confirm("Remove this block? (Undo restores it.)")) removeSel();
    }
    else if (k === "Escape") { closeAddMenu(); if (S.sel.kind !== "report") select("report", null); }
  });
  document.addEventListener("click", function (ev) {
    if (addMenu && !addMenu.contains(ev.target)) closeAddMenu();
  });
  $("[data-canvas]").addEventListener("scroll", positionToolbar, { passive: true });
  window.addEventListener("resize", function () {
    clearTimeout(S.resizeTimer);
    S.resizeTimer = setTimeout(function () { if (S.view === "pages") renderCanvas(); else { applyZoom(); positionToolbar(); } }, 200);
  });

  /* ---------------- view / zoom / toggles ---------------- */

  function setView(v) {
    S.view = v === "pages" ? "pages" : "continuous";
    try { window.localStorage.setItem(VIEW_KEY, S.view); } catch (e) { /* private mode */ }
    Array.prototype.forEach.call(root.querySelectorAll("[data-view-switch] [data-view]"), function (b) {
      b.setAttribute("aria-checked", String(b.getAttribute("data-view") === S.view));
    });
    renderCanvas();
  }
  var zoomSel = $("[data-zoom]");
  zoomSel.addEventListener("change", function () {
    S.zoom = zoomSel.value;
    try { window.localStorage.setItem(ZOOM_KEY, S.zoom); } catch (e) { /* private mode */ }
    applyZoom(); positionToolbar();
  });
  var hidT = $("[data-toggle-hidden]");
  if (hidT) hidT.addEventListener("change", function () { S.showHidden = hidT.checked; rerender().catch(function (e) { notice(e.message, "error"); }); });
  var privT = $("[data-toggle-private]");
  privT.addEventListener("change", function () {
    S.showPrivate = privT.checked;
    rerender().then(renderOutline).catch(function (e) { notice(e.message, "error"); });
  });
  if (autosaveBox()) {
    autosaveBox().checked = readAutosavePref();
    autosaveBox().addEventListener("change", function (e) {
      setAutosave(e.target.checked);
      if (e.target.checked) { notice("Autosave on — edits are saved a few seconds after you stop.", "ok"); scheduleAutosave(); }
      else notice("Autosave off — press Save (Ctrl+S) to keep your edits.", "ok");
    });
  }
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flushAutosave();
  });
  window.addEventListener("beforeunload", function (ev) {
    if (S.layout && dirty()) { ev.preventDefault(); ev.returnValue = ""; }
  });

  /* ---------------- assistant hook ---------------- */

  window.Ovid2ReportStudio = {
    OPS: OPS.slice(),
    info: function () {
      return S.report ? { report_id: RID, title: S.layout.title, source_id: S.report.source_id,
                          can_edit: canEdit, dirty: dirty(), version: S.version,
                          autosave: autosaveOn() } : null;
    },
    outline: function () {
      if (!S.layout) return null;
      return {
        report_id: RID, source_id: S.report.source_id, title: S.layout.title, theme: S.layout.theme,
        can_edit: canEdit, unsaved_changes: dirty(),
        sections: (S.layout.sections || []).map(function (s) {
          return { id: s.id, title: s.title, visible: s.visible !== false,
                   blocks: (s.blocks || []).map(function (b) {
                     return { id: b.id, type: b.type, chart: b.chart || null, dataset: b.dataset || null,
                              title: b.title || null, visible: b.visible !== false, export: b.export !== false };
                   }), kind: s.kind || "content" };
        })
      };
    },
    applyOps: function (ops) {
      return applyOps(ops).then(function (notes) { S.agentEdits = true; refreshChrome(); return notes; });
    },
    undo: undo
  };

  /* ---------------- boot ---------------- */

  try {
    S.view = window.localStorage.getItem(VIEW_KEY) === "pages" ? "pages" : "continuous";
    S.zoom = window.localStorage.getItem(ZOOM_KEY) || "1";
  } catch (e) { /* private mode */ }
  zoomSel.value = S.zoom;
  Array.prototype.forEach.call(root.querySelectorAll("[data-view-switch] [data-view]"), function (b) {
    b.setAttribute("aria-checked", String(b.getAttribute("data-view") === S.view));
  });

  Promise.all([
    api("GET", "/reports/" + encodeURIComponent(RID)),
    api("GET", "/report-catalogue"),
    api("GET", "/reports/" + encodeURIComponent(RID) + "/render?mode=view&private_notes=true"),
    loadNotes()
  ]).then(function (r) {
    S.report = r[0]; S.catalogue = r[1]; S.model = r[2];
    S.layout = clone(r[0].layout); S.saved = clone(r[0].layout); S.version = r[0].version;
    renderExportMenu();
    renderAll();
    return api("GET", "/report-sources/" + encodeURIComponent(S.report.source_id)).then(function (src) {
      S.source = src; renderInspector();
    }).catch(function () { /* source description is optional for viewing */ });
  }).catch(function (e) {
    $("[data-canvas]").textContent = "Could not load this report: " + e.message;
  });
})();
