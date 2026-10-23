/*
 * Ovid2 control panel — right-rail Appearance tool.
 *
 * Owns two things:
 *
 *   1. The right TOOLS RAIL + expandable PANEL controller. The rail is a
 *      thin, always-visible vertical bar of tool buttons pinned to the
 *      right edge. Clicking a tool button toggles the panel, which
 *      slides in from the right and SHRINKS the main content (via the
 *      right-panel-open class + CSS padding) rather than overlaying it.
 *      A dedicated collapse button toggles the same state; its chevron
 *      direction (handled in CSS) indicates where the panel will move
 *      on the next toggle. State persists to localStorage
 *      (ovid2RightPanelOpen). On mobile the panel overlays with a scrim.
 *
 *   2. The APPEARANCE EDITOR rendered inside the panel: pick one of the
 *      six base themes, then restyle five component groups
 *      independently (colour + font size + font weight + font family),
 *      with LIVE preview. Overrides (a flat map of CSS-var name -> value)
 *      and named saved themes (base theme + all overrides) are stored
 *      per user ON THE SERVER through the store in theme.js
 *      (/api/v1/me/appearance) and re-applied before first paint on
 *      every page, on any browser the user signs in from.
 *
 * The assistant (llm_panel.js) drives the same stores through
 * window.Ovid2Appearance (set_appearance / save_appearance /
 * apply_saved_appearance UI actions); ALLOWED_VARS is the allow-list it
 * validates against, and refresh() re-seeds an open editor.
 *
 * Depends on the shared theme library window.Ovid2Theme (theme.js),
 * which owns the theme list, colour maths, and applyTheme/
 * applyCustomOverrides primitives. Classic (non-module) script loaded
 * with <script defer> after theme.js.
 */

