/* Ovid2 Report Studio — library page (/reports).
 *
 * Scopes, folders, search and tag filters over GET /api/v1/reports; the
 * Sources tab over GET /api/v1/report-sources; "New report" dialog
 * (POST /api/v1/reports). ?ingest=<instance_id> ingests that run's
 * artifacts first (POST /api/v1/report-sources/ingest) and pre-selects its
 * health source. All DOM text goes through textContent.
 */
(function () {
  "use strict";

  var root = document.getElementById("report-library");
  if (!root) return;
  var canWrite = root.getAttribute("data-can-write") === "true";
  var state = { scope: "all", folder: null, q: "", tag: "", sort: "updated", reports: [], folders: [],
                sources: [], kinds: [], templates: [], selected: {} };

  function $(sel, el) { return (el || root).querySelector(sel); }
  function $$(sel, el) { return Array.prototype.slice.call((el || root).querySelectorAll(sel)); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function icon(name) {
    var s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("width", "15"); s.setAttribute("height", "15"); s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2");
    s.setAttribute("aria-hidden", "true");
    var paths = { star: "M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z" };
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", paths[name] || "");
    s.appendChild(p);
    return s;
  }

  function api(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: { Accept: "application/json" } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    return fetch("/api/v1" + path, opts).then(function (r) {
      return r.text().then(function (t) {
        var data; try { data = t ? JSON.parse(t) : null; } catch (e) { data = t; }
        if (!r.ok) {
          var d = data && data.detail, msg = typeof d === "string" ? d : (d && d.message) || ("HTTP " + r.status);
          var err = new Error(msg); err.status = r.status; err.errors = (d && d.errors) || []; throw err;
        }
        return data;
      });
    });
  }

  function notice(text, kind) {
    var n = $("[data-notice]");
    n.textContent = text || "";
    n.className = "rs-notice" + (kind ? " rs-notice-" + kind : "");
    n.hidden = !text;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    try { return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }); }
    catch (e) { return iso; }
  }

  /* ---------------- reports ---------------- */

  function loadReports() {
    var qs = new URLSearchParams();
    if (state.scope === "archived") qs.set("archived", "true");
    else qs.set("scope", state.scope);
    if (state.folder !== null) qs.set("folder", state.folder);
    return api("GET", "/reports?" + qs.toString()).then(function (d) {
      state.reports = d.reports || [];
      state.folders = d.folders || [];
      if (state.scope === "all" && state.folder === null) {
        var c = $("[data-count=all]"); if (c) c.textContent = String(d.total || 0);
      }
      renderFolders(); renderTags(); renderReports();
    }).catch(function (e) { $("[data-reports]").textContent = "Could not load reports: " + e.message; });
  }

  function renderFolders() {
    var box = $("[data-folders]");
    box.innerHTML = "";
    var named = state.folders.filter(function (f) { return f.folder; });
    if (!named.length) { box.appendChild(el("p", "note small", "No folders yet.")); }
    named.forEach(function (f) {
      var b = el("button", "rs-side-item rs-folder");
      b.type = "button";
      b.setAttribute("data-folder", f.folder);
      if (state.folder === f.folder) b.setAttribute("aria-current", "true");
      var depth = f.folder.split("/").length - 1;
      b.style.paddingLeft = (0.6 + depth * 0.8) + "rem";
      b.appendChild(el("span", null, f.folder.split("/").pop()));
      b.appendChild(el("span", "rs-count", String(f.count)));
      b.title = f.folder;
      box.appendChild(b);
    });
    var dl = document.querySelector("[data-folder-list]");
    if (dl) {
      dl.innerHTML = "";
      named.forEach(function (f) { var o = el("option"); o.value = f.folder; dl.appendChild(o); });
    }
  }

  function renderTags() {
    var sel = $("[data-tag]"), cur = sel.value, seen = {};
    state.reports.forEach(function (r) { (r.tags || []).forEach(function (t) { seen[t] = true; }); });
    sel.innerHTML = "";
    var any = el("option", null, "Any tag"); any.value = ""; sel.appendChild(any);
    Object.keys(seen).sort().forEach(function (t) { var o = el("option", null, t); o.value = t; sel.appendChild(o); });
    sel.value = seen[cur] ? cur : "";
    state.tag = sel.value;
  }

  function visibleReports() {
    var q = state.q.toLowerCase();
    var out = state.reports.filter(function (r) {
      if (state.tag && (r.tags || []).indexOf(state.tag) === -1) return false;
      if (!q) return true;
      return [r.title, r.description, r.folder, r.owner].concat(r.tags || []).join(" ").toLowerCase().indexOf(q) !== -1;
    });
    out.sort(function (a, b) {
      if (state.sort === "title") return a.title.localeCompare(b.title);
      var k = state.sort === "created" ? "created_at" : "updated_at";
      return (b[k] || "").localeCompare(a[k] || "");
    });
    return out;
  }

  function renderReports() {
    var box = $("[data-reports]");
    box.innerHTML = "";
    var list = visibleReports();
    if (!list.length) {
      var empty = el("div", "rs-empty");
      empty.appendChild(el("p", null, state.scope === "all" && !state.q && !state.tag
        ? "No reports yet. Create one from a report source — every finished health, analysis or ETL run publishes one."
        : "No reports match."));
      if (canWrite && state.scope === "all") {
        var b = el("button", "button primary", "New report"); b.type = "button";
        b.addEventListener("click", function () { openNew(); });
        empty.appendChild(b);
      }
      box.appendChild(empty);
      return;
    }
    list.forEach(function (r) { box.appendChild(card(r)); });
  }

  function card(r) {
    var c = el("article", "rs-card" + (r.archived ? " rs-archived" : ""));
    var head = el("div", "rs-card-head");
    var a = el("a", "rs-card-title", r.title);
    a.href = "/reports/" + encodeURIComponent(r.report_id);
    head.appendChild(a);
    var fav = el("button", "rs-fav" + (r.favorite ? " on" : ""));
    fav.type = "button";
    fav.title = r.favorite ? "Remove from favourites" : "Add to favourites";
    fav.setAttribute("aria-pressed", r.favorite ? "true" : "false");
    fav.appendChild(icon("star"));
    fav.addEventListener("click", function () {
      api("PUT", "/reports/" + encodeURIComponent(r.report_id) + "/favorite", { favorite: !r.favorite })
        .then(function () { r.favorite = !r.favorite; renderReports(); })
        .catch(function (e) { notice(e.message, "error"); });
    });
    head.appendChild(fav);
    c.appendChild(head);
    if (r.description) c.appendChild(el("p", "rs-card-desc", r.description));
    var meta = el("div", "rs-card-meta");
    if (r.status === "draft") meta.appendChild(el("span", "badge status-draft", "draft"));
    if (r.permission !== "owner") meta.appendChild(el("span", "badge", (r.permission === "edit" ? "can edit · " : "view · ") + r.owner));
    else if (r.shared) meta.appendChild(el("span", "badge status-ok", "shared"));
    if (r.folder) meta.appendChild(el("span", "rs-chip rs-chip-folder", r.folder));
    (r.tags || []).forEach(function (t) {
      var chip = el("button", "rs-chip", "#" + t); chip.type = "button";
      chip.addEventListener("click", function () { $("[data-tag]").value = t; state.tag = t; renderReports(); });
      meta.appendChild(chip);
    });
    c.appendChild(meta);
    var foot = el("div", "rs-card-foot");
    foot.appendChild(el("span", "note small", r.sections + " sections · " + r.blocks + " blocks · v" + r.version + " · " + r.theme));
    foot.appendChild(el("span", "note small", "Updated " + fmtDate(r.updated_at)));
    c.appendChild(foot);
    if (r.archived && r.permission === "owner") {
      var rb = el("button", "button small", "Restore"); rb.type = "button";
      rb.addEventListener("click", function () {
        api("POST", "/reports/" + encodeURIComponent(r.report_id) + "/restore", {})
          .then(function () { notice("Restored " + r.title + ".", "ok"); loadReports(); })
          .catch(function (e) { notice(e.message, "error"); });
      });
      c.appendChild(rb);
    }
    return c;
  }

  /* ---------------- sources ---------------- */

  function loadSources() {
    var qs = new URLSearchParams();
    var q = $("[data-src-q]").value.trim(), k = $("[data-src-kind]").value;
    if (q) qs.set("q", q);
    if (k) qs.set("kind", k);
    return api("GET", "/report-sources?" + qs.toString()).then(function (d) {
      state.sources = d.sources || [];
      renderSources();
    }).catch(function (e) { $("[data-sources]").textContent = "Could not load sources: " + e.message; });
  }

  function kindLabel(kind) {
    var k = state.kinds.filter(function (x) { return x.kind === kind; })[0];
    return k ? k.label : kind;
  }

  function renderSources() {
    var box = $("[data-sources]");
    box.innerHTML = "";
    if (!state.sources.length) {
      box.appendChild(el("p", "note", "No report sources yet. They appear when a run finishes (or ingest one above)."));
      return;
    }
    var table = el("table", "data-table rs-src-table");
    var thead = el("thead"), tr = el("tr");
    ["", "Source", "Kind", "Pipeline", "Batch", "Generated", "Datasets", ""].forEach(function (h) { tr.appendChild(el("th", null, h)); });
    thead.appendChild(tr); table.appendChild(thead);
    var tbody = el("tbody");
    state.sources.forEach(function (s) {
      var row = el("tr");
      var pick = el("td"), cb = el("input"); cb.type = "checkbox";
      cb.disabled = s.kind !== "health";
      cb.title = s.kind === "health" ? "Select for a trend" : "Only health sources can be combined into a trend";
      cb.checked = !!state.selected[s.source_id];
      cb.addEventListener("change", function () { if (cb.checked) state.selected[s.source_id] = true; else delete state.selected[s.source_id]; });
      pick.appendChild(cb); row.appendChild(pick);
      var t = el("td"); t.appendChild(el("div", null, s.title)); t.appendChild(el("div", "mono small note", s.source_id)); row.appendChild(t);
      row.appendChild(el("td", null, kindLabel(s.kind)));
      row.appendChild(el("td", "mono small", s.pipeline || "—"));
      row.appendChild(el("td", "mono small", s.batch_id || "—"));
      row.appendChild(el("td", "small", fmtDate(s.generated_at || s.ingested_at)));
      var ds = s.datasets || {};
      row.appendChild(el("td", "small", Object.keys(ds).map(function (k) { return k + " (" + ds[k] + ")"; }).join(", ")));
      var act = el("td", "rs-src-actions");
      var b = el("button", "button small primary", "Create report"); b.type = "button"; b.disabled = !canWrite;
      b.addEventListener("click", function () { openNew(s.source_id); });
      act.appendChild(b);
      var v = el("a", "button small", "Reports"); v.href = "#";
      v.addEventListener("click", function (ev) {
        ev.preventDefault();
        api("GET", "/reports?source_id=" + encodeURIComponent(s.source_id)).then(function (d) {
          showTab("reports"); state.reports = d.reports || []; renderReports();
          notice((d.total || 0) + " report(s) on " + s.title + ".", "ok");
        });
      });
      act.appendChild(v);
      row.appendChild(act);
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    var wrap = el("div", "table-wrap"); wrap.appendChild(table);
    box.appendChild(wrap);
  }

  function showTab(name) {
    $$("[data-tab]").forEach(function (t) { t.hidden = t.getAttribute("data-tab") !== name; });
    if (name === "sources") loadSources();
  }

  /* ---------------- new report ---------------- */

  function fillTemplates(sourceId) {
    var src = state.sources.filter(function (s) { return s.source_id === sourceId; })[0];
    var sel = $("[data-new-template]", document);
    sel.innerHTML = "";
    if (!src) return;
    var rec = src.recommended_templates || [];
    var list = state.templates.filter(function (t) { return t.source_kind === src.kind; });
    list.sort(function (a, b) { return (rec.indexOf(b.template_id) !== -1) - (rec.indexOf(a.template_id) !== -1); });
    list.forEach(function (t) {
      var o = el("option", null, t.name + (rec.indexOf(t.template_id) !== -1 ? " (recommended)" : t.builtin ? "" : " (yours)"));
      o.value = t.template_id; o.setAttribute("data-desc", t.description || "");
      sel.appendChild(o);
    });
    var blank = el("option", null, "Blank report"); blank.value = ""; sel.appendChild(blank);
    descTemplate();
  }

  function descTemplate() {
    var sel = $("[data-new-template]", document), o = sel.options[sel.selectedIndex];
    $("[data-new-template-desc]", document).textContent = o ? (o.getAttribute("data-desc") || "") : "";
  }

  function openNew(sourceId) {
    if (!canWrite) return;
    var dlg = $("[data-new-dialog]");
    var p = state.sources.length ? Promise.resolve() : api("GET", "/report-sources?limit=500").then(function (d) { state.sources = d.sources || []; });
    Promise.all([p, api("GET", "/report-templates").then(function (t) { state.templates = t; })]).then(function () {
      var sel = $("[data-new-source]", document);
      sel.innerHTML = "";
      if (!state.sources.length) { notice("No report sources yet — run a health / analysis / ETL job first, or ingest a past run on the Sources tab.", "warn"); return; }
      state.sources.forEach(function (s) {
        var o = el("option", null, s.title + " — " + kindLabel(s.kind) + (s.batch_id ? " · " + s.batch_id : ""));
        o.value = s.source_id; sel.appendChild(o);
      });
      if (sourceId) sel.value = sourceId;
      fillTemplates(sel.value);
      $("[data-new-errors]", document).hidden = true;
      dlg.showModal();
    }).catch(function (e) { notice(e.message, "error"); });
  }

  function submitNew(ev) {
    ev.preventDefault();
    var f = ev.target, errs = $("[data-new-errors]", document);
    var tags = (f.tags.value || "").split(",").map(function (t) { return t.trim(); }).filter(Boolean);
    var body = { source_id: f.source_id.value, folder: f.folder.value.trim(), tags: tags };
    if (f.template_id.value) body.template_id = f.template_id.value;
    else body.layout = { v: 1, title: f.title.value.trim() || "Untitled report", sections: [] };
    if (f.title.value.trim()) body.title = f.title.value.trim();
    api("POST", "/reports", body).then(function (r) {
      window.location.assign("/reports/" + encodeURIComponent(r.report_id));
    }).catch(function (e) {
      errs.innerHTML = "";
      (e.errors && e.errors.length ? e.errors : [e.message]).forEach(function (m) { errs.appendChild(el("li", null, m)); });
      errs.hidden = false;
    });
  }

  /* ---------------- ingest + trend ---------------- */

  function ingest(instanceId) {
    notice("Ingesting run " + instanceId + "…");
    return api("POST", "/report-sources/ingest", { instance_id: instanceId }).then(function (d) {
      var n = (d.sources || []).length;
      if (!n) { notice("That run produced no report data (no health report, analysis artifact or event log).", "warn"); return null; }
      notice("Ingested " + n + " source(s) from " + instanceId + ".", "ok");
      state.sources = [];
      return d.sources;
    }).catch(function (e) { notice("Ingest failed: " + e.message, "error"); return null; });
  }

  function buildTrend() {
    var ids = Object.keys(state.selected);
    if (ids.length < 2) { notice("Tick two or more health sources to combine into a trend.", "warn"); return; }
    api("POST", "/report-sources/trend", { source_ids: ids }).then(function (s) {
      state.selected = {};
      notice("Built trend source " + s.source_id + ".", "ok");
      loadSources();
    }).catch(function (e) { notice(e.message, "error"); });
  }

  /* ---------------- wiring ---------------- */

  root.addEventListener("click", function (ev) {
    var t = ev.target.closest("button, a");
    if (!t || !root.contains(t)) return;
    if (t.hasAttribute("data-scope")) {
      state.scope = t.getAttribute("data-scope"); state.folder = null;
      $$("[data-scope], [data-folder]").forEach(function (b) { b.removeAttribute("aria-current"); });
      t.setAttribute("aria-current", "true");
      showTab("reports"); loadReports();
    } else if (t.hasAttribute("data-folder")) {
      state.folder = t.getAttribute("data-folder"); state.scope = "mine";
      $$("[data-scope], [data-folder]").forEach(function (b) { b.removeAttribute("aria-current"); });
      t.setAttribute("aria-current", "true");
      showTab("reports"); loadReports();
    } else if (t.hasAttribute("data-tab-btn")) {
      showTab(t.getAttribute("data-tab-btn"));
    } else if (t.hasAttribute("data-new-report")) {
      openNew();
    } else if (t.hasAttribute("data-trend")) {
      buildTrend();
    } else if (t.hasAttribute("data-ingest-btn")) {
      var id = $("[data-ingest-id]").value.trim();
      if (id) ingest(id).then(function () { loadSources(); });
    } else if (t.hasAttribute("data-close")) {
      $("[data-new-dialog]").close();
    }
  });
  $("[data-q]").addEventListener("input", function (e) { state.q = e.target.value; renderReports(); });
  $("[data-tag]").addEventListener("change", function (e) { state.tag = e.target.value; renderReports(); });
  $("[data-sort]").addEventListener("change", function (e) { state.sort = e.target.value; renderReports(); });
  var srcTimer = null;
  $("[data-src-q]").addEventListener("input", function () { clearTimeout(srcTimer); srcTimer = setTimeout(loadSources, 250); });
  $("[data-src-kind]").addEventListener("change", loadSources);
  $("[data-new-source]", document).addEventListener("change", function (e) { fillTemplates(e.target.value); });
  $("[data-new-template]", document).addEventListener("change", descTemplate);
  $("[data-new-form]", document).addEventListener("submit", submitNew);

  api("GET", "/report-catalogue").then(function (c) {
    state.kinds = c.source_kinds || [];
    var sel = $("[data-src-kind]");
    state.kinds.forEach(function (k) { var o = el("option", null, k.label); o.value = k.kind; sel.appendChild(o); });
  }).catch(function () { /* catalogue is cosmetic here */ });

  loadReports();
  var pending = root.getAttribute("data-ingest");
  var preSource = root.getAttribute("data-source");
  if (pending && /^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$/.test(pending)) {
    ingest(pending).then(function (stored) {
      if (!stored) return;
      var health = stored.filter(function (s) { return s.kind === "health"; })[0] || stored[0];
      history.replaceState(null, "", "/reports");
      if (canWrite) openNew(health.source_id); else showTab("sources");
    });
  } else if (preSource && canWrite) {
    openNew(preSource);
  }
})();
