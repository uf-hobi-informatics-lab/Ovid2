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
 * never reads the page theme for the canvas.
 *
 * window.Ovid2ReportStudio.{outline, applyOps, info} is the hook the
 * assistant's edit_report_layout UI action uses (llm_panel.js): the ops are
 * re-validated by the same server endpoint and are undoable here.
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

  var S = {
    report: null, layout: null, saved: null, model: null, catalogue: null, source: null,
    undo: [], redo: [], sel: { kind: "report", id: null }, busy: false, version: 0,
    // True once the assistant edited the working copy: autosave then waits
    // for a human Save (the assistant never saves on its own).
    agentEdits: false
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
               { ops: ops, save: false, render: true, layout: S.layout })
      .then(function (r) {
        S.undo.push(clone(S.layout));
        if (S.undo.length > 100) S.undo.shift();
        S.redo = [];
        S.layout = r.layout; S.model = r.model;
        showErrors([]);
        renderAll();
        if (opts.select) S.sel = opts.select;
        return r.notes || [];
      })
      .catch(function (e) { showErrors(e.errors && e.errors.length ? e.errors : [e.message]); throw e; })
      .then(function (v) { S.busy = false; refreshChrome(); return v; },
            function (e) { S.busy = false; refreshChrome(); throw e; });
  }

  function rerender() {
    return api("POST", "/reports/" + encodeURIComponent(RID) + "/preview", { layout: S.layout })
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

  function renderCanvas() {
    var box = $("[data-canvas]");
    if (!S.model) { box.textContent = "Loading…"; return; }
    if (!window.Ovid2ReportRender) { box.textContent = "Renderer failed to load."; return; }
    window.Ovid2ReportRender.render(box, S.model, {
      actions: true,
      onBlock: function (card, b) {
        card.classList.add("rs-pickable");
        if (S.sel.kind === "block" && S.sel.id === b.id) card.classList.add("rs-selected");
        card.addEventListener("click", function (ev) {
          if (ev.target.closest(".vega-embed details, a")) return;
          select("block", b.id);
        });
      },
      onSection: function (sec, s) {
        var h = sec.querySelector(".ovr-section-title");
        if (h) {
          h.classList.add("rs-pickable");
          h.addEventListener("click", function () { select("section", s.id); });
        }
        if (S.sel.kind === "section" && S.sel.id === s.id) sec.classList.add("rs-selected");
      }
    });
    var hidden = 0;
    (S.layout.sections || []).forEach(function (s) {
      if (s.visible === false) hidden += 1;
      (s.blocks || []).forEach(function (b) { if (b.visible === false) hidden += 1; });
    });
    if (hidden) {
      var n = el("p", "rs-hidden-note note small", hidden + " hidden item(s) — not shown and not exported.");
      box.insertBefore(n, box.firstChild);
    }
  }

  function select(kind, id) {
    S.sel = { kind: kind, id: id };
    renderOutline(); renderInspector();
    Array.prototype.forEach.call(root.querySelectorAll(".rs-selected"), function (n) { n.classList.remove("rs-selected"); });
    var target = kind === "block" ? root.querySelector('.ovr-block[data-block-id="' + id + '"]')
               : kind === "section" ? root.querySelector('.ovr-section[data-section-id="' + id + '"]') : null;
    if (target) { target.classList.add("rs-selected"); target.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
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

  function blockLabel(b) {
    if (b.title) return b.title;
    if (b.type === "chart") { var c = chart(b.chart); return (c ? c.label : b.chart) + (b.dataset ? " · " + b.dataset : ""); }
    return b.type === "callout" ? "Callout" : "Text";
  }

  function renderOutline() {
    var ol = $("[data-outline]");
    ol.innerHTML = "";
    var rb = $("[data-select-report]");
    rb.classList.toggle("active", S.sel.kind === "report");
    var sections = S.layout.sections || [];
    if (!sections.length) ol.appendChild(el("li", "note small", "No sections yet."));
    sections.forEach(function (s, si) {
      var li = el("li", "rs-node rs-node-section" + (s.visible === false ? " rs-off" : ""));
      li.setAttribute("data-kind", "section"); li.setAttribute("data-id", s.id);
      var row = el("div", "rs-row" + (S.sel.kind === "section" && S.sel.id === s.id ? " active" : ""));
      row.draggable = canEdit;
      row.appendChild(el("span", "rs-grip", "\u2807"));
      row.appendChild(el("span", "rs-label", s.title));
      row.appendChild(moveButtons("section", s.id, si, sections.length));
      row.appendChild(eyeButton(s, true));
      row.addEventListener("click", function () { select("section", s.id); });
      wireDrag(row, "section", s.id, null);
      li.appendChild(row);
      var bl = el("ol", "rs-tree-blocks");
      (s.blocks || []).forEach(function (b, bi) {
        var bli = el("li", "rs-node rs-node-block" + (b.visible === false ? " rs-off" : ""));
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
      if (canEdit) {
        var add = el("li", "rs-add-block");
        var ab = el("button", "rs-add-btn", "+ Block"); ab.type = "button";
        ab.addEventListener("click", function () { S.sel = { kind: "new-block", id: s.id }; renderOutline(); renderInspector(); });
        add.appendChild(ab); bl.appendChild(add);
      }
      li.appendChild(bl);
      ol.appendChild(li);
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

  function renderInspector() {
    var box = $("[data-inspector]");
    box.innerHTML = "";
    var t = $("[data-inspector-title]");
    if (S.sel.kind === "section" && findSection(S.sel.id)) { t.textContent = "Section"; inspectSection(box, findSection(S.sel.id)); }
    else if (S.sel.kind === "block" && findBlock(S.sel.id)) { t.textContent = "Block"; inspectBlock(box, findBlock(S.sel.id)); }
    else if (S.sel.kind === "new-block" && findSection(S.sel.id)) { t.textContent = "Add a block"; gallery(box, findSection(S.sel.id), null); }
    else { S.sel = { kind: "report", id: null }; t.textContent = "Report"; inspectReport(box); }
  }

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
    row.appendChild(vis); row.appendChild(pb); box.appendChild(row);
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
      [["text", "Text", "Markdown paragraph"], ["callout", "Callout", "Highlighted note"]].forEach(function (k) {
        var b = el("button", "rs-tile"); b.type = "button"; b.title = k[2];
        b.appendChild(el("span", "rs-tile-name", k[1]));
        b.addEventListener("click", function () {
          var blk = { type: k[0], text: k[0] === "callout" ? "**Note:** …" : "Write here…" };
          if (k[0] === "callout") blk.tone = "info";
          applyOps([{ op: "add_block", section_id: section.id, block: blk }]).then(function () {
            var s = findSection(section.id); var nb = s.blocks[s.blocks.length - 1]; select("block", nb.id);
          }).catch(function () {});
        });
        basics.appendChild(b);
      });
      wrap.appendChild(el("h3", "rs-gallery-cat", "Text"));
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
      applyOps([{ op: "add_block", section_id: section.id, block: { type: "chart", chart: c.name, dataset: dsName, params: params, title: c.label } }])
        .then(function () { var s = findSection(section.id); select("block", s.blocks[s.blocks.length - 1].id); })
        .catch(function () {});
    }
  }

  function inspectBlock(box, found) {
    var b = found.block, s = found.section;
    function upd(o, merge) {
      var op = { op: "update_block", block_id: b.id, set: o };
      if (merge) op.merge_params = true;
      return applyOps([op]).then(function () { renderInspector(); }).catch(function () {});
    }
    box.appendChild(field("Title", textInput(b.title, function (v) { upd({ title: v }); }, { max: 200 })));
    box.appendChild(field("Width", selectInput([{ value: "full", label: "Full" }, { value: "two_thirds", label: "Two thirds" }, { value: "half", label: "Half" }, { value: "third", label: "Third" }],
      b.width || "full", function (v) { upd({ width: v }); })));
    var vis = el("label", "rs-check"); vis.appendChild(checkInput(b.visible !== false, function (v) { upd({ visible: v }); })); vis.appendChild(document.createTextNode(" Visible"));
    box.appendChild(vis);

    if (b.type === "text" || b.type === "callout") {
      if (b.type === "callout") box.appendChild(field("Tone", selectInput(["info", "success", "warning", "danger", "note"].map(function (x) { return { value: x }; }), b.tone || "info", function (v) { upd({ tone: v }); })));
      if (b.text_ref) box.appendChild(el("p", "note small", "Showing generated text “" + b.text_ref + "” from the source. Type below to replace it."));
      box.appendChild(field("Text (markdown)", textInput(b.text, function (v) { upd(v ? { text: v, text_ref: null } : { text: null }); }, { multiline: true, rows: 8, max: 20000 })));
    } else {
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
      box.appendChild(field("Caption", textInput(b.caption, function (v) { upd({ caption: v }); }, { multiline: true, rows: 2, max: 2000 })));
      if (c && c.renderer === "vega") {
        var ex = el("div", "rs-actions");
        ["svg", "png"].forEach(function (f) {
          var eb = el("button", "button small", "Download " + f.toUpperCase()); eb.type = "button";
          eb.addEventListener("click", function () { exportAs(f, b.id); });
          ex.appendChild(eb);
        });
        box.appendChild(ex);
      }
    }
    if (canEdit) {
      var acts = el("div", "rs-actions");
      var dup = el("button", "button small", "Duplicate"); dup.type = "button";
      dup.addEventListener("click", function () {
        var copy = clone(b); delete copy.id; copy.title = (b.title || blockLabel(b)) + " (copy)";
        var idx = s.blocks.map(function (x) { return x.id; }).indexOf(b.id) + 1;
        applyOps([{ op: "add_block", section_id: s.id, index: idx, block: copy }]).then(function () { select("block", findSection(s.id).blocks[idx].id); }).catch(function () {});
      });
      var mv = selectInput((S.layout.sections || []).map(function (x) { return { value: x.id, label: "Move to: " + x.title }; }), s.id, function (v) {
        if (v !== s.id) applyOps([{ op: "move_block", block_id: b.id, section_id: v, index: -1 }]).catch(function () {});
      });
      var del = el("button", "button small danger", "Remove"); del.type = "button";
      del.addEventListener("click", function () {
        applyOps([{ op: "remove_block", block_id: b.id }]).then(function () { select("section", s.id); }).catch(function () {});
      });
      acts.appendChild(dup); acts.appendChild(mv); acts.appendChild(del); box.appendChild(acts);
    }
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

  function exportAs(fmt, blockId) {
    var body = { format: fmt };
    if (blockId) body.block_id = blockId;
    if (dirty()) body.layout = S.layout;
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
      b.addEventListener("click", function () { m.hidden = true; exportAs(f.format); });
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
    if (!t || !root.contains(t)) { closeMenus(null); return; }
    if (t.hasAttribute("data-export-toggle")) { var m = $("[data-export-menu]"); closeMenus(m); m.hidden = !m.hidden; t.setAttribute("aria-expanded", String(!m.hidden)); return; }
    if (t.hasAttribute("data-more-toggle")) { var mm = $("[data-more-menu]"); closeMenus(mm); mm.hidden = !mm.hidden; t.setAttribute("aria-expanded", String(!mm.hidden)); return; }
    closeMenus(null);
    if (t.hasAttribute("data-save")) save();
    else if (t.hasAttribute("data-undo")) undo();
    else if (t.hasAttribute("data-redo")) redo();
    else if (t.hasAttribute("data-select-report")) select("report", null);
    else if (t.hasAttribute("data-add-section")) {
      applyOps([{ op: "add_section", title: "New section" }]).then(function () {
        var s = S.layout.sections[S.layout.sections.length - 1]; select("section", s.id);
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
    if (/INPUT|TEXTAREA|SELECT/.test(tag)) return;
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); if (ev.shiftKey) redo(); else undo(); }
    else if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); }
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
                              title: b.title || null, visible: b.visible !== false };
                   }) };
        })
      };
    },
    applyOps: function (ops) {
      return applyOps(ops).then(function (notes) { S.agentEdits = true; refreshChrome(); return notes; });
    },
    undo: undo
  };

  /* ---------------- boot ---------------- */

  Promise.all([
    api("GET", "/reports/" + encodeURIComponent(RID)),
    api("GET", "/report-catalogue"),
    api("GET", "/reports/" + encodeURIComponent(RID) + "/render")
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
