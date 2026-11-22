/*
 * <ovid-mark> — the animated Ovid2 sprout logo.
 *
 * A dependency-free Web Component (SVG + CSS in shadow DOM). The sprout
 * spells OVID in one line: left leaf = O, centre almond = I, right leaf
 * = D; the O and D upper edges meet in a V that flows into a short
 * stem. The mark reports pipeline activity through colour and motion.
 *
 * Spec: .opencode/knowledge/delta/ovid-mark-spec.md (Claude Design
 * handoff). Geometry, timings and colours follow it exactly.
 *
 * API
 *   <ovid-mark state="idle" size="48"></ovid-mark>
 *   state:  idle | extract | transform | load | running | sync |
 *           success | warning | error          (default idle)
 *   size:   rendered width in px (default 48; height = 0.8 x width)
 *   silent: boolean attribute — do not announce state changes
 *   CSS vars (inherited from the page / theme):
 *     --ovid-base --ovid-extract --ovid-transform --ovid-load
 *     --ovid-success --ovid-warning --ovid-error --ovid-sync
 *     --ovid-rest-1 --ovid-rest-2 --ovid-rest-3   (rest gradient)
 *   el.flick()               replays the one-shot rustle (0.9 s)
 *   "ovid-state-change"      CustomEvent, detail { from, to }
 *   success returns to idle after 4 s unless another state arrives.
 *
 * Only transform, opacity, stroke-dashoffset and fill-opacity are
 * animated; colour changes transition over 350 ms. Under
 * prefers-reduced-motion nothing animates (colour changes only).
 *
 * Classic (non-module) script so templates can load it with <script defer>.
 */

