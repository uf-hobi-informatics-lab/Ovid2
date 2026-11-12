/* Ovid2 Report Studio — render-model → DOM renderer.
 *
 * Shared by the Studio preview (/reports/{id}) and the self-contained HTML
 * export (inlined verbatim). Consumes the JSON render model produced by
 * ovid2.reports.compose.render_model, so the browser and every exporter
 * show the same report.
 *
 * Theme independence: everything is styled from model.theme via CSS
 * custom properties scoped to the .ovr-report root (--ovr-*). This file
 * and report_render.css never read the web page's theme tokens; pinned by
 * tests/contract/test_report_theme_independence.py.
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
    box.appendChild(v);
    box.appendChild(el("div", "ovr-kpi-label", k.label));
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
    if (t.truncated) wrap.appendChild(el("p", "ovr-muted", "Showing " + t.rows.length + " of " + t.total_rows + " rows."));
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
                            renderer: "svg", tooltip: true }).catch(function (e) {
        holder.textContent = "Chart failed: " + (e && e.message || e);
      });
    }, 0);
    return holder;
  }

  function renderBlock(b, model, opts) {
    var card = el("figure", "ovr-block ovr-w-" + (b.width || "full") + " ovr-r-" + b.renderer);
    card.setAttribute("data-block-id", b.id);
    if (b.title && b.renderer !== "kpi") card.appendChild(el("h3", "ovr-block-title", b.title));
    var body;
    if (b.renderer === "vega") body = renderVega(b, opts);
    else if (b.renderer === "kpi") body = renderKpi(b, model.theme);
    else if (b.renderer === "table") body = renderTable(b, model.theme);
    else if (b.renderer === "text" || b.renderer === "callout") {
      body = el("div", b.renderer === "callout" ? "ovr-callout ovr-tone-" + (b.tone || "info") : "ovr-text");
      body.innerHTML = b.html || "";  // server-rendered, raw HTML disabled
    } else body = el("p", "ovr-error", b.error || "Unavailable");
    card.appendChild(body);
    if (b.caption) card.appendChild(el("figcaption", "ovr-caption", b.caption));
    if (b.suppressed_cells) card.appendChild(el("p", "ovr-muted ovr-small", b.suppressed_cells + " small cell(s) suppressed."));
    if (opts.onBlock) opts.onBlock(card, b);
    return card;
  }

  function render(target, model, opts) {
    opts = opts || {};
    target.innerHTML = "";
    var rep = el("article", "ovr-report ovr-page-" + ((model.page || {}).orientation || "portrait"));
    applyTheme(rep, model.theme || {});
    var head = el("header", "ovr-head");
    head.appendChild(el("h1", "ovr-title", model.title));
    if (model.subtitle) head.appendChild(el("p", "ovr-subtitle", model.subtitle));
    if (model.description_html) { var d = el("div", "ovr-text"); d.innerHTML = model.description_html; head.appendChild(d); }
    rep.appendChild(head);
    if ((model.options || {}).show_toc && model.sections.length) {
      var toc = el("nav", "ovr-toc");
      toc.appendChild(el("h2", "ovr-toc-title", "Contents"));
      var ol = el("ol");
      model.sections.forEach(function (s) {
        var li = el("li"), a = el("a", null, s.title);
        a.href = "#ovr-s-" + s.id; li.appendChild(a); ol.appendChild(li);
      });
      toc.appendChild(ol); rep.appendChild(toc);
    }
    model.sections.forEach(function (s) {
      var sec = el("section", "ovr-section" + (s.page_break_before ? " ovr-break" : ""));
      sec.id = "ovr-s-" + s.id;
      sec.setAttribute("data-section-id", s.id);
      sec.appendChild(el("h2", "ovr-section-title", s.title));
      if (s.description_html) { var sd = el("div", "ovr-text"); sd.innerHTML = s.description_html; sec.appendChild(sd); }
      var grid = el("div", "ovr-grid");
      s.blocks.forEach(function (b) { grid.appendChild(renderBlock(b, model, opts)); });
      sec.appendChild(grid);
      if (opts.onSection) opts.onSection(sec, s);
      rep.appendChild(sec);
    });
    if ((model.options || {}).show_provenance !== false) {
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
      rep.appendChild(prov);
    }
    target.appendChild(rep);
    return rep;
  }

  root.Ovid2ReportRender = { render: render, applyTheme: applyTheme };
})(typeof window !== "undefined" ? window : this);
