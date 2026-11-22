/*
 * Workflow graph — layout + SVG rendering shared by the canvas editor
 * (workflow_canvas.js), the run view (workflow_run.js) and the index
 * thumbnails (workflows.js).
 *
 * Model:   { steps: [{id, pipeline, label, overrides, rollback, position}],
 *            edges: [{from, to, type: "data"|"after"}], trigger: {type, ...} }
 * Layout:  layered left → right (longest-path ranks, then a few barycentre
 *          sweeps to reduce crossings). Positions saved on a step win.
 * Render:  plain SVG (no library) styled only by CSS classes (.wf-*), so
 *          every theme applies. Node: kind accent bar, pipeline name, the
 *          resolved input → output, a kind badge and status / rollback marks.
 *
 * Dependency-free ES module.
 */

export const NODE_W = 232;
export const NODE_H = 86;
export const COL_GAP = 96;
export const ROW_GAP = 30;
export const TRIGGER_W = 132;
export const TRIGGER_H = 40;

const NS = "http://www.w3.org/2000/svg";

export function svgEl(tag, attrs = {}, parent = null) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v !== undefined && v !== null) el.setAttribute(k, String(v));
  }
  if (parent) parent.appendChild(el);
  return el;
}

export const esc = (s) => String(s == null ? "" : s).replace(/[<>&"']/g,
  (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[c]);

/* ------------------------------------------------------------------ layout */

export function ranks(steps, edges) {
  const ids = steps.map((s) => s.id);
  const parents = new Map(ids.map((i) => [i, []]));
  for (const e of edges) if (parents.has(e.to) && parents.has(e.from)) parents.get(e.to).push(e.from);
  const rank = new Map();
  const visiting = new Set();
  const visit = (id) => {
    if (rank.has(id)) return rank.get(id);
    if (visiting.has(id)) return 0; // cycle guard (validation reports it)
    visiting.add(id);
    let r = 0;
    for (const p of parents.get(id) || []) r = Math.max(r, visit(p) + 1);
    visiting.delete(id);
    rank.set(id, r);
    return r;
  };
  ids.forEach(visit);
  return rank;
}

/* Return {id: {x, y}} — auto layout for every step (ignores saved positions). */
export function autoLayout(steps, edges) {
  const rank = ranks(steps, edges);
  const cols = [];
  for (const s of steps) {
    const r = rank.get(s.id) || 0;
    (cols[r] = cols[r] || []).push(s.id);
  }
  const parents = new Map(steps.map((s) => [s.id, []]));
  const children = new Map(steps.map((s) => [s.id, []]));
  for (const e of edges) {
    if (parents.has(e.to) && children.has(e.from)) {
      parents.get(e.to).push(e.from);
      children.get(e.from).push(e.to);
    }
  }
  const order = new Map();
  cols.forEach((col) => col && col.forEach((id, i) => order.set(id, i)));
  const bary = (id, nb) => {
    const ns = nb.get(id) || [];
    if (!ns.length) return order.get(id);
    return ns.reduce((a, n) => a + order.get(n), 0) / ns.length;
  };
  for (let sweep = 0; sweep < 4; sweep++) {
    const forward = sweep % 2 === 0;
    const seq = forward ? cols.slice(1) : cols.slice(0, -1).reverse();
    for (const col of seq) {
      if (!col) continue;
      col.sort((a, b) => bary(a, forward ? parents : children) - bary(b, forward ? parents : children));
      col.forEach((id, i) => order.set(id, i));
    }
  }
  const tallest = Math.max(1, ...cols.map((c) => (c ? c.length : 0)));
  const pos = {};
  cols.forEach((col, r) => {
    if (!col) return;
    const offset = ((tallest - col.length) * (NODE_H + ROW_GAP)) / 2;
    col.forEach((id, i) => {
      pos[id] = {
        x: TRIGGER_W + COL_GAP + r * (NODE_W + COL_GAP),
        y: offset + i * (NODE_H + ROW_GAP),
      };
    });
  });
  return pos;
}

/* Positions honouring saved coordinates; unsaved steps get the auto layout. */
export function layout(steps, edges) {
  const auto = autoLayout(steps, edges);
  const pos = {};
  for (const s of steps) {
    const p = s.position;
    pos[s.id] = p && Number.isFinite(p.x) && Number.isFinite(p.y) ? { x: p.x, y: p.y } : auto[s.id];
  }
  return pos;
}

export function rootIds(steps, edges) {
  const targets = new Set(edges.map((e) => e.to));
  return steps.filter((s) => !targets.has(s.id)).map((s) => s.id);
}

export function triggerPos(steps, edges, pos) {
  const roots = rootIds(steps, edges);
  const ys = roots.map((id) => pos[id]).filter(Boolean).map((p) => p.y + NODE_H / 2);
  const xs = roots.map((id) => pos[id]).filter(Boolean).map((p) => p.x);
  const cy = ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : NODE_H / 2;
  const x = xs.length ? Math.min(...xs) - COL_GAP - TRIGGER_W : 0;
  return { x, y: cy - TRIGGER_H / 2 };
}

/* Bezier from (x1,y1) to (x2,y2), horizontal tangents. */
export function curve(x1, y1, x2, y2) {
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.5);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

/* ------------------------------------------------------------------ render */

export const TRIGGER_GLYPH = {
  manual: '<path d="M9 11V5a1.5 1.5 0 0 1 3 0v6M12 10V4a1.5 1.5 0 0 1 3 0v6M15 10.5V6a1.5 1.5 0 0 1 3 0v8a6 6 0 0 1-6 6h-1.5a6 6 0 0 1-4.6-2.2L3.5 14a1.6 1.6 0 0 1 2.4-2.1L9 14"/>',
  cron: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  interval: '<path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v4h-4"/>',
  watch: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
};

function glyph(parent, inner, x, y, size, cls) {
  const g = svgEl("svg", {
    x, y, width: size, height: size, viewBox: "0 0 24 24", class: cls,
    fill: "none", stroke: "currentColor", "stroke-width": 2,
    "stroke-linecap": "round", "stroke-linejoin": "round",
  }, parent);
  g.innerHTML = inner;
  return g;
}

function clip(text, max) {
  const s = String(text || "");
  return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
}

export function ensureDefs(svg) {
  let defs = svg.querySelector("defs");
  if (defs) return defs;
  defs = svgEl("defs", {}, svg);
  for (const [id, cls] of [["wf-arrow-data", "wf-arrow is-data"], ["wf-arrow-after", "wf-arrow is-after"],
                           ["wf-arrow-sel", "wf-arrow is-selected"], ["wf-arrow-trig", "wf-arrow is-trigger"]]) {
    const m = svgEl("marker", {
      id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
    }, defs);
    svgEl("path", { d: "M0,0 L10,5 L0,10 z", class: cls }, m);
  }
  const pat = svgEl("pattern", { id: "wf-grid", width: 24, height: 24, patternUnits: "userSpaceOnUse" }, defs);
  svgEl("circle", { cx: 1, cy: 1, r: 1, class: "wf-grid-dot" }, pat);
  return defs;
}

/*
 * Draw the whole graph into ``layer`` (a <g>). ``ctx``:
 *   pipelines   {name: {kind, input_address, output_address, ...}}
 *   status      {stepId: status}    (run view) — optional
 *   selected    {kind: "step"|"edge", id}     — optional
 *   errors      {stepId: [msg]}               — optional
 *   resolveIO   (step) => {input, output, inputFrom}
 *   edgeLabel   (edge) => {text, cls, title} | null   — optional branch chip
 *   decisions   {"from→to": {taken, reason}}  (run view) — optional
 */
export function drawGraph(layer, model, pos, ctx = {}) {
  layer.innerHTML = "";
  const { steps, edges } = model;
  const edgesG = svgEl("g", { class: "wf-edges" }, layer);
  const nodesG = svgEl("g", { class: "wf-nodes" }, layer);

  // Trigger pill → roots.
  if (steps.length && model.trigger) {
    const tp = triggerPos(steps, edges, pos);
    const t = model.trigger.type || "manual";
    const tg = svgEl("g", { class: `wf-trigger is-${t}`, transform: `translate(${tp.x},${tp.y})`, "data-trigger": "1" }, nodesG);
    svgEl("rect", { width: TRIGGER_W, height: TRIGGER_H, rx: TRIGGER_H / 2, class: "wf-trigger-body" }, tg);
    glyph(tg, TRIGGER_GLYPH[t] || TRIGGER_GLYPH.manual, 12, 10, 20, "wf-trigger-icon");
    const label = svgEl("text", { x: 40, y: 18, class: "wf-trigger-label" }, tg);
    label.textContent = t;
    const sub = svgEl("text", { x: 40, y: 31, class: "wf-trigger-sub" }, tg);
    sub.textContent = t === "cron" ? clip(model.trigger.cron || "", 14)
      : t === "interval" ? `every ${model.trigger.interval_minutes || "?"} min`
      : t === "watch" ? "new batch" : "on demand";
    for (const id of rootIds(steps, edges)) {
      const p = pos[id];
      if (!p) continue;
      svgEl("path", {
        d: curve(tp.x + TRIGGER_W, tp.y + TRIGGER_H / 2, p.x - 2, p.y + NODE_H / 2),
        class: "wf-edge is-trigger", "marker-end": "url(#wf-arrow-trig)",
      }, edgesG);
    }
  }

  // Edges.
  edges.forEach((e, i) => {
    const a = pos[e.from];
    const b = pos[e.to];
    if (!a || !b) return;
    const sel = ctx.selected && ctx.selected.kind === "edge" && ctx.selected.id === i;
    const d = curve(a.x + NODE_W, a.y + NODE_H / 2, b.x - 2, b.y + NODE_H / 2);
    const g = svgEl("g", { class: `wf-edge-g${sel ? " is-selected" : ""}`, "data-edge": i }, edgesG);
    svgEl("path", { d, class: "wf-edge-hit" }, g);
    const st = ctx.status ? ctx.status[e.from] : null;
    // Run view: whether the link was taken ("is-taken" / "is-not-taken").
    const dec = ctx.decisions ? ctx.decisions[`${e.from}\u2192${e.to}`] : null;
    const decCls = dec ? (dec.taken ? " is-taken" : " is-not-taken") : "";
    const extra = ctx.edgeLabel ? ctx.edgeLabel(e) : null;
    const branchCls = extra ? ` ${extra.cls}` : "";
    svgEl("path", {
      d,
      class: `wf-edge is-${e.type}${branchCls}${st ? " from-" + st : ""}${decCls}${sel ? " is-selected" : ""}`,
      "marker-end": `url(#${sel ? "wf-arrow-sel" : e.type === "data" ? "wf-arrow-data" : "wf-arrow-after"})`,
    }, g);
    // Mid-point chip: the link type, plus its branch label (if / else / on fail).
    const mx = (a.x + NODE_W + b.x) / 2;
    const my = (a.y + b.y) / 2 + NODE_H / 2;
    const label = extra ? `${e.type} \u00b7 ${clip(extra.text, 14)}` : e.type;
    const w = Math.max(40, label.length * 6.2 + 16);
    const chip = svgEl("g", { class: `wf-edge-chip is-${e.type}${branchCls}${decCls}`, transform: `translate(${mx},${my})` }, g);
    svgEl("rect", { x: -w / 2, y: -9, width: w, height: 18, rx: 9 }, chip);
    const tx = svgEl("text", { x: 0, y: 4, "text-anchor": "middle" }, chip);
    tx.textContent = label;
    const tip = (extra && extra.title ? extra.title : "") + (dec && dec.reason ? `${extra && extra.title ? "\n" : ""}${dec.taken ? "taken" : "not taken"}: ${dec.reason}` : "");
    if (tip) svgEl("title", {}, chip).textContent = tip;
  });

  // Nodes.
  for (const s of steps) {
    const p = pos[s.id];
    if (!p) continue;
    const pipe = (ctx.pipelines || {})[s.pipeline] || null;
    const kind = pipe ? pipe.kind : "missing";
    const st = ctx.status ? ctx.status[s.id] || "waiting" : null;
    const sel = ctx.selected && ctx.selected.kind === "step" && ctx.selected.id === s.id;
    const err = ctx.errors && ctx.errors[s.id] && ctx.errors[s.id].length;
    const g = svgEl("g", {
      class: `wf-node${sel ? " is-selected" : ""}${err ? " has-error" : ""}${st ? " status-" + st : ""}${pipe ? "" : " is-missing"}`,
      transform: `translate(${p.x},${p.y})`, "data-step": s.id, "data-kind": kind,
      tabindex: 0, role: "button", "aria-label": `Step ${s.label || s.pipeline}`,
    }, nodesG);
    svgEl("rect", { width: NODE_W, height: NODE_H, rx: 12, class: "wf-node-body" }, g);
    svgEl("rect", { width: 5, height: NODE_H - 16, x: 0, y: 8, rx: 2.5, class: "wf-node-accent" }, g);
    // Header line: kind badge + title.
    const kb = svgEl("g", { class: "wf-node-kind", transform: "translate(16,12)" }, g);
    const kindText = String(kind).replace("_", " ");
    const kw = Math.max(34, kindText.length * 6.4 + 14);
    svgEl("rect", { width: kw, height: 17, rx: 8.5 }, kb);
    const kt = svgEl("text", { x: kw / 2, y: 12.2, "text-anchor": "middle" }, kb);
    kt.textContent = kindText;
    const title = svgEl("text", { x: 16, y: 47, class: "wf-node-title" }, g);
    title.textContent = clip(s.label || s.pipeline, 26);
    const io = ctx.resolveIO ? ctx.resolveIO(s) : null;
    const sub = svgEl("text", { x: 16, y: 68, class: "wf-node-sub" }, g);
    if (io) {
      const inTxt = io.inputFrom ? `\u21e2 ${io.inputFrom}` : io.input || "open";
      sub.textContent = `${clip(inTxt, 13)}  \u2192  ${clip(io.output || "open", 13)}`;
    } else {
      sub.textContent = s.pipeline;
    }
    // Status / rollback / error marks (top-right).
    let mx = NODE_W - 22;
    if (st) {
      const dot = svgEl("g", { class: `wf-node-status status-${st}`, transform: `translate(${mx},20)` }, g);
      svgEl("circle", { r: 7 }, dot);
      if (st === "running" || st === "queued" || st === "snapshotting" || st === "probing" || st === "rolling_back" || st === "cleaning") {
        svgEl("circle", { r: 11, class: "wf-pulse" }, dot);
      }
      const tt = svgEl("title", {}, dot);
      tt.textContent = st.replace(/_/g, " ");
      mx -= 22;
    }
    if (s.rollback !== false) {
      const sh = glyph(g, '<path d="M12 21s7-3.5 7-9V6l-7-3-7 3v6c0 5.5 7 9 7 9z"/>', mx - 8, 12, 16, "wf-node-shield");
      const tt = svgEl("title", {}, sh);
      tt.textContent = "rolled back if the workflow fails";
      mx -= 22;
    }
    if (err) {
      const w = glyph(g, '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17v.5"/>', mx - 8, 12, 16, "wf-node-warn");
      const tt = svgEl("title", {}, w);
      tt.textContent = ctx.errors[s.id].join("\n");
    }
    // Ports.
    svgEl("circle", { cx: 0, cy: NODE_H / 2, r: 6, class: "wf-port is-in", "data-port": "in" }, g);
    svgEl("circle", { cx: NODE_W, cy: NODE_H / 2, r: 6, class: "wf-port is-out", "data-port": "out" }, g);
  }
  return { edgesG, nodesG };
}

/* Bounding box of the drawn graph (for fit-to-screen). */
export function bounds(steps, edges, pos) {
  if (!steps.length) return { x: 0, y: 0, w: 600, h: 300 };
  const tp = triggerPos(steps, edges, pos);
  let x0 = tp.x, y0 = Math.min(tp.y, Infinity), x1 = -Infinity, y1 = -Infinity;
  for (const s of steps) {
    const p = pos[s.id];
    if (!p) continue;
    x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y);
    x1 = Math.max(x1, p.x + NODE_W); y1 = Math.max(y1, p.y + NODE_H);
  }
  y0 = Math.min(y0, tp.y);
  y1 = Math.max(y1, tp.y + TRIGGER_H);
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/* Minimal pan / zoom on an <svg> + inner <g> (no d3 dependency). */
export class PanZoom {
  constructor(svg, layer, onChange) {
    this.svg = svg;
    this.layer = layer;
    this.k = 1; this.tx = 40; this.ty = 40;
    this.onChange = onChange || (() => {});
    this._drag = null;
    svg.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      this.zoomAt(ev.clientX - r.left, ev.clientY - r.top, ev.deltaY < 0 ? 1.12 : 1 / 1.12);
    }, { passive: false });
    svg.addEventListener("pointerdown", (ev) => {
      if (ev.target !== svg && !ev.target.classList.contains("wf-bg")) return;
      this._drag = { x: ev.clientX, y: ev.clientY, tx: this.tx, ty: this.ty, moved: false };
      svg.setPointerCapture(ev.pointerId);
      svg.classList.add("is-panning");
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!this._drag) return;
      const dx = ev.clientX - this._drag.x, dy = ev.clientY - this._drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) this._drag.moved = true;
      this.tx = this._drag.tx + dx; this.ty = this._drag.ty + dy;
      this.apply();
    });
    const end = () => {
      if (this._drag) this._panEndedMoved = this._drag.moved;
      this._drag = null;
      svg.classList.remove("is-panning");
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    this.apply();
  }
  apply() {
    this.layer.setAttribute("transform", `translate(${this.tx},${this.ty}) scale(${this.k})`);
    this.onChange(this.k);
  }
  zoomAt(cx, cy, f) {
    const k = Math.min(2.5, Math.max(0.25, this.k * f));
    f = k / this.k;
    this.tx = cx - (cx - this.tx) * f;
    this.ty = cy - (cy - this.ty) * f;
    this.k = k;
    this.apply();
  }
  zoom(f) {
    const r = this.svg.getBoundingClientRect();
    this.zoomAt(r.width / 2, r.height / 2, f);
  }
  fit(box, pad = 48) {
    const r = this.svg.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const k = Math.min(1.25, Math.max(0.25, Math.min((r.width - 2 * pad) / box.w, (r.height - 2 * pad) / box.h)));
    this.k = k;
    this.tx = (r.width - box.w * k) / 2 - box.x * k;
    this.ty = (r.height - box.h * k) / 2 - box.y * k;
    this.apply();
  }
  /* Client → graph coordinates. */
  toGraph(clientX, clientY) {
    const r = this.svg.getBoundingClientRect();
    return { x: (clientX - r.left - this.tx) / this.k, y: (clientY - r.top - this.ty) / this.k };
  }
}

/* Build the canvas skeleton: background grid rect + transformed layer. */
export function mountCanvas(svg) {
  svg.innerHTML = "";
  ensureDefs(svg);
  svgEl("rect", { x: 0, y: 0, width: "100%", height: "100%", fill: "url(#wf-grid)", class: "wf-bg" }, svg);
  const layer = svgEl("g", { class: "wf-layer" }, svg);
  const overlay = svgEl("g", { class: "wf-overlay" }, layer);
  const graph = svgEl("g", { class: "wf-graph" }, layer);
  layer.insertBefore(graph, overlay);
  return { layer, graph, overlay };
}
