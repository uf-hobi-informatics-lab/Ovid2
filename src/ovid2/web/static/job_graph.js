/*
 * Ovid2 job graph — the right-hand "pipeline under construction" view on
 * /submit.
 *
 * Draws Source → Pipeline → Target as three SVG nodes joined by edges.
 * Each node is grey + dashed until its slot is assigned, then turns green
 * (``is-done``). An edge turns green once both of its ends are done.
 * Optional chips (Tables / Stages / Checks / Engine) render under the
 * graph as HTML and highlight when the operator customised them.
 *
 * Clicking (or Enter on) a node dispatches ``ovid2:graph-focus`` with
 * ``{slot}`` so the page controller can open the matching navigator
 * section.
 *
 * Dependency-free ES module; reads nothing from the DOM except its own
 * root, receives state via ``render(state)``.
 */

import { logoSvg, LOGOS } from "/static/logos.js";

const SVGNS = "http://www.w3.org/2000/svg";

const W = 480;
const H = 190;
const NODE_W = 128;
const NODE_H = 118;
const NODE_Y = 34;
const XS = [12, (W - NODE_W) / 2, W - NODE_W - 12];

function svgEl(tag, attrs, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  if (parent) parent.appendChild(n);
  return n;
}

function truncate(text, max) {
  const s = String(text || "");
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

/* Append a 24-unit logo glyph into ``g`` at (x, y) scaled to ``size``. */
function drawLogo(g, name, x, y, size) {
  const holder = svgEl("svg", {
    x, y, width: size, height: size, viewBox: "0 0 24 24",
    fill: "none", stroke: "currentColor", "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round",
    class: "graph-logo logo logo-" + name,
  }, g);
  // LOGOS values are static, trusted markup authored in logos.js.
  const key = Object.prototype.hasOwnProperty.call(LOGOS, name) ? name : "unknown";
  holder.innerHTML = LOGOS[key];
}

export class JobGraph {
  constructor(root) {
    this.root = root;
    this.svg = root.querySelector("svg.job-graph-svg");
    this.chips = root.querySelector("[data-role=graph-chips]");
    this.status = root.querySelector("[data-role=graph-status]");
    if (this.svg) {
      this.svg.setAttribute("viewBox", "0 0 " + W + " " + H);
      this.svg.addEventListener("click", (evt) => {
        const node = evt.target.closest("[data-slot]");
        if (node) this.focus(node.getAttribute("data-slot"));
      });
      this.svg.addEventListener("keydown", (evt) => {
        if (evt.key !== "Enter" && evt.key !== " ") return;
        const node = evt.target.closest("[data-slot]");
        if (node) { evt.preventDefault(); this.focus(node.getAttribute("data-slot")); }
      });
    }
    if (this.chips) {
      this.chips.addEventListener("click", (evt) => {
        const chip = evt.target.closest("[data-slot]");
        if (chip) this.focus(chip.getAttribute("data-slot"));
      });
    }
  }

  focus(slot) {
    document.dispatchEvent(new CustomEvent("ovid2:graph-focus", { detail: { slot } }));
  }

  /**
   * @param {{nodes: Array<{slot:string,title:string,value:string,logo:string,done:boolean,optional?:boolean}>,
   *          chips: Array<{slot:string,label:string,value:string,custom:boolean,logo:string}>,
   *          complete: boolean, missing: string[]}} state
   */
  render(state) {
    if (!this.svg) return;
    const svg = this.svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);

    // Arrow-head markers (grey + green).
    const defs = svgEl("defs", {}, svg);
    for (const [id, cls] of [["jg-arrow", "graph-arrow"], ["jg-arrow-ok", "graph-arrow is-done"]]) {
      const m = svgEl("marker", {
        id, viewBox: "0 0 10 10", refX: 9, refY: 5,
        markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
      }, defs);
      svgEl("path", { d: "M0 0L10 5L0 10z", class: cls }, m);
    }

    const nodes = state.nodes || [];
    // Edges first so nodes paint over them.
    for (let i = 0; i < nodes.length - 1; i++) {
      const ok = nodes[i].done && nodes[i + 1].done;
      const x1 = XS[i] + NODE_W;
      const x2 = XS[i + 1];
      const y = NODE_Y + NODE_H / 2;
      svgEl("line", {
        x1: x1 + 2, y1: y, x2: x2 - 4, y2: y,
        class: "graph-edge" + (ok ? " is-done" : ""),
        "marker-end": "url(#" + (ok ? "jg-arrow-ok" : "jg-arrow") + ")",
      }, svg);
    }

    nodes.forEach((n, i) => {
      const g = svgEl("g", {
        class: "graph-node" + (n.done ? " is-done" : "") + (n.active ? " is-active" : ""),
        "data-slot": n.slot,
        transform: "translate(" + XS[i] + "," + NODE_Y + ")",
        tabindex: 0,
        role: "button",
        "aria-label": n.title + ": " + (n.done ? n.value : "not assigned"),
      }, svg);
      svgEl("rect", { width: NODE_W, height: NODE_H, rx: 14, class: "graph-node-box" }, g);
      svgEl("circle", { cx: NODE_W / 2, cy: 34, r: 22, class: "graph-node-disc" }, g);
      drawLogo(g, n.logo || "unknown", NODE_W / 2 - 13, 21, 26);
      const title = svgEl("text", { x: NODE_W / 2, y: 74, class: "graph-node-title" }, g);
      title.textContent = n.title;
      const val = svgEl("text", { x: NODE_W / 2, y: 92, class: "graph-node-value" }, g);
      val.textContent = n.done ? truncate(n.value, 18) : "click to assign";
      if (n.sub) {
        const sub = svgEl("text", { x: NODE_W / 2, y: 107, class: "graph-node-sub" }, g);
        sub.textContent = truncate(n.sub, 20);
      }
      const t = svgEl("title", {}, g);
      t.textContent = n.title + ": " + (n.done ? n.value : "not assigned yet");
      // Status badge (top-right): check when done.
      const badge = svgEl("g", { class: "graph-badge", transform: "translate(" + (NODE_W - 14) + ",14)" }, g);
      svgEl("circle", { r: 9, class: "graph-badge-disc" }, badge);
      if (n.done) {
        svgEl("polyline", { points: "-4,0 -1,3 4,-3", class: "graph-badge-check" }, badge);
      } else {
        const num = svgEl("text", { class: "graph-badge-num", y: 3.5 }, badge);
        num.textContent = String(i + 1);
      }
    });

    // Caption row (above nodes).
    const cap = svgEl("text", { x: W / 2, y: 18, class: "graph-caption" }, svg);
    cap.textContent = state.complete ? "Job ready to submit" : "Assign every node to build the job";

    if (this.chips) {
      this.chips.innerHTML = (state.chips || []).filter((c) => !c.hidden).map((c) =>
        '<button type="button" class="graph-chip' + (c.custom ? " is-custom" : "") +
        (c.done === false ? " is-missing" : "") + '" data-slot="' + c.slot + '">' +
        logoSvg(c.logo || "unknown", 14) +
        '<span class="graph-chip-label">' + c.label + "</span>" +
        '<span class="graph-chip-value">' + String(c.value || "")
          .replace(/[<>&"']/g, (ch) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[ch]) +
        "</span></button>"
      ).join("");
    }
    if (this.status) {
      this.status.textContent = state.complete
        ? "All set."
        : "Still needed: " + (state.missing || []).join(", ");
      this.status.classList.toggle("is-done", !!state.complete);
    }
    this.root.classList.toggle("is-complete", !!state.complete);
  }
}
