/*
 * Ovid2 control panel — theme library (shared helpers).
 *
 * A tiny inline snippet in ``base.html`` runs before first paint to
 * apply ``document.documentElement[data-theme]`` and any saved custom
 * colour / appearance overrides from localStorage. That prevents a
 * flash of the wrong theme on reload. This deferred script is the
 * shared THEME LIBRARY: it owns the six base-theme definitions, the
 * WCAG colour maths, and the ``applyTheme`` / ``applyCustomOverrides``
 * primitives. The interactive UI (the right-rail Appearance panel that
 * lets a user pick a theme, restyle component groups, and save named
 * themes) lives in ``appearance.js`` and consumes this library through
 * ``window.Ovid2Theme``.
 *
 * State lives entirely in the browser (localStorage), so there is no
 * backend and no REST surface. Keys:
 *   ovid2Theme        the selected theme slug ("light" is the default)
 *   ovid2ThemeCustom  a JSON object of colour overrides, any subset of
 *                     { accent, bg, surface, fg }
 *   ovid2Appearance   a flat map of per-component CSS-var overrides
 *                     (owned by appearance.js; re-applied pre-paint)
 *
 * A ``ovid2:themechange`` CustomEvent is broadcast on every apply so
 * other consumers (charts, highlighters) can react.
 *
 * Kept as a classic (non-module) script so ``base.html`` can load it
 * with a plain ``<script defer>`` and the pre-paint snippet can reuse
 * the same storage keys.
 */

(function () {
  "use strict";

  var STORAGE_KEY = "ovid2Theme";
  var CUSTOM_KEY = "ovid2ThemeCustom";
  var CUSTOM_KEYS = ["accent", "bg", "surface", "fg"];

  // slug -> full theme metadata. swatch colours mirror the palette
  // token values authored in ovid2.css so the switcher preview matches
  // what a click actually applies. The six shipped themes.
  var THEMES = [
    { slug: "clinical", label: "Clinical", mode: "light",
      swatch: { bg: "#eef4f9", surface: "#ffffff", accent: "#0891b2", fg: "#0f1b26" } },
    { slug: "matrix", label: "Matrix", mode: "dark",
      swatch: { bg: "#000000", surface: "#050f07", accent: "#00ff41", fg: "#b6ffc4" } },
    { slug: "super-mario-world", label: "Super Mario World", mode: "light",
      swatch: { bg: "#6bb6ff", surface: "#ffffff", accent: "#e52521", fg: "#1b1b2f" } },
    { slug: "tatooine", label: "Star Wars / Tatooine", mode: "dark",
      swatch: { bg: "#14100b", surface: "#221a11", accent: "#f2a935", fg: "#f3e2c2" } },
    { slug: "financial", label: "Financial", mode: "dark",
      swatch: { bg: "#0d141b", surface: "#131d27", accent: "#16b877", fg: "#e8edf2" } },
    { slug: "lego", label: "LEGO", mode: "light",
      swatch: { bg: "#eef3f7", surface: "#ffffff", accent: "#d01012", fg: "#14202e" } }
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

  /* ---- Custom colour overrides ----------------------------------- */

  function readCustom() {
    try {
      var raw = localStorage.getItem(CUSTOM_KEY);
      if (!raw) { return {}; }
      var obj = JSON.parse(raw);
      if (!obj || typeof obj !== "object") { return {}; }
      var clean = {};
      CUSTOM_KEYS.forEach(function (k) {
        if (typeof obj[k] === "string" && hexToRgb(obj[k])) { clean[k] = obj[k]; }
      });
      return clean;
    } catch (e) {
      return {};
    }
  }

  function writeCustom(custom) {
    try {
      if (custom && Object.keys(custom).length) {
        localStorage.setItem(CUSTOM_KEY, JSON.stringify(custom));
      } else {
        localStorage.removeItem(CUSTOM_KEY);
      }
    } catch (e) { /* localStorage may be disabled; session-only */ }
  }

  // Apply the override object as inline CSS-var overrides on <html>.
  // Whether an explicit text colour was set decides if bg/surface get
  // an auto-contrast text; an explicit fg always wins (with a warning
  // surfaced separately by the editor).
  function applyCustomOverrides(custom) {
    var root = document.documentElement;
    var style = root.style;
    // Clear anything we might have set before, so Reset / theme
    // switches do not leave stale inline vars behind.
    ["--accent", "--accent-hover", "--accent-subtle", "--accent-fg",
     "--bg", "--bg-elev", "--card-bg", "--fg"].forEach(function (p) {
      style.removeProperty(p);
    });
    if (!custom) { return; }
    if (custom.accent) {
      style.setProperty("--accent", custom.accent);
      style.setProperty("--accent-hover", shade(custom.accent, 0.18));
      style.setProperty("--accent-subtle", rgbaFromHex(custom.accent, 0.14));
      style.setProperty("--accent-fg", readableInk(custom.accent));
    }
    if (custom.bg) {
      style.setProperty("--bg", custom.bg);
    }
    if (custom.surface) {
      style.setProperty("--bg-elev", custom.surface);
      style.setProperty("--card-bg", custom.surface);
    }
    if (custom.fg) {
      style.setProperty("--fg", custom.fg);
    } else if (custom.bg) {
      style.setProperty("--fg", readableInk(custom.bg));
    }
  }

  /* ---- Apply a theme --------------------------------------------- */

  function applyTheme(slug) {
    if (!isKnownSlug(slug)) { return; }
    document.documentElement.setAttribute("data-theme", slug);
    try {
      localStorage.setItem(STORAGE_KEY, slug);
    } catch (e) { /* session-only fallback */ }
    // Re-layer custom overrides on top of the freshly-applied theme.
    applyCustomOverrides(readCustom());
    try {
      document.dispatchEvent(new CustomEvent("ovid2:themechange", {
        detail: { theme: slug }
      }));
    } catch (e) { /* older browsers */ }
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

  function init() {
    // Settle whatever the pre-paint script applied (incl. custom vars).
    applyTheme(currentTheme());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
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
    applyCustomOverrides: applyCustomOverrides
  };
})();
