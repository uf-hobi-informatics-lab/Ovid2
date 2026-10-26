/*
 * Ovid2 control panel — theme library (shared helpers).
 *
 * A tiny inline snippet (``_appearance_prepaint.html``, included by
 * every full page) runs before first paint to apply
 * ``document.documentElement[data-theme]`` and the user's saved colour
 * / appearance overrides, which the SERVER renders into the page as
 * ``window.OVID2_APPEARANCE``. That prevents a flash of the wrong
 * theme on navigation. This deferred script is the
 * shared THEME LIBRARY: it owns the seven base-theme definitions, the
 * WCAG colour maths, and the ``applyTheme`` / ``applyCustomOverrides``
 * primitives. The interactive UI (the right-rail Appearance panel that
 * lets a user pick a theme, restyle component groups, and save named
 * themes) lives in ``appearance.js`` and consumes this library through
 * ``window.Ovid2Theme``.
 *
 * State is stored per user ON THE SERVER (``GET``/``PUT
 * /api/v1/me/appearance``) so the look follows the user to any browser
 * or laptop; nothing is kept in the browser. The document is:
 *   theme         the selected theme slug (null = clinical / matrix by
 *                 the OS colour-scheme preference)
 *   colors        quick colour overrides, any subset of
 *                 { accent, bg, surface, fg }
 *   appearance    a flat map of per-component CSS-var overrides
 *                 (edited through appearance.js)
 *   saved_themes  named looks [{ name, theme, overrides }]
 * Every change is PUT back (debounced for colour-picker drags). Values
 * left in localStorage by older releases are migrated once, then removed.
 *
 * A ``ovid2:themechange`` CustomEvent is broadcast on every apply so
 * other consumers (charts, highlighters) can react.
 *
 * Kept as a classic (non-module) script so ``base.html`` can load it
 * with a plain ``<script defer>``.
 */

