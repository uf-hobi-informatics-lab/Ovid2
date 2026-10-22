/*
 * pulse_bar widget — 4px top pulse that ripples during `cooking` and
 * settles during `idle` / `done` / `failed`. Reads state from the bus.
 */

export default {
  id: "pulse_bar",
  slot: "pulse",

  mount(root, { bus }) {
    root.innerHTML = "";
    const bar = document.createElement("div");
    bar.className = "pulse-bar";
    bar.setAttribute("aria-hidden", "true");
    root.appendChild(bar);

    const handle = { bar, state: bus.getState() };
    // Reflect state class on the bar itself so animation CSS can key
    // off it independent of the outer .live-canvas[data-state="…"].
    bar.dataset.state = handle.state;
    bus.on("state", ({ state }) => {
      handle.state = state;
      handle.bar.dataset.state = state;
    });
    return handle;
  },

  update() {
    /* pulse_bar has no per-sample update */
  },

  tick() {
    /* animation is pure CSS */
  },

  unmount(handle) {
    if (handle && handle.bar && handle.bar.parentNode) {
      handle.bar.parentNode.removeChild(handle.bar);
    }
  },
};
