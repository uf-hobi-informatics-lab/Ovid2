/* Ovid2 Report Studio — render-model → DOM renderer.
 *
 * Shared by the Studio canvas (/reports/{id}) and the self-contained HTML
 * export (inlined verbatim). Consumes the JSON render model produced by
 * ovid2.reports.compose.render_model, so the browser and every exporter
 * show the same report.
 *
 * Theme independence: everything is styled from model.theme (and a
 * block's resolved style overrides) via CSS custom properties scoped to
 * the .ovr-report root (--ovr-*). This file and report_render.css never
 * read the web page's theme tokens; pinned by
 * tests/contract/test_report_catalogues.py.
 *
 * Two layouts:
 *   render(target, model, opts)          continuous sheet (default);
 *   render(..., {pages: true})           paginated sheets at the report's
 *                                         page size / orientation with the
 *                                         PDF margins and a page footer.
 *
 * Hooks for the editor: opts.onBlock(card, b), opts.onSection(sec, s),
 * opts.onHead(head, model), opts.onGrid(grid, s). Pagination keeps the same
 * DOM nodes, so editor hooks keep working in either layout.
 *
 * Safety: all text goes through textContent; markdown arrives as HTML that
 * the server rendered with raw HTML disabled.
 */
(function (root) {
  "use strict";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* Page geometry in CSS px (96 dpi), mirroring render/pdf.py. */
  var PAGE_IN = { letter: [8.5, 11], a4: [8.27, 11.69], legal: [8.5, 14] };
  var MARGIN_IN = 0.6, MARGIN_BOTTOM_IN = 0.7, DPI = 96;

  function pageBox(page) {
    var p = page || {};
    var wh = PAGE_IN[p.size] || PAGE_IN.letter;
    var w = wh[0], h = wh[1];
    if (p.orientation === "landscape") { var t = w; w = h; h = t; }
    return { width: Math.round(w * DPI), height: Math.round(h * DPI),
             margin: Math.round(MARGIN_IN * DPI), bottom: Math.round(MARGIN_BOTTOM_IN * DPI) };
  }

  function applyTheme(node, theme) {
    var c = theme.colors || {}, f = theme.fonts || {}, s = theme.sizes || {};
    var vars = {
      "--ovr-bg": c.background, "--ovr-surface": c.surface, "--ovr-text": c.text,
      "--ovr-muted": c.muted, "--ovr-border": c.border, "--ovr-accent": c.accent,
      "--ovr-grid": c.grid, "--ovr-font": f.body, "--ovr-font-heading": f.heading,
      "--ovr-font-mono": f.mono, "--ovr-size": (s.base || 11) + "pt",
      "--ovr-size-title": (s.title || 20) + "pt", "--ovr-size-heading": (s.heading || 15) + "pt"
    };
    Object.keys(vars).forEach(function (k) { if (vars[k]) node.style.setProperty(k, vars[k]); });
    var st = theme.status || {};
    Object.keys(st).forEach(function (k) { node.style.setProperty("--ovr-status-" + k.replace(/_/g, "-"), st[k]); });
    node.setAttribute("data-report-theme", theme.name || "publication");
    node.setAttribute("data-report-mode", theme.mode || "light");
  }

  /* Per-component overrides: colours as --ovr-b-* variables, the rest as
   * classes. Absent keys fall back to the theme in CSS. */
  function applyStyle(node, sr) {
    sr = sr || {};
    if (sr.accent) node.style.setProperty("--ovr-b-accent", sr.accent);
    if (sr.background) node.style.setProperty("--ovr-b-bg", sr.background);
    if (sr.text_color) node.style.setProperty("--ovr-b-text", sr.text_color);
    ["align", "emphasis", "font_scale", "border", "padding"].forEach(function (k) {
      if (sr[k]) node.classList.add("ovr-" + k.replace("_", "-") + "-" + sr[k]);
    });
    if (sr.background) node.classList.add("ovr-has-bg");
    if (sr.text_color) node.classList.add("ovr-has-text");
    if (sr.accent) node.classList.add("ovr-has-accent");
  }

  function statusColor(theme, v) {
    var st = (theme && theme.status) || {};
    return Object.prototype.hasOwnProperty.call(st, v) ? st[v] : null;
  }

  function renderKpi(b, theme) {
    var k = b.kpi || {};
    var box = el("div", "ovr-kpi" + (k.status ? " ovr-kpi-" + k.status : ""));
    var v = el("div", "ovr-kpi-value", k.display);
    if (k.status === "good" && statusColor(theme, "passed")) v.style.color = statusColor(theme, "passed");
    if (k.status === "bad" && statusColor(theme, "exception")) v.style.color = statusColor(theme, "exception");
    box.appendChild(el("div", "ovr-kpi-label", k.label));
    box.appendChild(v);
    return box;
  }

  function renderTable(b, theme) {
    var t = b.table || { columns: [], rows: [] };
    var wrap = el("div", "ovr-table-wrap");
    if (!t.rows.length) { wrap.appendChild(el("p", "ovr-muted", "No rows.")); return wrap; }
    var table = el("table", "ovr-table" + (t.zebra ? " ovr-zebra" : ""));
    var thead = el("thead"), tr = el("tr");
    t.columns.forEach(function (c) { tr.appendChild(el("th", null, c.title)); });
    thead.appendChild(tr); table.appendChild(thead);
    var idx = t.status_column ? t.columns.map(function (c) { return c.name; }).indexOf(t.status_column) : -1;
    var tbody = el("tbody");
    t.rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (v, i) {
        var td = el("td", null, v);
        if (i === idx) {
          var col = statusColor(theme, v);
          if (col) { td.className = "ovr-status"; td.style.setProperty("--ovr-chip", col); }
        }
        r.appendChild(td);
      });
      tbody.appendChild(r);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    if (t.truncated) wrap.appendChild(el("p", "ovr-muted ovr-small", "Showing " + t.rows.length + " of " + t.total_rows + " rows."));
    return wrap;
  }

  function renderVega(b, opts) {
    var holder = el("div", "ovr-chart");
    holder.setAttribute("data-block-id", b.id);
    var embed = root.vegaEmbed;
    if (!embed) { holder.appendChild(el("p", "ovr-muted", "Charts need the Vega bundle.")); return holder; }
    var spec = JSON.parse(JSON.stringify(b.spec));
    spec.width = "container";
    spec.autosize = { type: "fit-x", contains: "padding" };
    // Defer until attached so "container" width resolves.
    setTimeout(function () {
      embed(holder, spec, { actions: opts.actions === false ? false : { export: true, source: false, compiled: false, editor: false },
                            renderer: "svg", tooltip: true }).then(function () {
        if (opts.onChartDone) opts.onChartDone(holder, b);
      }).catch(function (e) {
        holder.textContent = "Chart failed: " + (e && e.message || e);
        if (opts.onChartDone) opts.onChartDone(holder, b);
      });
    }, 0);
    return holder;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso).slice(0, 10) : d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function renderNote(b) {
    var box = el("div", "ovr-note ovr-tone-" + (b.tone || "note"));
    var by = el("div", "ovr-note-by");
    by.appendChild(el("span", "ovr-note-tag", b.private ? "Private note" : "Note"));
    if (b.author) by.appendChild(el("span", null, b.author));
    if (b.updated_at) by.appendChild(el("span", null, fmtDate(b.updated_at)));
    box.appendChild(by);
    var body = el("div", "ovr-text ovr-note-body");
    body.innerHTML = b.html || "";  // server-rendered, raw HTML disabled
    box.appendChild(body);
    return box;
  }

  function renderInfo(b) {
    var dl = el("dl", "ovr-info");
    (b.rows || []).forEach(function (r, i) {
      var dt = el("dt", null, r.key); dt.setAttribute("data-row", i);
      var dd = el("dd", null, r.value); dd.setAttribute("data-row", i);
      if (!r.value) dd.classList.add("ovr-empty");
      dl.appendChild(dt); dl.appendChild(dd);
    });
    return dl;
  }

  function renderChecklist(b) {
    var ul = el("ul", "ovr-checklist");
    (b.items || []).forEach(function (it, i) {
      var li = el("li", it.done ? "ovr-done" : null);
      li.setAttribute("data-item", i);
      var box = el("span", "ovr-check"); box.setAttribute("aria-hidden", "true");
      li.appendChild(box);
      li.appendChild(el("span", "ovr-check-text", it.text));
      ul.appendChild(li);
    });
    return ul;
  }

  function renderBlock(b, model, opts) {
    var card = el("figure", "ovr-block ovr-w-" + (b.width || "full") + " ovr-r-" + b.renderer);
    card.setAttribute("data-block-id", b.id);
    applyStyle(card, b.style_resolved);
    if (b.visible === false) card.classList.add("ovr-is-hidden");
    if (b.export === false) card.classList.add("ovr-no-export");
    if (b.private) card.classList.add("ovr-private");
    var titled = b.renderer !== "kpi" && b.renderer !== "divider" && b.renderer !== "spacer";
    if (titled && (b.title || opts.editable)) {
      var h = el("h3", "ovr-block-title", b.title || "");
      if (!b.title) h.classList.add("ovr-placeholder");
      card.appendChild(h);
    }
    var body;
    if (b.renderer === "vega") body = renderVega(b, opts);
    else if (b.renderer === "kpi") body = renderKpi(b, model.theme);
    else if (b.renderer === "table") body = renderTable(b, model.theme);
    else if (b.renderer === "text" || b.renderer === "callout") {
      body = el("div", b.renderer === "callout" ? "ovr-callout ovr-tone-" + (b.tone || "info") : "ovr-text");
      body.innerHTML = b.html || "";  // server-rendered, raw HTML disabled
    } else if (b.renderer === "note") body = renderNote(b);
    else if (b.renderer === "info") body = renderInfo(b);
    else if (b.renderer === "checklist") body = renderChecklist(b);
    else if (b.renderer === "divider") body = el("hr", "ovr-divider");
    else if (b.renderer === "spacer") body = el("div", "ovr-spacer ovr-spacer-" + (b.size || "md"));
    else body = el("p", "ovr-error", b.error || "Unavailable");
    body.classList.add("ovr-body");
    card.appendChild(body);
    if (b.caption || (opts.editable && b.renderer === "vega")) {
      var cap = el("figcaption", "ovr-caption", b.caption || "");
      if (!b.caption) cap.classList.add("ovr-placeholder");
      card.appendChild(cap);
    }
    if (b.suppressed_cells) card.appendChild(el("p", "ovr-muted ovr-small", b.suppressed_cells + " small cell(s) suppressed."));
    if (opts.onBlock) opts.onBlock(card, b);
    return card;
  }

  function renderHead(model, opts) {
    var head = el("header", "ovr-head");
    head.appendChild(el("h1", "ovr-title", model.title));
    if (model.subtitle || opts.editable) {
      var sub = el("p", "ovr-subtitle", model.subtitle || "");
      if (!model.subtitle) sub.classList.add("ovr-placeholder");
      head.appendChild(sub);
    }
    if (model.description_html || opts.editable) {
      var d = el("div", "ovr-text ovr-intro");
      d.innerHTML = model.description_html || "";
      if (!model.description_html) d.classList.add("ovr-placeholder");
      head.appendChild(d);
    }
    if (opts.onHead) opts.onHead(head, model);
    return head;
  }

  function renderSection(s, model, opts) {
    var sec = el("section", "ovr-section" + (s.page_break_before ? " ovr-break" : "") +
                 (s.kind === "notes" ? " ovr-section-notes" : ""));
    sec.id = "ovr-s-" + s.id;
    sec.setAttribute("data-section-id", s.id);
    applyStyle(sec, s.style_resolved);
    if (s.visible === false) sec.classList.add("ovr-is-hidden");
    if (s.private) sec.classList.add("ovr-private");
    var hd = el("div", "ovr-section-head");
    if (s.kind === "notes") hd.appendChild(el("span", "ovr-section-kicker", "Additional information"));
    hd.appendChild(el("h2", "ovr-section-title", s.title));
    sec.appendChild(hd);
    if (s.description_html || opts.editable) {
      var sd = el("div", "ovr-text ovr-section-desc");
      sd.innerHTML = s.description_html || "";
      if (!s.description_html) sd.classList.add("ovr-placeholder");
      sec.appendChild(sd);
    }
    var grid = el("div", "ovr-grid");
    s.blocks.forEach(function (b) { grid.appendChild(renderBlock(b, model, opts)); });
    sec.appendChild(grid);
    if (opts.onGrid) opts.onGrid(grid, s);
    if (opts.onSection) opts.onSection(sec, s);
    return sec;
  }

  function renderToc(model) {
    var toc = el("nav", "ovr-toc");
    toc.appendChild(el("h2", "ovr-toc-title", "Contents"));
    var ol = el("ol");
    model.sections.forEach(function (s) {
      if (s.visible === false) return;
      var li = el("li"), a = el("a", null, s.title);
      a.href = "#ovr-s-" + s.id; li.appendChild(a); ol.appendChild(li);
    });
    toc.appendChild(ol);
    return toc;
  }

  function renderProvenance(model) {
    var prov = el("footer", "ovr-provenance");
    prov.appendChild(el("h2", "ovr-prov-title", "Provenance"));
    var dl = el("dl");
    Object.keys(model.provenance || {}).forEach(function (k) {
      var v = model.provenance[k];
      if (v === null || v === "" || v === undefined) return;
      dl.appendChild(el("dt", null, k.replace(/_/g, " ")));
      dl.appendChild(el("dd", null, String(v)));
    });
    prov.appendChild(dl);
    return prov;
  }

  /* Build the report's content nodes (head, toc, sections, provenance). */
  function contentNodes(model, opts) {
    var out = [renderHead(model, opts)];
    if ((model.options || {}).show_toc && model.sections.length) out.push(renderToc(model));
    model.sections.forEach(function (s) { out.push(renderSection(s, model, opts)); });
    if ((model.options || {}).show_provenance !== false) out.push(renderProvenance(model));
    return out;
  }

  function newRoot(model, cls) {
    var rep = el("article", "ovr-report " + cls + " ovr-page-" + ((model.page || {}).orientation || "portrait"));
    applyTheme(rep, model.theme || {});
    return rep;
  }

  function render(target, model, opts) {
    opts = opts || {};
    target.innerHTML = "";
    if (opts.pages) return renderPages(target, model, opts);
    var rep = newRoot(model, "ovr-continuous");
    contentNodes(model, opts).forEach(function (n) { rep.appendChild(n); });
    target.appendChild(rep);
    return rep;
  }

  /* ---------------- pages mode ---------------- */

  function makeSheet(book, model, box, n) {
    var sheet = el("div", "ovr-sheet");
    sheet.style.width = box.width + "px";
    sheet.style.height = box.height + "px";
    sheet.style.padding = box.margin + "px " + box.margin + "px " + box.bottom + "px";
    var body = el("div", "ovr-sheet-body");
    sheet.appendChild(body);
    var foot = el("div", "ovr-sheet-foot");
    foot.style.left = box.margin + "px"; foot.style.right = box.margin + "px";
    foot.style.bottom = Math.round(0.3 * DPI) + "px";
    foot.appendChild(el("span", null, model.title || ""));
    foot.appendChild(el("span", "ovr-sheet-num", "Page " + n));
    sheet.appendChild(foot);
    book.appendChild(sheet);
    return body;
  }

  /* Lay content out on sheets. Content is first staged at the sheet's
   * content width so charts can draw at their real size; once every chart
   * is drawn (or after a timeout) the same nodes are flowed onto sheets.
   * Sections are split at block-row boundaries (rows packed by width like
   * the PDF writer); a section title is never left alone on a page. */
  function renderPages(target, model, opts) {
    var box = pageBox(model.page);
    var rep = newRoot(model, "ovr-paged");
    var staging = el("div", "ovr-staging");
    staging.style.width = (box.width - 2 * box.margin) + "px";
    var charts = 0;
    model.sections.forEach(function (s) { s.blocks.forEach(function (b) { if (b.renderer === "vega") charts += 1; }); });
    var done = false, timer = null;
    var userDone = opts.onChartDone;
    var o = {};
    Object.keys(opts).forEach(function (k) { o[k] = opts[k]; });
    o.onChartDone = function (h, b) {
      if (userDone) userDone(h, b);
      charts -= 1;
      if (charts <= 0) finish();
    };
    var nodes = contentNodes(model, o);
    nodes.forEach(function (n) { staging.appendChild(n); });
    rep.appendChild(staging);
    target.appendChild(rep);
    function finish() {
      if (done) return;
      done = true; clearTimeout(timer);
      flow(rep, staging, nodes, model, box, opts);
    }
    if (charts <= 0) finish(); else timer = setTimeout(finish, 2500);
    return rep;
  }

  function flow(rep, staging, nodes, model, box, opts) {
    var book = el("div", "ovr-book");
    rep.insertBefore(book, staging);
    var n = 1;
    var body = makeSheet(book, model, box, n);
    var limit = box.height - box.margin - box.bottom;

    function fits() { return body.scrollHeight <= limit + 1; }
    function next() { n += 1; body = makeSheet(book, model, box, n); return body; }
    function place(node) {
      body.appendChild(node);
      if (!fits() && body.childNodes.length > 1) { body.removeChild(node); next().appendChild(node); }
    }

    nodes.forEach(function (node) {
      if (!node.classList.contains("ovr-section")) { place(node); return; }
      if (node.classList.contains("ovr-break") && body.childNodes.length) next();
      var grid = node.querySelector(".ovr-grid");
      var cards = Array.prototype.slice.call(grid.children);
      // Group cards into rows by their width fraction.
      var FR = { full: 1, half: 0.5, third: 1 / 3, two_thirds: 2 / 3 };
      var rows = [], row = [], used = 0;
      cards.forEach(function (c) {
        var w = "full";
        Object.keys(FR).forEach(function (k) { if (c.classList.contains("ovr-w-" + k)) w = k; });
        var f = c.classList.contains("ovr-r-table") ? 1 : FR[w];
        if (row.length && used + f > 1.001) { rows.push(row); row = []; used = 0; }
        row.push(c); used += f;
      });
      if (row.length) rows.push(row);
      cards.forEach(function (c) { grid.removeChild(c); });
      // Section head (+ description + first row) must start together.
      var part = node, partGrid = grid;
      body.appendChild(part);
      (rows.shift() || []).forEach(function (c) { partGrid.appendChild(c); });
      if (!fits() && body.childNodes.length > 1) { body.removeChild(part); next().appendChild(part); }
      rows.forEach(function (r) {
        r.forEach(function (c) { partGrid.appendChild(c); });
        if (fits()) return;
        r.forEach(function (c) { partGrid.removeChild(c); });
        // Continue the section on a new sheet, without repeating its head.
        var cont = node.cloneNode(false);
        cont.classList.add("ovr-section-cont");
        cont.removeAttribute("id");
        partGrid = el("div", "ovr-grid");
        cont.appendChild(partGrid);
        next().appendChild(cont);
        r.forEach(function (c) { partGrid.appendChild(c); });
        part = cont;
      });
    });
    rep.removeChild(staging);
    if (opts.onPaged) opts.onPaged(book, n);
  }

  root.Ovid2ReportRender = { render: render, applyTheme: applyTheme, applyStyle: applyStyle, pageBox: pageBox };
})(typeof window !== "undefined" ? window : this);
