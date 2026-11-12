/*
 * Ovid2 flow graph — a theme-aware, N-node left-to-right SVG pipeline
 * diagram (the generalisation of job_graph.js's fixed three columns).
 *
 *   const g = new FlowGraph(rootEl);          // rootEl contains <svg class="flow-graph-svg">
 *   g.render({
 *     caption: "demographic",
 *     nodes: [{id, title, value, sub, logo, state, badge}],   // state: done|todo|warn|error|off|active
 *   });
 *
 * Nodes are laid out evenly; an edge is solid when both ends are done.
 * Clicking (or Enter on) a node dispatches ``ovid2:flow-focus`` with
 * ``{id, graph}`` on ``document`` so a page can open the matching editor.
 * Colours come from CSS custom properties (.flow-* rules in ovid2.css), so
 * every theme applies — no hard-coded palette.
 */

import { LOGOS } from "/static/logos.js";

const SVGNS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs, parent) {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
  if (parent) parent.appendChild(n);
  return n;
}

function truncate(text, max) {
  const s = String(text == null ? "" : text);
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

function drawLogo(g, name, x, y, size) {
  const holder = svgEl("svg", {
    x, y, width: size, height: size, viewBox: "0 0 24 24", fill: "none",
    stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round",
    "stroke-linejoin": "round", class: "flow-logo logo logo-" + name,
  }, g);
  const key = Object.prototype.hasOwnProperty.call(LOGOS, name) ? name : "unknown";
  holder.innerHTML = LOGOS[key];  // static, trusted markup from logos.js
}

export class FlowGraph {
  constructor(root, opts = {}) {
    this.root = root;
    this.name = opts.name || root.id || "flow";
    this.svg = root.querySelector("svg.flow-graph-svg") || svgEl("svg", { class: "flow-graph-svg" }, root);
    this.nodeW = opts.nodeW || 112;
    this.nodeH = opts.nodeH || 96;
    this.gap = opts.gap || 30;
    this.svg.addEventListener("click", (e) => {
      const n = e.target.closest("[data-flow-id]");
      if (n) this.focus(n.getAttribute("data-flow-id"));
    });
    this.svg.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const n = e.target.closest("[data-flow-id]");
      if (n) { e.preventDefault(); this.focus(n.getAttribute("data-flow-id")); }
    });
  }

  focus(id) {
    document.dispatchEvent(new CustomEvent("ovid2:flow-focus", { detail: { id, graph: this.name } }));
  }

  render(state) {
    const svg = this.svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const nodes = state.nodes || [];
    const W = Math.max(nodes.length * this.nodeW + (nodes.length - 1) * this.gap + 16, 240);
    const top = state.caption ? 26 : 8;
    const H = top + this.nodeH + 10;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", (state.caption ? state.caption + ": " : "") +
      nodes.map((n) => n.title + " " + (n.value || "")).join(" → "));

    const defs = svgEl("defs", {}, svg);
    for (const [id, cls] of [["fg-arrow", "flow-arrow"], ["fg-arrow-ok", "flow-arrow is-done"]]) {
      const m = svgEl("marker", { id: id + "-" + this.name, viewBox: "0 0 10 10", refX: 9, refY: 5,
        markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
      svgEl("path", { d: "M0 0L10 5L0 10z", class: cls }, m);
    }
    if (state.caption) {
      const cap = svgEl("text", { x: W / 2, y: 16, class: "flow-caption" }, svg);
      cap.textContent = state.caption;
    }
    const x = (i) => 8 + i * (this.nodeW + this.gap);
    for (let i = 0; i < nodes.length - 1; i++) {
      const ok = nodes[i].state === "done" && nodes[i + 1].state === "done";
      const off = nodes[i + 1].state === "off" || nodes[i].state === "off";
      const y = top + this.nodeH / 2;
      svgEl("line", {
        x1: x(i) + this.nodeW + 2, y1: y, x2: x(i + 1) - 4, y2: y,
        class: "flow-edge" + (ok ? " is-done" : "") + (off ? " is-off" : ""),
        "marker-end": "url(#" + (ok ? "fg-arrow-ok" : "fg-arrow") + "-" + this.name + ")",
      }, svg);
    }
    nodes.forEach((n, i) => {
      const g = svgEl("g", {
        class: "flow-node is-" + (n.state || "todo") + (n.active ? " is-active" : ""),
        "data-flow-id": n.id, transform: "translate(" + x(i) + "," + top + ")",
        tabindex: n.id ? 0 : -1, role: "button",
        "aria-label": n.title + ": " + (n.value || ""),
      }, svg);
      svgEl("rect", { width: this.nodeW, height: this.nodeH, rx: 12, class: "flow-node-box" }, g);
      svgEl("circle", { cx: this.nodeW / 2, cy: 26, r: 17, class: "flow-node-disc" }, g);
      drawLogo(g, n.logo || "unknown", this.nodeW / 2 - 10, 16, 20);
      const t = svgEl("text", { x: this.nodeW / 2, y: 59, class: "flow-node-title" }, g);
      t.textContent = truncate(n.title, 16);
      const v = svgEl("text", { x: this.nodeW / 2, y: 74, class: "flow-node-value" }, g);
      v.textContent = truncate(n.value, 18);
      if (n.sub) {
        const s = svgEl("text", { x: this.nodeW / 2, y: 87, class: "flow-node-sub" }, g);
        s.textContent = truncate(n.sub, 20);
      }
      if (n.badge != null && n.badge !== "") {
        const b = svgEl("g", { class: "flow-badge", transform: "translate(" + (this.nodeW - 13) + ",13)" }, g);
        svgEl("circle", { r: 9, class: "flow-badge-disc" }, b);
        const bt = svgEl("text", { y: 3.5, class: "flow-badge-num" }, b);
        bt.textContent = truncate(n.badge, 3);
      }
      const tip = svgEl("title", {}, g);
      tip.textContent = n.title + (n.value ? ": " + n.value : "") + (n.tip ? "\n" + n.tip : "");
    });
  }
}
