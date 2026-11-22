/*
 * Workflows index — client-side filters and the mini DAG thumbnail on
 * every card (rendered from the card's ``data-wf-mini`` JSON with the
 * shared layout in workflow_graph.js). Without JS the cards still list
 * their steps as chips.
 */
import { NODE_H, NODE_W, autoLayout, curve, ranks, svgEl } from "/static/workflow_graph.js";

function drawMini(host) {
  let data;
  try { data = JSON.parse(host.getAttribute("data-wf-mini") || "{}"); } catch (_e) { return; }
  const steps = data.steps || [];
  const edges = data.edges || [];
  if (!steps.length) return;
  const kinds = data.kinds || {};
  const pos = autoLayout(steps, edges);
  const xs = Object.values(pos);
  const minX = Math.min(...xs.map((p) => p.x));
  const maxX = Math.max(...xs.map((p) => p.x)) + NODE_W;
  const maxY = Math.max(...xs.map((p) => p.y)) + NODE_H;
  const svg = svgEl("svg", {
    viewBox: `${minX - 12} -12 ${maxX - minX + 24} ${maxY + 24}`,
    class: "wf-mini-svg", preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true",
  });
  for (const e of edges) {
    const a = pos[e.from];
    const b = pos[e.to];
    if (!a || !b) continue;
    svgEl("path", {
      d: curve(a.x + NODE_W, a.y + NODE_H / 2, b.x, b.y + NODE_H / 2),
      class: `wf-mini-edge is-${e.type}`,
    }, svg);
  }
  const r = ranks(steps, edges);
  for (const s of steps) {
    const p = pos[s.id];
    const g = svgEl("g", { transform: `translate(${p.x},${p.y})`, class: "wf-mini-node", "data-kind": kinds[s.pipeline] || "" }, svg);
    svgEl("rect", { width: NODE_W, height: NODE_H, rx: 16 }, g);
    svgEl("rect", { width: 10, height: NODE_H - 24, x: 0, y: 12, rx: 5, class: "wf-mini-accent" }, g);
    const t = svgEl("text", { x: 26, y: NODE_H / 2 + 11 }, g);
    const label = s.label || s.pipeline;
    t.textContent = label.length > 15 ? label.slice(0, 14) + "\u2026" : label;
    svgEl("title", {}, g).textContent = `${label} (rank ${r.get(s.id) || 0})`;
  }
  host.querySelector(".wf-mini-fallback").hidden = true;
  host.appendChild(svg);
}

document.querySelectorAll("[data-wf-mini]").forEach(drawMini);

/* Filters. */
const bar = document.querySelector("[data-wf-filters]");
if (bar) {
  const cards = Array.from(document.querySelectorAll("[data-wf-card]"));
  const count = bar.querySelector("[data-wf-count]");
  const none = document.querySelector("[data-wf-no-match]");
  const apply = () => {
    const f = {};
    bar.querySelectorAll("[data-wf-filter]").forEach((el) => { f[el.dataset.wfFilter] = (el.value || "").toLowerCase(); });
    let shown = 0;
    for (const c of cards) {
      const ok = (!f.name || c.dataset.name.includes(f.name)) &&
        (!f.trigger || c.dataset.trigger === f.trigger) &&
        (!f.enabled || c.dataset.enabled === f.enabled);
      c.hidden = !ok;
      if (ok) shown++;
    }
    count.textContent = shown === cards.length ? "" : `${shown} of ${cards.length}`;
    if (none) none.hidden = shown !== 0;
  };
  bar.addEventListener("input", apply);
  bar.addEventListener("change", apply);
}
