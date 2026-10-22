/*
 * executors_ring — radial gauge for executors_active / executors_total.
 *
 * Renders a background arc (full circle) at low opacity and a foreground
 * arc that sweeps from 0 to (active / total) * 2π. The active number
 * is tweened between samples using d3.interpolateNumber so the display
 * looks continuous at 60 fps even though samples arrive every 500 ms.
 */

const RADIUS = 62;
const STROKE = 12;
const SIZE = (RADIUS + STROKE) * 2 + 8;

export default {
  id: "executors_ring",
  slot: "ring",

  mount(root, { bus, d3 }) {
    root.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "widget-executors";
    root.appendChild(wrap);

    const label = document.createElement("div");
    label.className = "widget-label";
    label.textContent = "Executors";
    wrap.appendChild(label);

    const svg = d3
      .select(wrap)
      .append("svg")
      .attr("class", "widget-svg widget-executors-svg")
      .attr("viewBox", `0 0 ${SIZE} ${SIZE}`)
      .attr("preserveAspectRatio", "xMidYMid meet");

    const g = svg
      .append("g")
      .attr("transform", `translate(${SIZE / 2}, ${SIZE / 2})`);

    const arcGen = d3
      .arc()
      .innerRadius(RADIUS - STROKE / 2)
      .outerRadius(RADIUS + STROKE / 2)
      .startAngle(0)
      .cornerRadius(STROKE / 2);

    g.append("path")
      .attr("class", "arc-bg")
      .attr("d", arcGen({ endAngle: Math.PI * 2 }));

    const fg = g
      .append("path")
      .attr("class", "arc-fg")
      .attr("d", arcGen({ endAngle: 0 }));

    const num = g
      .append("text")
      .attr("class", "widget-number ring-number")
      .attr("text-anchor", "middle")
      .attr("dy", "0.1em")
      .text("—");

    const sub = g
      .append("text")
      .attr("class", "widget-sub ring-sub")
      .attr("text-anchor", "middle")
      .attr("dy", "1.6em")
      .text("of —");

    const handle = {
      arcGen,
      fg,
      num,
      sub,
      d3,
      // Interpolation state: two anchors + monotonic timestamps so
      // the tick loop can draw the tween.
      prevActive: 0,
      nextActive: 0,
      total: 0,
      prevMs: 0,
      nextMs: 0,
    };
    return handle;
  },

  update(handle, sample) {
    if (!sample) return;
    handle.prevActive = handle.nextActive;
    handle.prevMs = handle.nextMs || performance.now();
    handle.nextActive = sample.executors_active || 0;
    handle.nextMs = performance.now();
    handle.total = sample.executors_total || 0;
    // Sub-line updates immediately (denominator changes are rare).
    handle.sub.text(handle.total ? "of " + handle.total : "of —");
  },

  tick(handle, nowMs) {
    const dt = handle.nextMs - handle.prevMs;
    const alpha = dt > 0 ? Math.min(1.2, (nowMs - handle.nextMs) / dt) : 1;
    // Clamp so we never over-shoot beyond one sample worth of extrapolation.
    const a = Math.max(0, Math.min(1, alpha));
    const v =
      handle.prevActive + (handle.nextActive - handle.prevActive) * a;
    const angle =
      handle.total > 0
        ? Math.min(1, v / handle.total) * Math.PI * 2
        : 0;
    handle.fg.attr("d", handle.arcGen({ endAngle: angle }));
    handle.num.text(handle.total ? Math.round(v) : "—");
  },

  resize() {
    /* SVG uses viewBox — nothing to do on resize. */
  },

  unmount(handle) {
    // handle is owned by its parent slot; canvas.js clears the slot.
    handle.fg = null;
    handle.num = null;
    handle.sub = null;
  },
};