(function () {
  "use strict";

  var PANEL_KEY = "ovid2RightPanelOpen";

  var html = document.documentElement;

  var mobileMql = null;
  try { mobileMql = window.matchMedia("(max-width: 899.98px)"); } catch (e) { /* ignore */ }
  function isMobile() { return mobileMql ? mobileMql.matches : false; }

  /* Curated, locally-available font families (no web-font fetch). Each
   * value is a full CSS font stack ending in a generic family so the
   * choice degrades gracefully on systems missing the preferred face.
   * The first value is a sentinel meaning "leave the group's default". */
  var FONT_CHOICES = [
    { value: "", label: "Default" },
    // Sans-serif faces.
    { value: "var(--font-sans)", label: "System sans" },
    { value: '"Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif', label: "Segoe UI / Helvetica" },
    { value: 'Arial, "Helvetica Neue", Helvetica, sans-serif', label: "Arial" },
    { value: 'Verdana, Geneva, Tahoma, sans-serif', label: "Verdana" },
    { value: 'Tahoma, Geneva, Verdana, sans-serif', label: "Tahoma" },
    { value: '"Trebuchet MS", "Segoe UI", Tahoma, sans-serif', label: "Trebuchet MS" },
    { value: '"Gill Sans", "Gill Sans MT", Calibri, sans-serif', label: "Gill Sans" },
    { value: 'Optima, Candara, "Segoe UI", sans-serif', label: "Optima / Candara" },
    // Serif faces.
    { value: "var(--font-serif)", label: "Serif" },
    { value: '"Times New Roman", Times, serif', label: "Times New Roman" },
    { value: 'Georgia, Cambria, "Times New Roman", serif', label: "Georgia" },
    { value: '"Palatino Linotype", Palatino, "Book Antiqua", serif', label: "Palatino" },
    { value: 'Garamond, "Apple Garamond", "Times New Roman", serif', label: "Garamond" },
    { value: 'Baskerville, "Baskerville Old Face", Georgia, serif', label: "Baskerville" },
    // Monospace faces.
    { value: "var(--mono)", label: "Monospace" },
    { value: '"Courier New", Courier, monospace', label: "Courier New" },
    { value: 'Consolas, "Lucida Console", Monaco, monospace', label: "Consolas / Monaco" },
    { value: 'Menlo, "DejaVu Sans Mono", "Liberation Mono", monospace', label: "Menlo" },
    // Display / decorative.
    { value: "var(--font-display)", label: "Display (theme brand face)" },
    { value: '"Orbitron", sans-serif', label: "Orbitron (sci-fi)" },
    { value: '"Fredoka", sans-serif', label: "Fredoka (rounded)" },
    { value: '"Baloo 2", sans-serif', label: "Baloo 2 (chunky)" },
    { value: '"VT323", monospace', label: "VT323 (terminal)" },
    { value: '"Inter", sans-serif', label: "Inter (clean UI)" },
    { value: '"Exo 2", sans-serif', label: "Exo 2 (techno)" },
    { value: '"Nunito", sans-serif', label: "Nunito (rounded sans)" },
    { value: '"Comic Sans MS", "Comic Sans", cursive', label: "Comic Sans" },
    { value: 'Impact, Haettenschweiler, "Arial Narrow Bold", sans-serif', label: "Impact" },
    { value: '"Brush Script MT", "Segoe Script", cursive', label: "Brush Script" }
  ];

  var WEIGHT_CHOICES = [
    { value: "", label: "Default" },
    { value: "300", label: "Light (300)" },
    { value: "400", label: "Regular (400)" },
    { value: "500", label: "Medium (500)" },
    { value: "600", label: "Semibold (600)" },
    { value: "700", label: "Bold (700)" }
  ];

  /* The five grouped components. Each declares which CSS custom
   * properties the editor may override. ``color`` maps a swatch to one
   * or more colour tokens; ``ff`` / ``fs`` / ``fw`` map the font
   * controls to their tokens (omitted where a group has no such knob).
   * ``ref`` is the token read back to seed the colour input. */
  var GROUPS = [
    {
      id: "surface",
      name: "Surface & background",
      color: { vars: ["--bg", "--bg-elev", "--card-bg"], ref: "--bg" }
      // surface has no typography of its own
    },
    {
      id: "text",
      name: "Body text",
      color: { vars: ["--fg"], ref: "--fg" },
      ff: "--body-ff", fs: "--body-fs", fw: "--body-fw", fsRef: "--body-fs"
    },
    {
      id: "accent",
      name: "Accent & buttons",
      color: { vars: ["--accent"], ref: "--accent", derive: "accent" },
      ff: "--btn-ff", fs: "--btn-fs", fw: "--btn-fw", fsRef: "--btn-fs"
    },
    {
      id: "cards",
      name: "Cards & tables",
      color: { vars: ["--card-bg"], ref: "--card-bg" },
      ff: "--card-ff", fs: "--card-fs", fw: "--card-fw", fsRef: "--table-fs"
    },
    {
      id: "nav",
      name: "Navigation",
      color: { vars: ["--drawer-bg"], ref: "--drawer-bg" },
      ff: "--nav-ff", fs: "--nav-fs", fw: "--nav-fw", fsRef: "--nav-fs"
    }
  ];

  var lib = window.Ovid2Theme || {};

  /* Every CSS custom property the appearance store may hold. The pre-paint
   * snippet in base.html and the assistant's set_appearance action are both
   * held to this list (the latter in llm_panel.js; pinned by
   * tests/contract/test_agent_tools_catalogue.py against catalogue.yaml). */
  var ALLOWED_VARS = ["--bg", "--bg-elev", "--card-bg", "--fg", "--accent",
                      "--accent-hover", "--accent-subtle", "--accent-fg",
                      "--drawer-bg",
                      "--body-ff", "--body-fs", "--body-fw",
                      "--btn-ff", "--btn-fs", "--btn-fw",
                      "--card-ff", "--card-fs", "--card-fw", "--table-fs",
                      "--nav-ff", "--nav-fs", "--nav-fw"];

  /* ---- Appearance override store --------------------------------- */

  // The store itself lives in theme.js (server-backed, in memory); these
  // are thin wrappers so the editor and the assistant share one copy.
  function readAppearance() { return lib.readAppearance ? lib.readAppearance() : {}; }
  function writeAppearance(map) { if (lib.writeAppearance) { lib.writeAppearance(map || {}); } }

  // Store ``map`` as the appearance overrides and repaint everything.
  // Kept as the editor's live-preview entry point.
  function applyAppearance(map) {
    writeAppearance(map);
    repaint();
  }

  // Repaint every inline override from the store in one fixed order:
  // quick colours, then per-component overrides (theme.js paint()).
  function repaint() { if (lib.repaint) { lib.repaint(); } }

  /* ---- Editor UI ------------------------------------------------- */

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }

  function seedColor(ref) {
    var map = readAppearance();
    if (map[colorVarKey(ref)]) { return map[colorVarKey(ref)]; }
    return lib.currentVar ? lib.currentVar(ref) : "#000000";
  }

  // The colour override for a group is keyed on its primary ``ref``
  // token, but written across all the group's colour vars.
  function colorVarKey(ref) { return ref; }

  function buildThemeList(container) {
    var section = el("div", "appearance-section");
    section.appendChild(el("p", "appearance-section-head", "Base theme"));
    var list = el("div", "theme-list");
    (lib.THEMES || []).forEach(function (theme) {
      var row = el("button", "theme-row");
      row.type = "button";
      row.setAttribute("data-theme-slug", theme.slug);
      var strip = el("span", "theme-swatch-strip");
      strip.setAttribute("aria-hidden", "true");
      ["bg", "surface", "accent", "fg"].forEach(function (k) {
        var chip = el("span", "theme-swatch");
        chip.style.background = theme.swatch[k];
        strip.appendChild(chip);
      });
      row.appendChild(strip);
      row.appendChild(el("span", "theme-row-label", theme.label));
      row.addEventListener("click", function () {
        // Selecting a base theme discards earlier colour/font overrides
        // (quick colours and per-component) so the Components editor
        // below reflects THIS theme's own token values. Same rule as the
        // assistant's set_appearance.
        if (lib.writeCustom) { lib.writeCustom({}); }
        writeAppearance({});
        if (lib.applyTheme) { lib.applyTheme(theme.slug); }
        markActiveTheme(container);
        // Re-seed colour inputs so they reflect the new theme's tokens.
        seedAllInputs(container);
        updateContrast(container);
      });
      list.appendChild(row);
    });
    section.appendChild(list);
    container.appendChild(section);
  }

  function markActiveTheme(container) {
    var slug = lib.currentTheme ? lib.currentTheme() : "clinical";
    var rows = container.querySelectorAll("[data-theme-slug]");
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.toggle("active",
        rows[i].getAttribute("data-theme-slug") === slug);
    }
  }

  function selectField(labelText, tokenKey, choices) {
    var label = el("span", "comp-control-label", labelText);
    var sel = document.createElement("select");
    sel.setAttribute("data-token", tokenKey);
    choices.forEach(function (c) {
      var opt = document.createElement("option");
      opt.value = c.value;
      opt.textContent = c.label;
      sel.appendChild(opt);
    });
    return { label: label, control: sel };
  }

  function buildGroup(container, group) {
    var box = el("div", "comp-group");
    box.setAttribute("data-group", group.id);

    var head = el("div", "comp-group-head");
    head.appendChild(el("span", "comp-group-name", group.name));
    var reset = el("button", "comp-group-reset", "Reset");
    reset.type = "button";
    head.appendChild(reset);
    box.appendChild(head);

    var controls = el("div", "comp-controls");

    if (group.color) {
      controls.appendChild(el("span", "comp-control-label", "Color"));
      var color = document.createElement("input");
      color.type = "color";
      color.setAttribute("data-color-ref", group.color.ref);
      controls.appendChild(color);
    }
    if (group.fs) {
      controls.appendChild(el("span", "comp-control-label", "Font size (px)"));
      var size = document.createElement("input");
      size.type = "number";
      size.min = "8";
      size.max = "48";
      size.step = "1";
      size.setAttribute("data-token", group.fs);
      size.setAttribute("data-token-unit", "px");
      controls.appendChild(size);
    }
    if (group.fw) {
      var w = selectField("Weight", group.fw, WEIGHT_CHOICES);
      controls.appendChild(w.label);
      controls.appendChild(w.control);
    }
    if (group.ff) {
      var f = selectField("Font", group.ff, FONT_CHOICES);
      controls.appendChild(f.label);
      controls.appendChild(f.control);
    }

    box.appendChild(controls);
    container.appendChild(box);

    reset.addEventListener("click", function () {
      resetGroup(container, group);
    });
  }

  function resetGroup(container, group) {
    var map = readAppearance();
    if (group.color) {
      group.color.vars.forEach(function (v) { delete map[v]; });
      // also drop derived accent companions
      if (group.color.derive === "accent") {
        delete map["--accent-hover"];
        delete map["--accent-subtle"];
        delete map["--accent-fg"];
      }
    }
    ["ff", "fs", "fw"].forEach(function (k) {
      if (group[k]) { delete map[group[k]]; }
    });
    applyAppearance(map);
    seedAllInputs(container);
    updateContrast(container);
  }

  function pxToNumber(value) {
    if (!value) { return ""; }
    var m = /([\d.]+)\s*px/.exec(value);
    if (m) { return m[1]; }
    return "";
  }

  function seedAllInputs(container) {
    var map = readAppearance();
    GROUPS.forEach(function (group) {
      var box = container.querySelector('.comp-group[data-group="' + group.id + '"]');
      if (!box) { return; }
      if (group.color) {
        var ci = box.querySelector('input[type="color"]');
        if (ci) {
          ci.value = map[group.color.ref] ||
            (lib.currentVar ? lib.currentVar(group.color.ref) : "#000000");
        }
      }
      if (group.fs) {
        var si = box.querySelector('input[type="number"]');
        if (si) {
          si.value = map[group.fs]
            ? pxToNumber(map[group.fs])
            : pxToNumber(readComputedPx(group.fsRef || group.fs));
        }
      }
      ["fw", "ff"].forEach(function (k) {
        if (!group[k]) { return; }
        var sel = box.querySelector('select[data-token="' + group[k] + '"]');
        if (sel) { sel.value = map[group[k]] || ""; }
      });
    });
    markActiveTheme(container);
  }

  function readComputedPx(token) {
    try {
      return getComputedStyle(html).getPropertyValue(token).trim();
    } catch (e) {
      return "";
    }
  }

  function onEditorInput(container, ev) {
    var t = ev.target;
    if (!t || !t.getAttribute) { return; }
    var map = readAppearance();

    if (t.type === "color") {
      var ref = t.getAttribute("data-color-ref");
      if (!ref) { return; }
      // Find the group by ref, write all its colour vars.
      GROUPS.forEach(function (g) {
        if (g.color && g.color.ref === ref) {
          g.color.vars.forEach(function (v) { map[v] = t.value; });
        }
      });
    } else {
      var token = t.getAttribute("data-token");
      if (!token) { return; }
      var unit = t.getAttribute("data-token-unit") || "";
      var val = t.value;
      if (val === "" || val == null) {
        delete map[token];
      } else {
        map[token] = unit ? (val + unit) : val;
      }
    }
    applyAppearance(map);
    updateContrast(container);
  }

  function updateContrast(container) {
    var warn = container.querySelector(".appearance-contrast-warning");
    if (!warn || !lib.contrastRatio) { return; }
    var map = readAppearance();
    var fg = map["--fg"] || (lib.currentVar ? lib.currentVar("--fg") : null);
    var bg = map["--bg"] || (lib.currentVar ? lib.currentVar("--bg") : null);
    if (!fg || !bg) { warn.hidden = true; return; }
    warn.hidden = lib.contrastRatio(fg, bg) >= 4.5;
  }

  /* ---- Saved themes ---------------------------------------------- */

  function readSaved() { return lib.readSaved ? lib.readSaved() : []; }
  function writeSaved(arr) { if (lib.writeSaved) { lib.writeSaved(arr || []); } }

  // Fold quick colours into the per-component map so a saved theme
  // reproduces exactly what is on screen (component overrides win, as
  // when painted). Used by the panel's Save and the assistant's
  // save_appearance alike.
  function foldedOverrides() {
    var map = {};
    var c = lib.readCustom ? lib.readCustom() : {};
    var ap = readAppearance();
    if (c.accent) { map["--accent"] = c.accent; }
    if (c.bg) { map["--bg"] = c.bg; }
    if (c.surface) { map["--bg-elev"] = c.surface; map["--card-bg"] = c.surface; }
    if (c.fg) { map["--fg"] = c.fg; }
    Object.keys(ap).forEach(function (k) { map[k] = ap[k]; });
    return map;
  }

  function buildSavedSection(container) {
    var section = el("div", "appearance-section");
    section.appendChild(el("p", "appearance-section-head", "Saved themes"));

    var list = el("div", "appearance-saved-list");
    section.appendChild(list);

    var saveRow = el("div", "appearance-save-row");
    var name = document.createElement("input");
    name.type = "text";
    name.placeholder = "Theme name";
    name.setAttribute("aria-label", "Theme name");
    var saveBtn = el("button", "button small primary", "Save");
    saveBtn.type = "button";
    saveRow.appendChild(name);
    saveRow.appendChild(saveBtn);
    section.appendChild(saveRow);

    container.appendChild(section);

    function refresh() {
      list.innerHTML = "";
      readSaved().forEach(function (entry, idx) {
        var row = el("div", "saved-theme-row");
        row.appendChild(el("span", "saved-theme-name", entry.name));
        var apply = el("button", null, "Apply");
        apply.type = "button";
        apply.addEventListener("click", function () {
          if (lib.writeCustom) { lib.writeCustom({}); }
          writeAppearance(entry.overrides || {});
          if (entry.theme && lib.applyTheme) { lib.applyTheme(entry.theme); }
          else { repaint(); }
          seedAllInputs(container);
          updateContrast(container);
        });
        var del = el("button", null, "Delete");
        del.type = "button";
        del.addEventListener("click", function () {
          var arr = readSaved();
          arr.splice(idx, 1);
          writeSaved(arr);
          refresh();
        });
        row.appendChild(apply);
        row.appendChild(del);
        list.appendChild(row);
      });
    }

    saveBtn.addEventListener("click", function () {
      var nm = (name.value || "").trim();
      if (!nm) { name.focus(); return; }
      var arr = readSaved();
      var entry = {
        name: nm,
        theme: lib.currentTheme ? lib.currentTheme() : "clinical",
        overrides: foldedOverrides()
      };
      // Overwrite a same-named theme rather than duplicating.
      var existing = -1;
      for (var i = 0; i < arr.length; i++) {
        if (arr[i].name === nm) { existing = i; break; }
      }
      if (existing >= 0) { arr[existing] = entry; }
      else { arr.push(entry); }
      writeSaved(arr);
      name.value = "";
      refresh();
    });

    refresh();
    refreshSaved = refresh;
  }

  function buildActions(container) {
    var actions = el("div", "appearance-actions");
    var resetAll = el("button", "button small", "Reset all");
    resetAll.type = "button";
    resetAll.addEventListener("click", function () {
      if (lib.writeCustom) { lib.writeCustom({}); }
      applyAppearance({});
      seedAllInputs(container);
      updateContrast(container);
    });
    actions.appendChild(resetAll);
    container.appendChild(actions);
  }

  var editorBuilt = false;
  var refreshSaved = null;

  /* Re-seed an already-built editor after an external change (e.g. the
   * assistant applied a theme). No-op when the editor was never opened. */
  function refreshEditor() {
    var container = document.getElementById("appearance-editor");
    if (!editorBuilt || !container) { return; }
    seedAllInputs(container);
    updateContrast(container);
    if (refreshSaved) { refreshSaved(); }
  }

  function buildEditor() {
    if (editorBuilt) { return; }
    var container = document.getElementById("appearance-editor");
    if (!container) { return; }

    buildThemeList(container);

    var compSection = el("div", "appearance-section");
    compSection.appendChild(el("p", "appearance-section-head", "Components"));
    GROUPS.forEach(function (g) { buildGroup(compSection, g); });
    var warn = el("p", "appearance-contrast-warning",
      "Low contrast — body text may be hard to read.");
    warn.hidden = true;
    compSection.appendChild(warn);
    container.appendChild(compSection);

    buildActions(container);
    buildSavedSection(container);

    // Live preview: any input/change inside the component section
    // updates immediately.
    compSection.addEventListener("input", function (ev) {
      onEditorInput(container, ev);
    });
    compSection.addEventListener("change", function (ev) {
      onEditorInput(container, ev);
    });

    editorBuilt = true;
    seedAllInputs(container);
    updateContrast(container);

    // Keep the theme highlight in sync if a theme changes elsewhere.
    document.addEventListener("ovid2:themechange", function () {
      markActiveTheme(container);
    });
  }

  /* ---- Rail + panel controller ----------------------------------- */

  function openPanel() {
    // Right-rail panels are mutually exclusive (assistant vs appearance).
    if (window.Ovid2Llm && typeof window.Ovid2Llm.close === "function") {
      window.Ovid2Llm.close();
    }
    html.classList.add("right-panel-open");
    try { localStorage.setItem(PANEL_KEY, "1"); } catch (e) { /* ignore */ }
    setExpanded(true);
    buildEditor();
  }

  function closePanel() {
    html.classList.remove("right-panel-open");
    try { localStorage.setItem(PANEL_KEY, "0"); } catch (e) { /* ignore */ }
    setExpanded(false);
  }

  function togglePanel() {
    if (html.classList.contains("right-panel-open")) { closePanel(); }
    else { openPanel(); }
  }

  function setExpanded(on) {
    var b = document.getElementById("tool-appearance");
    if (b) { b.setAttribute("aria-expanded", on ? "true" : "false"); }
    var panel = document.getElementById("tools-panel");
    if (panel) { panel.setAttribute("aria-hidden", on ? "false" : "true"); }
    var appBtn = document.getElementById("tool-appearance");
    if (appBtn) { appBtn.classList.toggle("active", on); }
  }

  function init() {
    // Reflect the pre-paint panel state onto the buttons + build the
    // editor eagerly if the panel started open.
    var startsOpen = html.classList.contains("right-panel-open");
    setExpanded(startsOpen);
    if (startsOpen) { buildEditor(); }

    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || typeof t.closest !== "function") { return; }
      if (t.closest("#tool-appearance")) {
        ev.preventDefault();
        togglePanel();
        return;
      }
      if (t.closest("[data-tools-close]") ||
          (t.classList && t.classList.contains("right-scrim"))) {
        closePanel();
      }
    });

    // ESC closes the panel on mobile (where it overlays).
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" &&
          isMobile() &&
          html.classList.contains("right-panel-open")) {
        closePanel();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.Ovid2Appearance = {
    open: openPanel,
    close: closePanel,
    toggle: togglePanel,
    applyAppearance: applyAppearance,
    readAppearance: readAppearance,
    writeAppearance: writeAppearance,
    readSaved: readSaved,
    writeSaved: writeSaved,
    refresh: refreshEditor,
    repaint: repaint,
    foldedOverrides: foldedOverrides,
    ALLOWED_VARS: ALLOWED_VARS.slice()
  };
})();
