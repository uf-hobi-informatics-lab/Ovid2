/*
 * banner widget — shows a status banner overlay when the run enters the
 * `retrying` or `failed` state. Hidden otherwise.
 *
 * The banner listens to bus.on("state", …) directly through the mount
 * callback's bus reference. The canvas's own state-class on the root
 * element also drives CSS colour, but banner text is widget-owned.
 */

const MESSAGES = {
  retrying:
    "Some tasks are retrying — this is usually fine.",
  failed: "Run failed. See the events table below.",
  done: null,
  cooking: null,
  idle: null,
};

export default {
  id: "banner",
  slot: "banner",

  mount(root, { bus }) {
    root.innerHTML = "";
    const el = document.createElement("div");
    el.className = "live-banner";
    el.setAttribute("role", "status");
    el.hidden = true;

    const dot = document.createElement("span");
    dot.className = "live-banner-dot";
    dot.setAttribute("aria-hidden", "true");
    el.appendChild(dot);

    const text = document.createElement("span");
    text.className = "live-banner-text";
    el.appendChild(text);
    root.appendChild(el);

    const handle = { el, text };
    function apply(state) {
      const msg = MESSAGES[state] || null;
      if (!msg) {
        handle.el.hidden = true;
        handle.el.dataset.state = state;
        return;
      }
      handle.el.hidden = false;
      handle.el.dataset.state = state;
      handle.text.textContent = msg;
    }
    apply(bus.getState());
    bus.on("state", ({ state }) => apply(state));

    return handle;
  },

  update() {
    /* driven by bus.on(state) */
  },

  tick() {
    /* nothing */
  },

  unmount(handle) {
    handle.el = null;
    handle.text = null;
  },
};
