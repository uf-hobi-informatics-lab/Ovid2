/*
 * Ovid2 display canvas.
 *
 * Root SVG element + grid layout for widget slots. Widgets mount into
 * pre-labelled slots so their positions are stable and the layout can
 * evolve without every widget knowing about it.
 *
 * Layout (grid areas):
 *
 *   ┌────────────── pulse ──────────────┐
 *   │  ring       │      throughput      │
 *   │  elapsed    │      gantt           │
 *   └───────────────────────────────────┘
 *   banner (absolute overlay)
 *
 * Slot ids: "pulse", "ring", "throughput", "elapsed", "gantt", "banner".
 *
 * Every widget is a module with the shape:
 *
 *   export default {
 *     id: "executors_ring",
 *     mount(root, { bus, d3, size }) { return handle; },
 *     update(sample, prev, dtMs) {},
 *     tick(nowMs, latest) {},
 *     unmount(handle) {},
 *   };
 *
 * The canvas invokes update/tick automatically by subscribing on the
 * bus for the widget. Widgets never touch the bus themselves.
 */

import { createBus } from "./bus.js";

/**
 * Mount a display canvas onto `rootEl` and register the given widgets.
 *
 * @param {HTMLElement} rootEl - container element (already in DOM).
 * @param {object} opts
 * @param {string} opts.instanceId - run id for the SSE endpoint.
 * @param {Array}  opts.widgets    - list of widget modules to register.
 * @param {object} opts.d3         - global d3 object (window.d3).
 */
export function mountCanvas(rootEl, { instanceId, widgets, d3 }) {
  if (!rootEl || !instanceId) return null;

  const bus = createBus({ instanceId });

  // Build the DOM skeleton once. Each slot is a <div> so widgets can
  // hold either an inline SVG (charts) or plain HTML (banner, number
  // tiles) — the outer canvas is intentionally shape-agnostic.
  rootEl.innerHTML = "";
  rootEl.classList.add("live-canvas");

  function slot(id, extraClass = "") {
    const el = document.createElement("div");
    el.className = "live-slot slot-" + id + (extraClass ? " " + extraClass : "");
    el.dataset.slot = id;
    rootEl.appendChild(el);
    return el;
  }

  const slots = {
    pulse: slot("pulse"),
    ring: slot("ring"),
    throughput: slot("throughput"),
    elapsed: slot("elapsed"),
    gantt: slot("gantt"),
    banner: slot("banner"),
  };

  // Track state at the root so CSS can react (already the previous
  // contract on .live-shell[data-state]). Widgets can still opt in
  // to state changes via bus.on("state", ...).
  rootEl.dataset.state = bus.getState();
  bus.on("state", ({ state }) => {
    rootEl.dataset.state = state;
  });

  // ResizeObserver — widgets that care redraw on size change.
  let widgetHandles = [];
  const ro = new ResizeObserver(() => {
    for (const w of widgetHandles) {
      if (w.module && typeof w.module.resize === "function") {
        try {
          w.module.resize(w.handle, sizeOf(w.slot));
        } catch (e) {
          // eslint-disable-next-line no-console
          console.warn("[ovid2.display] resize", w.module.id, e);
        }
      }
    }
  });

  function sizeOf(el) {
    const r = el.getBoundingClientRect();
    return { width: r.width, height: r.height };
  }

  // Mount every widget into its declared slot.
  widgetHandles = widgets
    .map((mod) => {
      const slotName = mod.slot || mod.id;
      const el = slots[slotName];
      if (!el) {
        // eslint-disable-next-line no-console
        console.warn("[ovid2.display] no slot for widget", mod.id, slotName);
        return null;
      }
      let handle = null;
      try {
        handle = mod.mount(el, { bus, d3, size: sizeOf(el) });
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn("[ovid2.display] mount failed", mod.id, e);
        return null;
      }
      ro.observe(el);
      // Wire bus -> widget lifecycle callbacks.
      if (typeof mod.update === "function") {
        bus.on("sample", ({ sample, prev, dtMs }) => {
          try {
            mod.update(handle, sample, prev, dtMs);
          } catch (e) {
            // eslint-disable-next-line no-console
            console.warn("[ovid2.display] update", mod.id, e);
          }
        });
      }
      if (typeof mod.tick === "function") {
        bus.on("tick", ({ nowMs, latest }) => {
          try {
            mod.tick(handle, nowMs, latest);
          } catch (e) {
            // eslint-disable-next-line no-console
            console.warn("[ovid2.display] tick", mod.id, e);
          }
        });
      }
      return { module: mod, handle, slot: el };
    })
    .filter((x) => x !== null);

  function unmount() {
    for (const w of widgetHandles) {
      if (typeof w.module.unmount === "function") {
        try {
          w.module.unmount(w.handle);
        } catch (e) {
          /* ignore */
        }
      }
    }
    widgetHandles = [];
    ro.disconnect();
    bus.close();
    rootEl.innerHTML = "";
  }

  return { bus, unmount };
}
