/* Submit-page interactivity: storage mode toggle + pipeline run plan.
 *
 * Two concerns, both vanilla ES5-ish (no build step):
 *
 * 1. Storage mode toggle — each Input / Output card has a
 *    "Saved address" vs "Browse storage" switch. Flipping it shows the
 *    matching pane and writes the mode into the card's hidden
 *    ``<name>_mode`` input so the POST handler knows how to resolve the
 *    endpoint.
 *
 * 2. Pipeline run plan — when the pipeline <select> changes, fetch
 *    GET /api/v1/pipelines/{pipeline}/plan and render:
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

  // ------------------------------------------------------------------
  // 1. Storage mode toggle
  // ------------------------------------------------------------------

  function bindStorageCards() {
    var cards = document.querySelectorAll("[data-storage-card]");
    for (var i = 0; i < cards.length; i++) {
      (function (card) {
        var role = card.getAttribute("data-storage-card"); // input | output
        var modeField = card.querySelector('input[name="' + role + '_mode"]');
        var btns = card.querySelectorAll(".mode-btn");
        card.addEventListener("click", function (evt) {
          var btn = evt.target.closest(".mode-btn");
          if (!btn) return;
          evt.preventDefault();
          var mode = btn.getAttribute("data-mode");
          for (var j = 0; j < btns.length; j++) {
            btns[j].classList.toggle("active", btns[j] === btn);
          }
          var panes = card.querySelectorAll(".mode-pane");
          for (var k = 0; k < panes.length; k++) {
            panes[k].hidden = panes[k].getAttribute("data-pane") !== mode;
          }
          if (modeField) modeField.value = mode;
          // Leaving browse (or re-entering it) clears any stale
          // validation error; the submit-time check re-evaluates.
          if (mode !== "browse") {
            card.classList.remove("invalid");
            var err = card.querySelector("[data-role=picker-error]");
            if (err) { err.hidden = true; err.textContent = ""; }
          }
        });
      })(cards[i]);
    }
  }

  // ------------------------------------------------------------------
  // 1b. Storage browse-mode validation (hard block)
  // ------------------------------------------------------------------

  // When a card is in "Browse storage" mode the operator MUST have
  // picked a storage account, otherwise the server rejects the submit
  // ("Pick a storage account to browse before submitting."). Catch that
  // here first and block the submit with an inline, per-card message so
  // the operator sees exactly which side is unfinished. Registered
  // before bindSerialize so an invalid submit never reaches
  // serialization.
  function bindStorageValidation() {
    var form = document.getElementById("submit-form");
    if (!form) return;
    form.addEventListener("submit", function (evt) {
      var cards = document.querySelectorAll("[data-storage-card]");
      var firstBad = null;
      for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var role = card.getAttribute("data-storage-card"); // input | output
        var modeField = card.querySelector('input[name="' + role + '_mode"]');
        var connField = card.querySelector('input[name="' + role + '_connection"]');
        var isBrowse = modeField && modeField.value === "browse";
        var hasConn = connField && connField.value.trim() !== "";
        var err = card.querySelector("[data-role=picker-error]");
        if (isBrowse && !hasConn) {
          card.classList.add("invalid");
          if (err) {
            err.textContent =
              "Select a storage account for this side, or switch to "
              + "\u201cSaved address\u201d.";
            err.hidden = false;
          }
          if (!firstBad) firstBad = card;
        } else {
          card.classList.remove("invalid");
          if (err) { err.hidden = true; err.textContent = ""; }
        }
      }
      if (firstBad) {
        evt.preventDefault();
        evt.stopImmediatePropagation();
        if (firstBad.scrollIntoView) {
          firstBad.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      }
    });
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

  // Read the resolved input source: either a saved address <select> or
  // the browse-mode connection + location hidden fields.
  function currentInputSource() {
    var card = document.querySelector('[data-storage-card="input"]');
    if (!card) return null;
    var modeField = card.querySelector('input[name="input_mode"]');
    var mode = modeField ? modeField.value : "address";
    if (mode === "browse") {
      var conn = card.querySelector('input[name="input_connection"]');
      var loc = card.querySelector('input[name="input_location"]');
      var connVal = conn ? conn.value.trim() : "";
      if (!connVal) return null;
      return { connection: connVal, location: loc ? loc.value.trim() : "" };
    }
    var sel = card.querySelector('select[name="input_address"]');
    var addr = sel ? sel.value.trim() : "";
    if (!addr) return null;
    return { address: addr };
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

  // React to the inline kind toggle: pick the right Tables data source
  // and lazily load the checks catalogue for health kinds.
  function bindKindChange() {
    document.addEventListener("ovid2:kind-change", function (evt) {
      applyKind((evt.detail && evt.detail.kind) || "etl");
    });
    // When the input source changes in copy/move mode, re-list tables.
    var inputCard = document.querySelector('[data-storage-card="input"]');
    if (inputCard) {
      inputCard.addEventListener("change", function () {
        if (isCopyOrMove(currentKind)) loadSourceTables();
      });
    }
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
      var sel = document.getElementById("pipeline-select");
      if (sel && sel.value) loadPlan(sel.value);
    }
  }

  function loadPlan(pipeline) {
    var form = document.getElementById("submit-form");
    var base = (form && form.getAttribute("data-plan-url")) || "/api/v1/pipelines/";
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
      });
  }

  function bindPipeline() {
    var sel = document.getElementById("pipeline-select");
    if (!sel) return;
    sel.addEventListener("change", function () {
      // For copy/move the Tables picker is driven by the *source*, not
      // the pipeline plan; for health kinds there is no CDM plan.
      if (isCopyOrMove(currentKind) || isHealth(currentKind)) return;
      loadPlan(sel.value);
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
  }

  // Serialise checked boxes to hidden fields on submit. When every
  // available box is checked, collapse to "all" (matches the CLI's
  // --tables all / --transformations all shortcut).
  function bindSerialize() {
    var form = document.getElementById("submit-form");
    if (!form) return;
    form.addEventListener("submit", function () {
      var kind = (document.getElementById("kind") || {}).value || "etl";
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
    bindStorageCards();
    bindStorageValidation();
    bindKindChange();
    bindPipeline();
    bindToolbar();
    bindChecksToolbar();
    bindSerialize();
    // Sync the initial picker to whatever kind the form loaded with (the
    // inline ovid2_toggle_submit_fields() ran before our listener bound).
    applyKind((document.getElementById("kind") || {}).value || "etl");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
