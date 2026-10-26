/* Submit-page option pickers (tables / transformations / checks).
 *
 * Vanilla ES5-ish (no build step). Driven by the job builder
 * (submit_builder.js) through document events:
 *
 *   - ``ovid2:kind-change``     {kind}      — run kind radio changed.
 *   - ``ovid2:pipeline-change`` {pipeline}  — a pipeline card was chosen.
 *   - ``ovid2:slot-change``     {role, …}   — a source/target was chosen
 *     (copy/move re-list the source's own tables).
 *
 * and emits ``ovid2:options-change`` whenever a selection changes so the
 * graph chips stay in sync.
 *
 * Pipeline run plan — when the pipeline changes, fetch
 *    GET /api/v1/pipeline-packages/{pipeline}/plan and render:
 *      - a checkbox per table (checked when it has a formatter),
 *      - a checkbox per transformation stage (all checked), with the
 *        per-table fixers listed read-only under the "fix" stage,
 *    then serialise the checked boxes into the hidden ``tables`` /
 *    ``transformations`` fields on submit ("all" when every box is
 *    checked, matching the CLI's ``--tables all`` shortcut).
 */
(function () {
  "use strict";

  function esc(s) {
    return String(s == null ? "" : s).replace(/[<>&"']/g, function (c) {
      return {
        "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;",
      }[c];
    });
  }

  var CHECK =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon"><polyline points="20 6 9 17 4 12"/></svg>';

  // Current run kind, read from the job-builder's kind radios.
  function currentKindValue() {
    var r = document.querySelector('input[name="kind"]:checked');
    return (r && r.value) || "etl";
  }

  // Tell the job builder (graph chips) that a picker changed.
  function notifyOptions() {
    document.dispatchEvent(new CustomEvent("ovid2:options-change"));
  }

  // ------------------------------------------------------------------
  // 2. Pipeline run plan
  // ------------------------------------------------------------------

  var state = { tables: [], stages: [], checks: [] };

  function renderTables(plan) {
    var list = document.getElementById("tables-list");
    var toolbar = document.getElementById("tables-toolbar");
    var badge = document.getElementById("tables-count");
    if (!list) return;
    var tables = plan.tables || [];
    state.tables = tables.map(function (t) { return t.name; });
    if (!tables.length) {
      list.innerHTML = '<p class="note">This pipeline has no CDM tables.</p>';
      if (toolbar) toolbar.hidden = true;
      return;
    }
    var html = "";
    for (var i = 0; i < tables.length; i++) {
      var t = tables[i];
      var checked = t.has_formatter ? " checked" : "";
      var cls = t.has_formatter ? "check-item" : "check-item no-formatter";
      var srcNote = t.has_formatter
        ? '<span class="src-tag">' + esc(t.source) + "</span>"
        : '<span class="src-tag muted">no formatter</span>';
      html += '<label class="' + cls + '" data-table="' + esc(t.name) + '">'
        + '<input type="checkbox" class="table-check" value="' + esc(t.name) + '"' + checked + ">"
        + '<span class="box">' + CHECK + "</span>"
        + '<span class="check-label">' + esc(t.name) + "</span>"
        + srcNote
        + "</label>";
    }
    list.innerHTML = html;
    if (toolbar) toolbar.hidden = false;
    updateTablesCount();
  }

  function updateTablesCount() {
    var badge = document.getElementById("tables-count");
    if (!badge) return;
    var boxes = document.querySelectorAll(".table-check");
    var checked = document.querySelectorAll(".table-check:checked");
    badge.textContent = checked.length + " / " + boxes.length + " selected";
    badge.hidden = false;
    notifyOptions();
  }

  function renderTransformations(plan) {
    var list = document.getElementById("transformations-list");
    if (!list) return;
    var stages = plan.transformations || [];
    state.stages = stages.slice();
    var fixers = plan.fixers || {};
    var multi = plan.multi_table_fixers || [];
    var html = "";
    for (var i = 0; i < stages.length; i++) {
      var stage = stages[i];
      html += '<div class="stage-card">'
        + '<label class="check-item stage-head">'
        + '<input type="checkbox" class="stage-check" value="' + esc(stage) + '" checked>'
        + '<span class="box">' + CHECK + "</span>"
        + '<span class="check-label">' + esc(stage) + "</span>"
        + "</label>";
      if (stage === "fix") {
        html += renderFixers(fixers, multi);
      }
      html += "</div>";
    }
    list.innerHTML = html;
    notifyOptions();
  }

  function renderFixers(fixers, multi) {
    var tables = Object.keys(fixers).sort();
    if (!tables.length && !multi.length) {
      return '<p class="note fixer-note">No fixers configured for this pipeline.</p>';
    }
    var html = '<details class="fixer-detail"><summary>Per-table fixers (read-only)</summary>';
    html += '<table class="fixer-table"><tbody>';
    for (var i = 0; i < tables.length; i++) {
      var tbl = tables[i];
      var chain = fixers[tbl] || [];
      var names = [];
      for (var j = 0; j < chain.length; j++) {
        var d = chain[j].detail ? " (" + esc(chain[j].detail) + ")" : "";
        names.push('<span class="fixer-pill">' + esc(chain[j].fix) + d + "</span>");
      }
      html += "<tr><td class=\"mono\">" + esc(tbl) + "</td><td>" + names.join(" ") + "</td></tr>";
    }
    if (multi.length) {
      var mnames = [];
      for (var k = 0; k < multi.length; k++) {
        mnames.push('<span class="fixer-pill">' + esc(multi[k].fix) + "</span>");
      }
      html += "<tr><td class=\"mono\">(multi-table)</td><td>" + mnames.join(" ") + "</td></tr>";
    }
    html += "</tbody></table></details>";
    return html;
  }

  // ------------------------------------------------------------------
  // 2b. Copy/move source tables + data-health checks pickers
  // ------------------------------------------------------------------

  // Current kind, tracked from the inline ovid2:kind-change event so the
  // pipeline <select> handler knows whether the Tables picker should be
  // filled from the pipeline's CDM plan (etl/analysis) or the copy/move
  // *source's* own tables (copy/move).
  var currentKind = "etl";

  function isCopyOrMove(kind) {
    return kind === "copy" || kind === "move";
  }
  function isHealth(kind) {
    return kind === "health" || kind === "dq_checks";
  }

  // Read the resolved input source from the job builder's hidden fields:
  // either a saved address or a browsed connection + location.
  function currentInputSource() {
    function val(name) {
      var f = document.querySelector('[name="' + name + '"]');
      return f ? String(f.value || "").trim() : "";
    }
    if (val("input_mode") === "browse") {
      if (!val("input_connection")) return null;
      return { connection: val("input_connection"), location: val("input_location") };
    }
    var addr = val("input_address");
    return addr ? { address: addr } : null;
  }

  // Fill the Tables picker from a copy/move source's own tables (e.g. an
  // Access .accdb file). Falls back to a helpful note when the source is
  // not enumerable (a plain folder, a Snowflake schema) so the operator
  // can still submit with the run-time "all" expansion.
  function loadSourceTables() {
    var list = document.getElementById("tables-list");
    var toolbar = document.getElementById("tables-toolbar");
    if (!list) return;
    var source = currentInputSource();
    if (!source) {
      state.tables = [];
      list.innerHTML =
        '<p class="note">Choose an input storage source above to list its tables.</p>';
      if (toolbar) toolbar.hidden = true;
      var badge0 = document.getElementById("tables-count");
      if (badge0) badge0.hidden = true;
      return;
    }
    var qs = source.address
      ? "address=" + encodeURIComponent(source.address)
      : "connection=" + encodeURIComponent(source.connection) +
        "&location=" + encodeURIComponent(source.location || "");
    list.innerHTML = '<p class="note">Listing source tables&hellip;</p>';
    if (toolbar) toolbar.hidden = true;
    fetch("/api/v1/sources/tables?" + qs, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (resp) {
        var names = (resp && resp.tables) || [];
        if (!resp || !resp.supported) {
          state.tables = [];
          list.innerHTML =
            '<p class="note">This source is not a self-describing table '
            + 'container. Every table will be copied ('
            + '<code>all</code>); pass an explicit list only if you need '
            + 'a subset.</p>';
          if (toolbar) toolbar.hidden = true;
          var badge1 = document.getElementById("tables-count");
          if (badge1) badge1.hidden = true;
          return;
        }
        if (!names.length) {
          state.tables = [];
          list.innerHTML =
            '<p class="note">' + esc(resp.message
              || "No tables found in this source.") + "</p>";
          if (toolbar) toolbar.hidden = true;
          return;
        }
        renderTables({ tables: names.map(function (n) {
          return { name: n, has_formatter: true, source: "source" };
        }) });
      })
      .then(notifyOptions, notifyOptions)
      .catch(function (err) {
        state.tables = [];
        list.innerHTML =
          '<p class="flash-error">Could not list source tables: '
          + esc(err && err.message) + "</p>";
        if (toolbar) toolbar.hidden = true;
      });
  }

  function renderChecks(checks) {
    var list = document.getElementById("checks-list");
    var toolbar = document.getElementById("checks-toolbar");
    if (!list) return;
    var rows = checks || [];
    state.checks = rows.map(function (c) { return String(c.id || ""); });
    if (!rows.length) {
      list.innerHTML = '<p class="note">No checks registered.</p>';
      if (toolbar) toolbar.hidden = true;
      return;
    }
    var html = "";
    for (var i = 0; i < rows.length; i++) {
      var c = rows[i];
      var id = String(c.id || "");
      var name = String(c.name || id);
      var part = String(c.part || "");
      var hay = (id + " " + name + " " + part).toLowerCase();
      html += '<label class="check-item" data-check="' + esc(hay) + '">'
        + '<input type="checkbox" class="check-check" value="' + esc(id) + '" checked>'
        + '<span class="box">' + CHECK + "</span>"
        + '<span class="check-label">' + esc(id)
        + (name && name !== id ? ' <span class="src-tag muted">' + esc(name) + "</span>" : "")
        + "</span>"
        + (part ? '<span class="src-tag">' + esc(part) + "</span>" : "")
        + "</label>";
    }
    list.innerHTML = html;
    if (toolbar) toolbar.hidden = false;
    updateChecksCount();
  }

  function updateChecksCount() {
    var badge = document.getElementById("checks-count");
    if (!badge) return;
    var boxes = document.querySelectorAll(".check-check");
    var checked = document.querySelectorAll(".check-check:checked");
    badge.textContent = checked.length + " / " + boxes.length + " selected";
    badge.hidden = false;
    notifyOptions();
  }

  function loadChecks() {
    var list = document.getElementById("checks-list");
    if (!list) return;
    if (state.checks.length) return; // already loaded
    list.innerHTML = '<p class="note">Loading checks&hellip;</p>';
    fetch("/api/v1/health/checks", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (rows) { renderChecks(rows || []); })
      .catch(function (err) {
        list.innerHTML =
          '<p class="flash-error">Could not load checks: '
          + esc(err && err.message) + "</p>";
      });
  }

  function bindChecksToolbar() {
    var toolbar = document.getElementById("checks-toolbar");
    var list = document.getElementById("checks-list");
    if (toolbar) {
      toolbar.addEventListener("click", function (evt) {
        var btn = evt.target.closest("[data-select]");
        if (!btn) return;
        evt.preventDefault();
        var want = btn.getAttribute("data-select") === "all";
        var boxes = document.querySelectorAll(".check-check");
        for (var i = 0; i < boxes.length; i++) {
          var label = boxes[i].closest(".check-item");
          if (label && label.style.display === "none") continue;
          boxes[i].checked = want;
        }
        updateChecksCount();
      });
      var filter = document.getElementById("checks-filter");
      if (filter) {
        filter.addEventListener("input", function () {
          var q = filter.value.toLowerCase();
          var items = document.querySelectorAll("#checks-list .check-item");
          for (var i = 0; i < items.length; i++) {
            var hay = items[i].getAttribute("data-check") || "";
            items[i].style.display = hay.indexOf(q) === -1 ? "none" : "";
          }
        });
      }
    }
    if (list) {
      list.addEventListener("change", function (evt) {
        if (evt.target && evt.target.classList.contains("check-check")) {
          updateChecksCount();
        }
      });
    }
  }

  // React to the job builder: kind radio, chosen pipeline, chosen source.
  function bindKindChange() {
    document.addEventListener("ovid2:kind-change", function (evt) {
      applyKind((evt.detail && evt.detail.kind) || "etl");
    });
    document.addEventListener("ovid2:slot-change", function (evt) {
      if (evt.detail && evt.detail.role === "input" && isCopyOrMove(currentKind)) {
        loadSourceTables();
      }
    });
  }

  // Apply a run kind: sync ``currentKind`` and populate the picker whose
  // data source matches the kind. Called both from the ovid2:kind-change
  // event and once at init (the inline toggle fires its first event
  // before this module's listener is bound, so we sync explicitly).
  function applyKind(kind) {
    currentKind = kind || "etl";
    if (isHealth(currentKind)) {
      loadChecks();
    } else if (isCopyOrMove(currentKind)) {
      loadSourceTables();
    } else {
      var pf = document.getElementById("pipeline-field");
      loadPlan(pf ? pf.value : "");
    }
  }

  function loadPlan(pipeline) {
    var form = document.getElementById("submit-form");
    var base = (form && form.getAttribute("data-plan-url")) || "/api/v1/pipeline-packages/";
    var tablesList = document.getElementById("tables-list");
    var transList = document.getElementById("transformations-list");
    if (!pipeline) {
      state.tables = []; state.stages = [];
      if (tablesList) tablesList.innerHTML = '<p class="note">Choose a pipeline to see its tables.</p>';
      if (transList) transList.innerHTML = '<p class="note">Choose a pipeline to see its transformation plan.</p>';
      var tb = document.getElementById("tables-toolbar");
      if (tb) tb.hidden = true;
      var badge = document.getElementById("tables-count");
      if (badge) badge.hidden = true;
      return;
    }
    if (tablesList) tablesList.innerHTML = '<p class="note">Loading tables&hellip;</p>';
    if (transList) transList.innerHTML = '<p class="note">Loading&hellip;</p>';
    fetch(base + encodeURIComponent(pipeline) + "/plan", {
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (plan) {
        renderTables(plan);
        renderTransformations(plan);
      })
      .catch(function (err) {
        if (tablesList) {
          tablesList.innerHTML =
            '<p class="flash-error">Could not load plan: ' + esc(err && err.message) + "</p>";
        }
        if (transList) transList.innerHTML = "";
        notifyOptions();
      });
  }

  function bindPipeline() {
    document.addEventListener("ovid2:pipeline-change", function (evt) {
      // For copy/move the Tables picker is driven by the *source*, not
      // the pipeline plan; for health kinds there is no CDM plan.
      if (isCopyOrMove(currentKind) || isHealth(currentKind)) return;
      loadPlan((evt.detail && evt.detail.pipeline) || "");
    });
  }

  function bindToolbar() {
    var toolbar = document.getElementById("tables-toolbar");
    var list = document.getElementById("tables-list");
    if (toolbar) {
      toolbar.addEventListener("click", function (evt) {
        var btn = evt.target.closest("[data-select]");
        if (!btn) return;
        evt.preventDefault();
        var want = btn.getAttribute("data-select") === "all";
        var boxes = document.querySelectorAll(".table-check");
        for (var i = 0; i < boxes.length; i++) {
          var label = boxes[i].closest(".check-item");
          if (label && label.style.display === "none") continue;
          boxes[i].checked = want;
        }
        updateTablesCount();
      });
      var filter = document.getElementById("tables-filter");
      if (filter) {
        filter.addEventListener("input", function () {
          var q = filter.value.toLowerCase();
          var items = document.querySelectorAll("#tables-list .check-item");
          for (var i = 0; i < items.length; i++) {
            var name = (items[i].getAttribute("data-table") || "").toLowerCase();
            items[i].style.display = name.indexOf(q) === -1 ? "none" : "";
          }
        });
      }
    }
    if (list) {
      list.addEventListener("change", function (evt) {
        if (evt.target && evt.target.classList.contains("table-check")) {
          updateTablesCount();
        }
      });
    }
    var stages = document.getElementById("transformations-list");
    if (stages) {
      stages.addEventListener("change", function (evt) {
        if (evt.target && evt.target.classList.contains("stage-check")) notifyOptions();
      });
    }
  }

  // Serialise checked boxes to hidden fields on submit. When every
  // available box is checked, collapse to "all" (matches the CLI's
  // --tables all / --transformations all shortcut).
  function bindSerialize() {
    var form = document.getElementById("submit-form");
    if (!form) return;
    form.addEventListener("submit", function () {
      var kind = currentKindValue();
      var isCopyOrMove = (kind === "copy" || kind === "move");

      var tField = document.getElementById("tables-field");
      var tBoxes = document.querySelectorAll(".table-check");
      if (tField && tBoxes.length) {
        var checked = [];
        for (var i = 0; i < tBoxes.length; i++) {
          if (tBoxes[i].checked) checked.push(tBoxes[i].value);
        }
        // For copy/move an explicit list is required (no "all"); for
        // etl-shaped kinds "all" is allowed when every box is ticked.
        if (!checked.length) {
          // Sentinel: FastAPI turns a bare "" back into the Form("all")
          // default, so we must send an explicit token the handler can
          // reject.
          tField.value = "__none__";
        } else if (!isCopyOrMove && checked.length === tBoxes.length) {
          tField.value = "all";
        } else {
          tField.value = checked.join(" ");
        }
      }

      var xField = document.getElementById("transformations-field");
      var xBoxes = document.querySelectorAll(".stage-check");
      if (xField && xBoxes.length) {
        var stages = [];
        for (var k = 0; k < xBoxes.length; k++) {
          if (xBoxes[k].checked) stages.push(xBoxes[k].value);
        }
        if (!stages.length) {
          xField.value = "__none__";
        } else {
          xField.value = (stages.length === xBoxes.length) ? "all" : stages.join(" ");
        }
      }

      // Health/dq_checks: individual check selection. An empty field (or
      // every box checked) means "run the whole selected report cycle";
      // a subset serialises to a space-separated id list which the
      // handler forwards as --checks (overriding the package).
      var cField = document.getElementById("checks-field");
      var cBoxes = document.querySelectorAll(".check-check");
      if (cField) {
        if (!cBoxes.length) {
          cField.value = "";
        } else {
          var picked = [];
          for (var m = 0; m < cBoxes.length; m++) {
            if (cBoxes[m].checked) picked.push(cBoxes[m].value);
          }
          // All checked => whole package (empty); a strict subset =>
          // explicit ids. Zero checked also means "whole package" here
          // (the coarse package select still applies) rather than an
          // error, so unticking everything can't produce an empty run.
          cField.value =
            picked.length && picked.length !== cBoxes.length
              ? picked.join(" ")
              : "";
        }
      }
    });
  }

  function init() {
    bindKindChange();
    bindPipeline();
    bindToolbar();
    bindChecksToolbar();
    bindSerialize();
    // Sync the initial picker to whatever kind the form loaded with.
    applyKind(currentKindValue());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
