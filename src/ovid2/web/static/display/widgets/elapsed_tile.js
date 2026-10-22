/*
 * elapsed_tile widget — big number showing elapsed time since page load.
 *
 * Anchored to performance.now() at mount time, ticks every animation
 * frame so the number is always fresh.
 */

/*
 * Format an elapsed duration.
 *
 * Below one hour we display **deci-seconds** — one digit after the
 * decimal point on the seconds field. That makes the counter feel
 * continuously alive without pulling metrics any faster: the rAF tick
 * loop already runs at ~60 fps, so we just format ``ms`` (not a
 * sample-side value) directly and the tenths animate smoothly.
 *
 * At one hour and up we fall back to whole seconds — a decimal on
 * ``ss`` at that scale is noise.
 */
function fmtElapsed(ms) {
  if (!isFinite(ms) || ms < 0) return "—";
  const totalMs = Math.max(0, Math.floor(ms));
  const totalSecondsFloor = Math.floor(totalMs / 1000);
  const hh = Math.floor(totalSecondsFloor / 3600);
  const mm = Math.floor((totalSecondsFloor % 3600) / 60);
  const ss = totalSecondsFloor % 60;
  const tenths = Math.floor((totalMs % 1000) / 100);
  if (hh > 0) {
    // Long runs — decimal tenths are noise. Whole seconds only.
    return (
      hh +
      "h " +
      String(mm).padStart(2, "0") +
      "m " +
      String(ss).padStart(2, "0") +
      "s"
    );
  }
  if (mm > 0) {
    return mm + "m " + String(ss).padStart(2, "0") + "." + tenths + "s";
  }
  return ss + "." + tenths + "s";
}

export default {
  id: "elapsed_tile",
  slot: "elapsed",

  mount(root) {
    root.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "widget-elapsed";
    root.appendChild(wrap);

    const label = document.createElement("div");
    label.className = "widget-label";
    label.textContent = "Elapsed";
    wrap.appendChild(label);

    const num = document.createElement("div");
    num.className = "widget-number elapsed-number";
    num.style.fontVariantNumeric = "tabular-nums";
    num.textContent = "0.0s";
    wrap.appendChild(num);

    return { num, startMs: performance.now(), lastText: "0.0s" };
  },

  update() {
    /* elapsed_tile does not need per-sample update */
  },

  tick(handle, nowMs) {
    const txt = fmtElapsed(nowMs - handle.startMs);
    if (txt !== handle.lastText) {
      handle.num.textContent = txt;
      handle.lastText = txt;
    }
  },

  unmount(handle) {
    handle.num = null;
  },
};
