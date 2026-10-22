/*
 * throughput widget — rows/sec line chart plus a live counter that tweens
 * smoothly between samples.
 *
 * The chart samples the ring buffer's rows_written_total deltas and
 * plots (rows_per_second) on a 120-point rolling window. Widths are
 * driven by viewBox so a single ResizeObserver call is enough.
 */

const VIEW_W = 320;
const VIEW_H = 96;
const PAD = 6;

export default {
  id: "throughput",
  slot: "throughput",

  mount(root, { bus, d3 }) {
    root.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "widget-throughput";
    root.appendChild(wrap);

    const label = document.createElement("div");
    label.className = "widget-label";
    label.textContent = "Rows / sec";
    wrap.appendChild(label);

    const num = document.createElement("div");
    num.className = "widget-number throughput-number";
    num.textContent = "—";
    wrap.appendChild(num);

    const svg = d3
      .select(wrap)
      .append("svg")
      .attr("class", "widget-svg widget-throughput-svg")
      .attr("viewBox", `0 0 ${VIEW_W} ${VIEW_H}`)
      .attr("preserveAspectRatio", "none");

    const gArea = svg.append("path").attr("class", "throughput-area");
    const gLine = svg.append("path").attr("class", "throughput-line");

    const x = d3.scaleLinear().range([PAD, VIEW_W - PAD]);
    const y = d3.scaleLinear().range([VIEW_H - PAD, PAD]);

    const line = d3
      .line()
      .x((_, i, arr) => x(i / Math.max(1, arr.length - 1)))
      .y((d) => y(d))
      .curve(d3.curveMonotoneX);

    const area = d3
      .area()
      .x((_, i, arr) => x(i / Math.max(1, arr.length - 1)))
      .y0(VIEW_H - PAD)
      .y1((d) => y(d))
      .curve(d3.curveMonotoneX);

    const handle = {
      wrap,
      num,
      gArea,
      gLine,
      x,
      y,
      line,
      area,
      d3,
      series: [], // rows/sec floats, at most 120 entries
      // Interpolation anchors for the number counter.
      prevRate: 0,
      nextRate: 0,
      prevMs: 0,
      nextMs: 0,
      // Rolling accumulator for rows_written_total so we can compute
      // deltas without hitting the ring buffer.
      lastRows: null,
      lastMs: null,
    };
    return handle;
  },

  update(handle, sample) {
    if (!sample) return;
    const t = sample.t_ms || 0;
    const rows = sample.rows_written_total;
    let rate = 0;
    if (
      rows != null &&
      handle.lastRows != null &&
      handle.lastMs != null &&
      t > handle.lastMs
    ) {
      const dr = rows - handle.lastRows;
      const dt = t - handle.lastMs;
      if (dr >= 0 && dt > 0) rate = (dr * 1000) / dt;
    }
    if (rows != null) {
      handle.lastRows = rows;
      handle.lastMs = t;
    }

    handle.series.push(rate);
    while (handle.series.length > 120) handle.series.shift();

    // Rescale y to the current window (log-ish: clamp min to 1 so a
    // small trickle still shows some height).
    const max = Math.max(1, ...handle.series);
    handle.y.domain([0, max]);
    handle.x.domain([0, 1]);

    // Redraw with a soft transition so segment additions don't step.
    handle.gArea
      .datum(handle.series)
      .transition()
      .duration(180)
      .attr("d", handle.area);
    handle.gLine
      .datum(handle.series)
      .transition()
      .duration(180)
      .attr("d", handle.line);

    // Tween the big counter.
    handle.prevRate = handle.nextRate;
    handle.prevMs = handle.nextMs || performance.now();
    handle.nextRate = rate;
    handle.nextMs = performance.now();
  },

  tick(handle, nowMs) {
    const dt = handle.nextMs - handle.prevMs;
    const alpha = dt > 0 ? Math.min(1, (nowMs - handle.nextMs) / dt) : 1;
    const a = Math.max(0, alpha);
    const v = handle.prevRate + (handle.nextRate - handle.prevRate) * a;
    const rounded = Math.max(0, Math.round(v));
    handle.num.textContent = rounded.toLocaleString();
  },

  resize() {
    /* viewBox handles resize */
  },

  unmount(handle) {
    handle.series.length = 0;
  },
};