(function () {
  "use strict";

  if (typeof window === "undefined" || !window.customElements) return;
  if (window.customElements.get("ovid-mark")) return;

  var STATES = ["idle", "extract", "transform", "load", "running",
                "sync", "success", "warning", "error"];

  var LABELS = {
    idle: "Idle",
    extract: "Extracting",
    transform: "Transforming",
    load: "Loading",
    running: "Pipeline running",
    sync: "Syncing",
    success: "Run succeeded",
    warning: "Data-quality warning",
    error: "Run failed"
  };

  /* Default colours (spec). Themes override through the CSS variables. */
  var COLORS = {
    base: "#1E3A6E",
    extract: "#2F6FD6",
    transform: "#7A4FD1",
    load: "#1F9A6B",
    success: "#1F9A6B",
    warning: "#C98A14",
    error: "#D2453B",
    sync: "#9DB0D3",
    rest1: "#9DB8EC",
    rest2: "#3F64B0",
    rest3: "#142C5C"
  };

  /* Geometry — viewBox 0 2 200 160. */
  var VIEWBOX = "0 2 200 160";
  var PATHS = {
    o: "M100 131.8 C76 80 40 60 10.9 73.9 C10 105 40 134 79.4 129.7",
    d: "M100 131.8 C124 80 160 60 189.1 73.9 C190 105 160 134 120.6 129.7",
    i: "M100 104.2 C137.24 75.73 137.24 37.77 100 9.3 C62.76 37.77 62.76 75.73 100 104.2Z",
    stem: "M100 131.8 L100 152",
    orbit: "M10.9 73.9 Q100 -55.3 189.1 73.9"
  };
  var TIPS = { o: [10.9, 73.9], i: [100, 9.3], d: [189.1, 73.9] };

  /* ---- packet keyframes, sampled along the real curves ---------------- */

  function bezier(p0, p1, p2, p3, t) {
    var u = 1 - t;
    return [
      u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
      u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]
    ];
  }

  function sample(fn, n) {
    var pts = [];
    for (var k = 0; k <= n; k++) pts.push(fn(k / n));
    return pts;
  }

  var V = [100, 131.8];
  var ROUTES = {
    // O tip -> V (reverse of the O upper edge).
    o: sample(function (t) { return bezier(V, [76, 80], [40, 60], TIPS.o, 1 - t); }, 10),
    // V -> I tip (straight up through the almond).
    i: sample(function (t) { return [100, V[1] + (TIPS.i[1] - V[1]) * t]; }, 10),
    // V -> D tip.
    d: sample(function (t) { return bezier(V, [124, 80], [160, 60], TIPS.d, t); }, 10)
  };

  function r1(x) { return Math.round(x * 10) / 10; }

  /* Keyframes moving a packet along ``pts`` between ``from``% and ``to``%
   * of the cycle, fading in / out at the ends and hidden elsewhere. */
  function packetKeyframes(name, pts, from, to) {
    var out = "@keyframes " + name + " {";
    var span = to - from;
    var fade = Math.min(6, span / 6);
    if (from > 0) {
      out += "0% { transform: translate(" + r1(pts[0][0]) + "px," + r1(pts[0][1]) + "px); opacity: 0; }";
    }
    pts.forEach(function (p, k) {
      var pct = from + (span * k) / (pts.length - 1);
      var op = 1;
      if (k === 0) op = 0;
      if (k === pts.length - 1) op = 0;
      out += r1(pct) + "% { transform: translate(" + r1(p[0]) + "px," + r1(p[1]) + "px); opacity: " + op + "; }";
      if (k === 0) {
        out += r1(pct + fade) + "% { opacity: 1; }";
      }
    });
    if (to < 100) {
      var last = pts[pts.length - 1];
      out += "100% { transform: translate(" + r1(last[0]) + "px," + r1(last[1]) + "px); opacity: 0; }";
    }
    return out + "}";
  }

  var CSS = [
    ":host { display: inline-block; line-height: 0; vertical-align: middle; }",
    ":host([hidden]) { display: none; }",
    "svg { display: block; overflow: visible; }",
    ".sr { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0;",
    "  overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; border: 0; }",
    "svg[data-tier='l'] { --sw: 4.5; }",
    "svg[data-tier='m'] { --sw: 6; }",
    "svg[data-tier='s'] { --sw: 11; }",
    ".edge, .stem { fill: none; stroke-width: var(--sw); stroke-linecap: round; stroke-linejoin: round;",
    "  transition: stroke .35s ease, opacity .35s ease; }",
    ".fill { stroke: none; fill: var(--ovid-base, " + COLORS.base + "); fill-opacity: 0;",
    "  transition: fill .35s ease, fill-opacity .35s ease; }",
    ".leaf { transition: opacity .35s ease; }",
    ".mark, .rot { transform-box: view-box; }",
    ".mark { transform-origin: 100px 152px; }",
    ".rot.o, .rot.d { transform-origin: 100px 131.8px; }",
    ".rot.i { transform-origin: 100px 104.2px; }",
    ".pk { fill: var(--ovid-base, " + COLORS.base + "); opacity: 0; }",
    ".node { fill: var(--ovid-sync, " + COLORS.sync + "); opacity: 0; transform-box: fill-box; transform-origin: center; }",
    ".orbit { fill: none; stroke: var(--ovid-sync, " + COLORS.sync + "); stroke-width: calc(var(--sw) * 0.5);",
    "  stroke-linecap: round; stroke-dasharray: 3 6; opacity: 0; transition: opacity .35s ease; }",
    "svg[data-tier='s'] .pk, svg[data-tier='s'] .node, svg[data-tier='s'] .orbit { display: none; }",

    /* idle */
    "svg[data-s='idle'] .mark { animation: ovm-breathe 4s ease-in-out infinite; }",
    "@keyframes ovm-breathe { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.03); } }",

    /* extract */
    "svg[data-s='extract'] .o .edge { stroke: var(--ovid-extract, " + COLORS.extract + "); stroke-dasharray: 14 9;",
    "  animation: ovm-dash .9s linear infinite; }",
    "svg[data-s='extract'] .leaf.i, svg[data-s='extract'] .leaf.d { opacity: .28; }",
    "svg[data-s='extract'] .pk-o { fill: var(--ovid-extract, " + COLORS.extract + "); animation: ovm-pk-o 1.2s linear infinite; }",
    "@keyframes ovm-dash { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -46; } }",

    /* transform */
    "svg[data-s='transform'] .i .edge { stroke: var(--ovid-transform, " + COLORS.transform + "); }",
    "svg[data-s='transform'] .leaf.i { animation: ovm-flick 1.4s linear infinite; }",
    "svg[data-s='transform'] .leaf.o, svg[data-s='transform'] .leaf.d { opacity: .28; }",
    "svg[data-s='transform'] .pk-i { fill: var(--ovid-transform, " + COLORS.transform + "); animation: ovm-pk-i 1.4s linear infinite; }",
    "@keyframes ovm-flick { 0% { opacity: 1; } 5.5% { opacity: .25; } 11% { opacity: 1; } 16.5% { opacity: .4; } 22%, 100% { opacity: 1; } }",

    /* load */
    "svg[data-s='load'] .d .edge { stroke: var(--ovid-load, " + COLORS.load + "); }",
    "svg[data-s='load'] .d .fill { fill: var(--ovid-load, " + COLORS.load + "); animation: ovm-fillpulse 1.6s ease-in-out infinite; }",
    "svg[data-s='load'] .leaf.o, svg[data-s='load'] .leaf.i { opacity: .28; }",
    "svg[data-s='load'] .pk-d { fill: var(--ovid-load, " + COLORS.load + "); animation: ovm-pk-d 1.6s ease-in-out infinite; }",
    "@keyframes ovm-fillpulse { 0%, 100% { fill-opacity: 0; } 50% { fill-opacity: .32; } }",

    /* running — O, I, D in turn, packets hand off */
    "svg[data-s='running'] .o .edge { stroke: var(--ovid-extract, " + COLORS.extract + "); }",
    "svg[data-s='running'] .i .edge { stroke: var(--ovid-transform, " + COLORS.transform + "); }",
    "svg[data-s='running'] .d .edge { stroke: var(--ovid-load, " + COLORS.load + "); }",
    "svg[data-s='running'] .leaf { animation: ovm-turn 3s ease-in-out infinite both; }",
    "svg[data-s='running'] .leaf.i { animation-delay: 1s; }",
    "svg[data-s='running'] .leaf.d { animation-delay: 2s; }",
    "svg[data-s='running'] .pk-o { fill: var(--ovid-extract, " + COLORS.extract + "); animation: ovm-pk-o-run 3s linear infinite both; }",
    "svg[data-s='running'] .pk-i { fill: var(--ovid-transform, " + COLORS.transform + "); animation: ovm-pk-i-run 3s linear 1s infinite both; }",
    "svg[data-s='running'] .pk-d { fill: var(--ovid-load, " + COLORS.load + "); animation: ovm-pk-d-run 3s linear 2s infinite both; }",
    "@keyframes ovm-turn { 0% { opacity: .3; } 16.6% { opacity: 1; } 33.3%, 100% { opacity: .3; } }",

    /* sync */
    "svg[data-s='sync'] .orbit { opacity: 1; animation: ovm-orbit 1.2s linear infinite; }",
    "svg[data-s='sync'] .node { animation: ovm-node 1.2s ease-in-out infinite both; }",
    "svg[data-s='sync'] .node.n-i { animation-delay: .4s; }",
    "svg[data-s='sync'] .node.n-d { animation-delay: .8s; }",
    "@keyframes ovm-orbit { from { stroke-dashoffset: 0; } to { stroke-dashoffset: -18; } }",
    "@keyframes ovm-node { 0%, 100% { transform: scale(.7); opacity: .3; } 50% { transform: scale(1.15); opacity: 1; } }",

    /* success */
    "svg[data-s='success'] .edge, svg[data-s='success'] .stem { stroke: var(--ovid-success, " + COLORS.success + "); }",
    "svg[data-s='success'] .fill { fill: var(--ovid-success, " + COLORS.success + "); fill-opacity: .12;",
    "  animation: ovm-flash .9s ease-out 1 both; }",
    "svg[data-s='success'] .rot.o, .flick .rot.o { animation: ovm-rustle-o .9s ease-out 1; }",
    "svg[data-s='success'] .rot.d, .flick .rot.d { animation: ovm-rustle-d .9s ease-out 1; }",
    "svg[data-s='success'] .rot.i, .flick .rot.i { animation: ovm-rustle-i .9s ease-out 1; }",
    "@keyframes ovm-flash { 0% { fill-opacity: 0; } 30% { fill-opacity: .4; } 100% { fill-opacity: .12; } }",
    "@keyframes ovm-rustle-o { 0% { transform: rotate(0); } 30% { transform: rotate(-8deg); } 65% { transform: rotate(3deg); } 100% { transform: rotate(0); } }",
    "@keyframes ovm-rustle-d { 0% { transform: rotate(0); } 30% { transform: rotate(8deg); } 65% { transform: rotate(-3deg); } 100% { transform: rotate(0); } }",
    "@keyframes ovm-rustle-i { 0% { transform: rotate(0); } 30% { transform: rotate(3deg); } 65% { transform: rotate(-3deg); } 100% { transform: rotate(0); } }",

    /* warning */
    "svg[data-s='warning'] .stem { stroke: var(--ovid-warning, " + COLORS.warning + "); animation: ovm-pulse 1.2s ease-in-out infinite; }",
    "@keyframes ovm-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .25; } }",

    /* error — shake, then droop (held) */
    "svg[data-s='error'] .edge, svg[data-s='error'] .stem { stroke: var(--ovid-error, " + COLORS.error + "); }",
    "svg[data-s='error'] .mark { animation: ovm-shake .45s ease-in-out 1; }",
    "svg[data-s='error'] .rot.o { animation: ovm-droop-o .6s ease-in-out .35s 1 both; }",
    "svg[data-s='error'] .rot.d { animation: ovm-droop-d .6s ease-in-out .35s 1 both; }",
    "svg[data-s='error'] .rot.i { animation: ovm-droop-i .6s ease-in-out .35s 1 both; }",
    "@keyframes ovm-shake { 0%, 100% { transform: translateX(0); } 20% { transform: translateX(-6px); }",
    "  40% { transform: translateX(6px); } 60% { transform: translateX(-6px); } 80% { transform: translateX(6px); } }",
    "@keyframes ovm-droop-o { from { transform: rotate(0); } to { transform: rotate(-12deg); } }",
    "@keyframes ovm-droop-d { from { transform: rotate(0); } to { transform: rotate(12deg); } }",
    "@keyframes ovm-droop-i { from { transform: rotate(0); } to { transform: rotate(4deg); } }",

    packetKeyframes("ovm-pk-o", ROUTES.o, 0, 100),
    packetKeyframes("ovm-pk-i", ROUTES.i, 0, 100),
    packetKeyframes("ovm-pk-d", ROUTES.d, 0, 100),
    packetKeyframes("ovm-pk-o-run", ROUTES.o, 0, 33.3),
    packetKeyframes("ovm-pk-i-run", ROUTES.i, 0, 33.3),
    packetKeyframes("ovm-pk-d-run", ROUTES.d, 0, 33.3),

    "@media (prefers-reduced-motion: reduce) {",
    "  *, .mark, .rot, .leaf, .edge, .fill, .pk, .node, .orbit { animation: none !important; }",
    "  .pk { opacity: 0 !important; }",
    "}"
  ].join("\n");

  var sheet = null;
  try {
    if (typeof CSSStyleSheet === "function" && "replaceSync" in CSSStyleSheet.prototype) {
      sheet = new CSSStyleSheet();
      sheet.replaceSync(CSS);
    }
  } catch (e) { sheet = null; }

  var SVG_NS = "http://www.w3.org/2000/svg";
  var uid = 0;

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG_NS, name);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }

  function normalise(state) {
    var s = String(state || "").trim().toLowerCase();
    return STATES.indexOf(s) === -1 ? "idle" : s;
  }

  function tierFor(size) {
    if (size >= 128) return "l";
    if (size >= 64) return "m";
    return "s";
  }

  /* Shared visually-hidden live region (light DOM — children of a
   * role="img" element are presentational, so the region cannot live
   * inside the mark itself). */
  var liveRegion = null;
  function announce(text) {
    if (!document.body) return;
    if (!liveRegion || !liveRegion.isConnected) {
      liveRegion = document.createElement("div");
      liveRegion.className = "ovid-mark-live";
      liveRegion.setAttribute("aria-live", "polite");
      liveRegion.setAttribute("role", "status");
      var st = liveRegion.style;
      st.position = "absolute"; st.width = "1px"; st.height = "1px";
      st.margin = "-1px"; st.overflow = "hidden"; st.clip = "rect(0 0 0 0)";
      st.whiteSpace = "nowrap"; st.border = "0"; st.padding = "0";
      document.body.appendChild(liveRegion);
    }
    liveRegion.textContent = text;
  }

  class OvidMark extends HTMLElement {
    static get observedAttributes() { return ["state", "size"]; }

    constructor() {
      super();
      this._state = "idle";
      this._successTimer = 0;
      this._flickTimer = 0;
      var root = this.attachShadow({ mode: "open" });
      if (sheet) {
        root.adoptedStyleSheets = [sheet];
      } else {
        var style = document.createElement("style");
        style.textContent = CSS;
        root.appendChild(style);
      }
      uid += 1;
      var gid = "ovm-g-" + uid;
      var svg = el("svg", { viewBox: VIEWBOX, "aria-hidden": "true", focusable: "false", "data-s": "idle" });
      var defs = el("defs", {}, svg);
      var grad = el("linearGradient", {
        id: gid, gradientUnits: "userSpaceOnUse", x1: "20", y1: "10", x2: "170", y2: "170"
      }, defs);
      el("stop", { offset: "0", style: "stop-color: var(--ovid-rest-1, " + COLORS.rest1 + ")" }, grad);
      el("stop", { offset: ".45", style: "stop-color: var(--ovid-rest-2, " + COLORS.rest2 + ")" }, grad);
      el("stop", { offset: "1", style: "stop-color: var(--ovid-rest-3, " + COLORS.rest3 + ")" }, grad);
      var stroke = "url(#" + gid + ")";

      var mark = el("g", { "class": "mark" }, svg);
      el("path", { "class": "orbit", d: PATHS.orbit }, mark);
      ["o", "d", "i"].forEach(function (k) {
        var rot = el("g", { "class": "rot " + k }, mark);
        var leaf = el("g", { "class": "leaf " + k }, rot);
        el("path", { "class": "fill", d: PATHS[k] }, leaf);
        el("path", { "class": "edge", d: PATHS[k], stroke: stroke }, leaf);
      });
      el("path", { "class": "stem", d: PATHS.stem, stroke: stroke }, mark);
      ["o", "i", "d"].forEach(function (k) {
        el("circle", { "class": "pk pk-" + k, r: "5", cx: "0", cy: "0" }, mark);
      });
      ["o", "i", "d"].forEach(function (k) {
        el("circle", { "class": "node n-" + k, r: "7", cx: String(TIPS[k][0]), cy: String(TIPS[k][1]) }, mark);
      });
      root.appendChild(svg);
      this._svg = svg;
    }

    connectedCallback() {
      if (!this.hasAttribute("role")) this.setAttribute("role", "img");
      this._applySize();
      this._applyState(this.getAttribute("state"), true);
    }

    disconnectedCallback() {
      clearTimeout(this._successTimer);
      clearTimeout(this._flickTimer);
    }

    attributeChangedCallback(name, oldValue, newValue) {
      if (name === "size") { this._applySize(); return; }
      if (name === "state") this._applyState(newValue, false);
    }

    get state() { return this._state; }
    set state(value) { this.setAttribute("state", normalise(value)); }

    /** Replay the one-shot rustle (0.9 s). */
    flick() {
      var svg = this._svg;
      svg.classList.remove("flick");
      void svg.getBoundingClientRect(); // restart the animation
      svg.classList.add("flick");
      clearTimeout(this._flickTimer);
      this._flickTimer = setTimeout(function () { svg.classList.remove("flick"); }, 900);
    }

    _applySize() {
      var size = parseFloat(this.getAttribute("size"));
      if (!(size > 0)) size = 48;
      this._svg.setAttribute("width", String(size));
      this._svg.setAttribute("height", String(Math.round(size * 0.8 * 10) / 10));
      this._svg.setAttribute("data-tier", tierFor(size));
    }

    _applyState(raw, initial) {
      var to = normalise(raw);
      var from = this._state;
      this._svg.setAttribute("data-s", to);
      this.setAttribute("aria-label", "Ovid2 \u2014 " + LABELS[to]);
      clearTimeout(this._successTimer);
      if (to === "success") {
        var self = this;
        this._successTimer = setTimeout(function () {
          if (self._state === "success") self.setAttribute("state", "idle");
        }, 4000);
      }
      if (to === from && !initial) return;
      this._state = to;
      if (initial) return;
      if (!this.hasAttribute("silent")) announce("Ovid2: " + LABELS[to]);
      this.dispatchEvent(new CustomEvent("ovid-state-change", {
        bubbles: true, composed: true, detail: { from: from, to: to }
      }));
    }

    /**
     * Return a standalone SVG string of the mark for a favicon.
     *
     * Solid colours only (dashes / flicker do not read at 16 px). With
     * ``colors.rest`` the line uses the rest gradient; ``focus`` (o|i|d)
     * dims the other two leaves so a timer can step O -> I -> D.
     */
    static faviconSvg(opts) {
      var o = opts || {};
      var c = o.color || COLORS.base;
      var stroke = c;
      var defs = "";
      if (o.rest) {
        defs = "<defs><linearGradient id='g' gradientUnits='userSpaceOnUse' x1='20' y1='10' x2='170' y2='170'>" +
          "<stop offset='0' stop-color='" + (o.rest[0] || COLORS.rest1) + "'/>" +
          "<stop offset='.45' stop-color='" + (o.rest[1] || COLORS.rest2) + "'/>" +
          "<stop offset='1' stop-color='" + (o.rest[2] || COLORS.rest3) + "'/>" +
          "</linearGradient></defs>";
        stroke = "url(#g)";
      }
      function op(k) { return o.focus && o.focus !== k ? " opacity='.3'" : ""; }
      var leafColor = o.leafColors || {};
      function leaf(k) {
        var s = leafColor[k] || stroke;
        return "<path d='" + PATHS[k] + "' stroke='" + s + "'" + op(k) + "/>";
      }
      return "<svg xmlns='http://www.w3.org/2000/svg' viewBox='" + VIEWBOX + "'>" + defs +
        "<g fill='none' stroke-width='14' stroke-linecap='round' stroke-linejoin='round'>" +
        leaf("o") + leaf("d") + leaf("i") +
        "<path d='" + PATHS.stem + "' stroke='" + (o.stemColor || stroke) + "'/>" +
        "</g></svg>";
    }
  }

  OvidMark.STATES = STATES.slice();
  OvidMark.LABELS = LABELS;
  OvidMark.COLORS = COLORS;
  OvidMark.PATHS = PATHS;

  window.customElements.define("ovid-mark", OvidMark);
})();
