/*
 * stage_gantt widget — horizontal stacked bar showing
 * completed | running | pending stages out of stages_total.
 *
 * Also prints the current_stage name below the bar. Both segments
 * transition on width with d3.transition().duration(220).
 */

const VIEW_W = 320;
const VIEW_H = 22;

export default {
  id: "stage_gantt",
  slot: "gantt",

  mount(root, { bus, d3 }) {
    root.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "widget-gantt";
    root.appendChild(wrap);

    const label = document.createElement("div");
    label.className = "widget-label";
    label.textContent = "Stage";
    wrap.appendChild(label);

    const stageName = document.createElement("div");
    stageName.className = "widget-sub gantt-stage";
    stageName.textContent = "—";
    wrap.appendChild(stageName);

    const svg = d3
      .select(wrap)
      .append("svg")
      .attr("class", "widget-svg widget-gantt-svg")
      .attr("viewBox", `0 0 ${VIEW_W} ${VIEW_H}`)
      .attr("preserveAspectRatio", "none");

    // Background full-width bar (rounded rect).
    svg
      .append("rect")
      .attr("class", "gantt-bg")
      .attr("x", 0)
      .attr("y", 4)
      .attr("width", VIEW_W)
      .attr("height", VIEW_H - 8)
      .attr("rx", (VIEW_H - 8) / 2);

    const completed = svg
      .append("rect")
      .attr("class", "gantt-completed")
      .attr("x", 0)
      .attr("y", 4)
      .attr("height", VIEW_H - 8)
      .attr("width", 0)
      .attr("rx", (VIEW_H - 8) / 2);

    const running = svg
      .append("rect")
      .attr("class", "gantt-running")
      .attr("x", 0)
      .attr("y", 4)
      .attr("height", VIEW_H - 8)
      .attr("width", 0);

    const handle = { completed, running, stageName, d3, currentStage: "" };
    return handle;
  },

  update(handle, sample) {
    if (!sample) return;
    const total = Math.max(1, sample.stages_total || 0);
    const done = Math.min(total, sample.stages_completed_total || 0);
    const active = sample.tasks_active > 0 ? 1 : 0; // one running stage bar
    const wDone = (done / total) * VIEW_W;
    const wRun = Math.min(VIEW_W - wDone, (active / total) * VIEW_W);

    handle.completed
      .transition()
      .duration(220)
      .attr("width", wDone);

    handle.running
      .transition()
      .duration(220)
      .attr("x", wDone)
      .attr("width", wRun);

    const stage = sample.current_stage || "—";
    if (stage !== handle.currentStage) {
      handle.currentStage = stage;
      // Fade the stage name on change.
      handle.stageName.style.opacity = "0";
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          handle.stageName.textContent = stage;
          handle.stageName.style.opacity = "1";
        })
      );
    }
  },

  tick() {
    /* no per-frame interpolation — d3.transition drives width */
  },

  unmount(handle) {
    handle.completed = null;
    handle.running = null;
  },
};