(function () {
  "use strict";

  var STORAGE_KEY = "ovid2Theme";
  var CUSTOM_KEY = "ovid2ThemeCustom";
  var CUSTOM_KEYS = ["accent", "bg", "surface", "fg"];

  // slug -> full theme metadata. swatch colours mirror the palette
  // token values authored in ovid2.css so the switcher preview matches
  // what a click actually applies. The seven shipped themes.
  var THEMES = [
    { slug: "clinical", label: "Clinical", mode: "light",
      swatch: { bg: "#f4f7fa", surface: "#ffffff", accent: "#08759a", fg: "#0e1a24" } },
    { slug: "matrix", label: "Matrix", mode: "dark",
      swatch: { bg: "#000300", surface: "#020b04", accent: "#00ff41", fg: "#a8ffb8" } },
    { slug: "super-mario-world", label: "Super Mario World", mode: "light",
      swatch: { bg: "#5c94fc", surface: "#fffdf4", accent: "#d8000f", fg: "#231a12" } },
    { slug: "tatooine", label: "Star Wars / Tatooine", mode: "dark",
      swatch: { bg: "#170f0a", surface: "#22170f", accent: "#ff8a3d", fg: "#f6e6c8" } },
    { slug: "financial", label: "Financial", mode: "dark",
      swatch: { bg: "#07090c", surface: "#0e1218", accent: "#ffb000", fg: "#e6edf3" } },
    { slug: "lego", label: "LEGO", mode: "light",
      swatch: { bg: "#f6f5f0", surface: "#ffffff", accent: "#c8102e", fg: "#1a1a1a" } },
    { slug: "spaceship", label: "Spaceship", mode: "dark",
      swatch: { bg: "#04080a", surface: "#08141a", accent: "#2ee6c8", fg: "#d4f3ec" } }
  ];

  // Legacy slugs that predate the shipped-theme rename. Mapped to their
  // modern equivalents so an old localStorage value still resolves.
  // dune -> tatooine and industry -> lego cover the themes that were
  // replaced in the "movie/game" pass.
  var LEGACY_ALIASES = {
    light: "clinical",
    dark: "matrix",
    dune: "tatooine",
    industry: "lego"
  };

  function isKnownSlug(slug) {
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].slug === slug) { return true; }
    }
    return false;
  }

  function getTheme(slug) {
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].slug === slug) { return THEMES[i]; }
    }
    return null;
  }

  function currentTheme() {
    var attr = document.documentElement.getAttribute("data-theme");
    if (isKnownSlug(attr)) { return attr; }
    if (LEGACY_ALIASES[attr]) { return LEGACY_ALIASES[attr]; }
    return "clinical";
  }

  /* ---- Colour maths (WCAG legibility guardrail) ------------------ */

  function hexToRgb(hex) {
    if (typeof hex !== "string") { return null; }
    var h = hex.trim().replace(/^#/, "");
    if (h.length === 3) {
      h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    }
    if (!/^[0-9a-fA-F]{6}$/.test(h)) { return null; }
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16)
    };
  }

  // Relative luminance per WCAG 2.x (sRGB -> linear -> weighted sum).
  function relLuminance(rgb) {
    var chan = [rgb.r, rgb.g, rgb.b].map(function (v) {
      var s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2];
  }

  // WCAG contrast ratio between two hex colours (1..21). Returns 1 on
  // any parse failure so callers fail safe (treat as low contrast).
  function contrastRatio(hexA, hexB) {
    var a = hexToRgb(hexA);
    var b = hexToRgb(hexB);
    if (!a || !b) { return 1; }
    var la = relLuminance(a);
    var lb = relLuminance(b);
    var hi = Math.max(la, lb);
    var lo = Math.min(la, lb);
    return (hi + 0.05) / (lo + 0.05);
  }

  var INK_DARK = "#14181f";
  var INK_LIGHT = "#f5f7fb";

  // Readable ink to sit ON a given background: whichever of the dark /
  // light inks has the higher contrast against it.
  function readableInk(hexBg) {
    return contrastRatio(hexBg, INK_DARK) >= contrastRatio(hexBg, INK_LIGHT)
      ? INK_DARK
      : INK_LIGHT;
  }

  // Nudge a hex toward white (amount > 0) or black (amount < 0). Used
  // to synthesise --accent-hover and --accent-subtle from a custom
  // accent so the derived states stay coherent.
  function shade(hex, amount) {
    var rgb = hexToRgb(hex);
    if (!rgb) { return hex; }
    function mix(c) {
      var target = amount >= 0 ? 255 : 0;
      var v = Math.round(c + (target - c) * Math.abs(amount));
      return Math.max(0, Math.min(255, v));
    }
    function hh(v) { var s = v.toString(16); return s.length === 1 ? "0" + s : s; }
    return "#" + hh(mix(rgb.r)) + hh(mix(rgb.g)) + hh(mix(rgb.b));
  }

  function rgbaFromHex(hex, alpha) {
    var rgb = hexToRgb(hex);
    if (!rgb) { return hex; }
    return "rgba(" + rgb.r + ", " + rgb.g + ", " + rgb.b + ", " + alpha + ")";
  }

  /* ---- Preference store (server-backed, in memory) -------------- */

  // The server renders the signed-in user's saved appearance into the
  // page as window.OVID2_APPEARANCE (see _appearance_prepaint.html). This
  // module keeps it in memory, repaints from it, and PUTs every change
  // back to /api/v1/me/appearance. Nothing is stored in the browser.
  var PREFS_URL = "/api/v1/me/appearance";
  var VAR_RE = /^--[a-z0-9-]+$/;
  var SAVE_DEBOUNCE_MS = 400;
  // Keys earlier releases kept in localStorage. Read once to migrate an
  // existing look onto the server, then deleted.
  var LEGACY_KEYS = ["ovid2Theme", "ovid2ThemeCustom", "ovid2Appearance", "ovid2SavedThemes"];

  function cleanColors(obj) {
    var clean = {};
    if (!obj || typeof obj !== "object") { return clean; }
    CUSTOM_KEYS.forEach(function (k) {
      if (typeof obj[k] === "string" && HEX6.test(obj[k])) { clean[k] = obj[k]; }
    });
    return clean;
  }

  // Value shapes the server accepts (prefs_svc mirrors the set_appearance
  // catalogue entry). Filtering here means a stray value is dropped
  // instead of making the whole save fail.
  var HEX6 = /^#[0-9a-fA-F]{6}$/;
  var FONT_RE = /^(var\(--(font-sans|font-serif|mono|font-display)\)|[A-Za-z0-9 ,"'-]{1,160})$/;
  var SIZE_RE = /^[0-9]{1,2}(\.[0-9]{1,3})?(px|rem|em)$/;
  var WEIGHT_RE = /^[1-9]00$/;
  var RGBA_RE = /^rgba\(\d{1,3}, \d{1,3}, \d{1,3}, (0|1|0?\.\d{1,3})\)$/;

  function validVar(k, v) {
    if (typeof v !== "string" || !v || !VAR_RE.test(k)) { return false; }
    if (/-ff$/.test(k)) { return FONT_RE.test(v); }
    if (/-fs$/.test(k)) { return SIZE_RE.test(v); }
    if (/-fw$/.test(k)) { return WEIGHT_RE.test(v); }
    if (k === "--accent-subtle") { return RGBA_RE.test(v); }
    return HEX6.test(v);
  }

  function cleanVars(obj, allowed) {
    var clean = {};
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) { return clean; }
    Object.keys(obj).forEach(function (k) {
      if (validVar(k, obj[k]) && (!allowed || allowed.indexOf(k) !== -1)) {
        clean[k] = obj[k];
      }
    });
    return clean;
  }

  function cleanSaved(arr, allowed) {
    if (!Array.isArray(arr)) { return []; }
    var seen = {};
    var out = [];
    arr.forEach(function (e) {
      if (!e || typeof e.name !== "string" || !e.name.trim() || e.name.length > 60 ||
          /[<>]/.test(e.name) || seen[e.name]) { return; }
      var slug = LEGACY_ALIASES[e.theme] || e.theme;
      if (!isKnownSlug(slug)) { return; }
      seen[e.name] = true;
      out.push({ name: e.name, theme: slug, overrides: cleanVars(e.overrides, allowed) });
    });
    return out.slice(0, 30);
  }

  function normalise(p, allowed) {
    var out = { theme: null, colors: {}, appearance: {}, saved_themes: [] };
    if (!p || typeof p !== "object") { return out; }
    var slug = LEGACY_ALIASES[p.theme] || p.theme;
    if (isKnownSlug(slug)) { out.theme = slug; }
    out.colors = cleanColors(p.colors);
    out.appearance = cleanVars(p.appearance, allowed);
    out.saved_themes = cleanSaved(p.saved_themes, allowed);
    return out;
  }

  function isEmpty(p) {
    return !p.theme && !Object.keys(p.colors).length &&
      !Object.keys(p.appearance).length && !p.saved_themes.length;
  }

  function copy(o) { return JSON.parse(JSON.stringify(o)); }

  var prefs = normalise(window.OVID2_APPEARANCE);

  /* ---- Saving to the server -------------------------------------- */

  var saveTimer = null;
  var dirty = false;
  var statusEl = null;

  function setSaveStatus(msg) {
    if (!statusEl) {
      if (!msg || !document.body) { return; }
      statusEl = document.createElement("div");
      statusEl.className = "appearance-save-status";
      statusEl.setAttribute("role", "status");
      document.body.appendChild(statusEl);
    }
    statusEl.textContent = msg || "";
    statusEl.hidden = !msg;
  }

  // keepalive lets an in-flight save survive the user clicking a link.
  function flush() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (!dirty || typeof fetch !== "function") { return; }
    dirty = false;
    var body = JSON.stringify(prefs);
    fetch(PREFS_URL, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      credentials: "same-origin",
      keepalive: true,
      body: body
    }).then(function (r) {
      if (!r.ok) { throw new Error("HTTP " + r.status); }
      setSaveStatus("");
    }).catch(function (err) {
      setSaveStatus("Your appearance could not be saved to the server (" +
        ((err && err.message) || "network error") +
        "). It applies to this page only.");
    });
  }

  // Discrete actions (theme pick, saved-theme edits) save at once;
  // continuous ones (colour-picker drags) are debounced.
  function persist(immediate) {
    dirty = true;
    if (saveTimer) { clearTimeout(saveTimer); }
    saveTimer = setTimeout(flush, immediate ? 0 : SAVE_DEBOUNCE_MS);
  }

  window.addEventListener("pagehide", flush);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") { flush(); }
  });

  /* ---- Painting -------------------------------------------------- */

  // Inline vars this module last wrote, so a repaint removes exactly
  // those. Seeded with everything the pre-paint snippet may have set.
  var painted = ["--accent", "--accent-hover", "--accent-subtle", "--accent-fg",
                 "--bg", "--bg-elev", "--card-bg", "--fg"]
    .concat(Object.keys(prefs.appearance));

  function setAccent(set, hex) {
    set("--accent", hex);
    set("--accent-hover", shade(hex, 0.18));
    set("--accent-subtle", rgbaFromHex(hex, 0.14));
    set("--accent-fg", readableInk(hex));
  }

  // Repaint every inline override in one fixed order: the base theme's
  // stylesheet, then quick colours, then per-component overrides on top.
  function paint() {
    var style = document.documentElement.style;
    painted.forEach(function (p) { style.removeProperty(p); });
    painted = [];
    function set(k, v) { style.setProperty(k, v); painted.push(k); }
    var c = prefs.colors;
    if (c.accent) { setAccent(set, c.accent); }
    if (c.bg) { set("--bg", c.bg); }
    if (c.surface) { set("--bg-elev", c.surface); set("--card-bg", c.surface); }
    if (c.fg) { set("--fg", c.fg); }
    else if (c.bg) { set("--fg", readableInk(c.bg)); }
    var a = prefs.appearance;
    Object.keys(a).forEach(function (k) { set(k, a[k]); });
    if (a["--accent"] && hexToRgb(a["--accent"])) { setAccent(set, a["--accent"]); }
  }

  /* ---- Store accessors (used by appearance.js and llm_panel.js) --- */

  function readCustom() { return copy(prefs.colors); }
  function writeCustom(custom) { prefs.colors = cleanColors(custom); persist(false); }
  function readAppearance() { return copy(prefs.appearance); }
  function writeAppearance(map) { prefs.appearance = cleanVars(map); persist(false); }
  function readSaved() { return copy(prefs.saved_themes); }
  function writeSaved(arr) { prefs.saved_themes = cleanSaved(arr); persist(true); }

  // Kept for API compatibility: the argument is ignored because the
  // store is the single source of truth; this simply repaints from it.
  function applyCustomOverrides() { paint(); }

  /* ---- Apply a theme --------------------------------------------- */

  function applyTheme(slug) {
    if (!isKnownSlug(slug)) { return; }
    document.documentElement.setAttribute("data-theme", slug);
    prefs.theme = slug;
    paint();
    persist(true);
    try {
      document.dispatchEvent(new CustomEvent("ovid2:themechange", {
        detail: { theme: slug }
      }));
    } catch (e) { /* older browsers */ }
  }

  // One-time move of a look saved by an older release (localStorage)
  // onto the server, only when the server has nothing for this user.
  // Runs after appearance.js has loaded so its allow-list can filter.
  function migrateLegacy() {
    var legacy = null;
    try {
      var raw = {};
      LEGACY_KEYS.forEach(function (k) { raw[k] = localStorage.getItem(k); });
      LEGACY_KEYS.forEach(function (k) { localStorage.removeItem(k); });
      if (raw.ovid2Theme || raw.ovid2ThemeCustom || raw.ovid2Appearance || raw.ovid2SavedThemes) {
        var parse = function (v, d) { try { return v ? JSON.parse(v) : d; } catch (e) { return d; } };
        legacy = {
          theme: raw.ovid2Theme,
          colors: parse(raw.ovid2ThemeCustom, {}),
          appearance: parse(raw.ovid2Appearance, {}),
          saved_themes: parse(raw.ovid2SavedThemes, [])
        };
      }
    } catch (e) { return; }
    if (!legacy || !isEmpty(prefs)) { return; }
    var app = window.Ovid2Appearance;
    prefs = normalise(legacy, app && app.ALLOWED_VARS);
    if (prefs.theme) { document.documentElement.setAttribute("data-theme", prefs.theme); }
    paint();
    persist(true);
    if (app && app.refresh) { app.refresh(); }
  }

  /* ---- Small shared helpers exposed to the Appearance UI --------- */

  // Current computed value of a CSS custom property, coerced to a
  // ``#rrggbb`` string when it looks like a colour (so <input type=color>
  // can be seeded). Returns "#000000" on any failure.
  function currentVar(name) {
    try {
      var v = getComputedStyle(document.documentElement)
        .getPropertyValue(name).trim();
      return hexToRgb(v) ? (v[0] === "#" ? v : "#" + v) : "#000000";
    } catch (e) {
      return "#000000";
    }
  }

  // Settle the pre-paint result: same store, full ordered repaint
  // (derived accent shades included). Never saves on page load.
  paint();
  if (document.readyState === "complete") {
    migrateLegacy();
  } else {
    document.addEventListener("DOMContentLoaded", migrateLegacy);
  }

  // Shared theme library. The interactive Appearance panel (appearance.js)
  // consumes this; there is intentionally no UI wiring in this file.
  window.Ovid2Theme = {
    THEMES: THEMES,
    applyTheme: applyTheme,
    currentTheme: currentTheme,
    getTheme: getTheme,
    isKnownSlug: isKnownSlug,
    readableInk: readableInk,
    contrastRatio: contrastRatio,
    shade: shade,
    rgbaFromHex: rgbaFromHex,
    hexToRgb: hexToRgb,
    currentVar: currentVar,
    readCustom: readCustom,
    writeCustom: writeCustom,
    readAppearance: readAppearance,
    writeAppearance: writeAppearance,
    readSaved: readSaved,
    writeSaved: writeSaved,
    repaint: paint,
    flush: flush,
    applyCustomOverrides: applyCustomOverrides
  };
})();
