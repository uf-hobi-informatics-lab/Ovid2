/*
 * Ovid2 display bus.
 *
 * A tiny pub/sub over the live metrics stream. Owns:
 *   - The SSE EventSource (plus polling fallback) to the metrics endpoint.
 *   - A short ring buffer of samples (120 entries, ~60s at 500ms).
 *   - The state classifier (idle | cooking | retrying | done | failed).
 *   - The requestAnimationFrame loop that drives widget.tick().
 *
 * Widgets subscribe:
 *
 *   bus.on("sample", ({ sample, prev, dtMs }) => { ... });
 *   bus.on("state",  ({ state, prevState }) => { ... });
 *   bus.on("tick",   ({ nowMs, latest }) => { ... });
 *
 * No framework, one file. Import as an ES module.
 */

const RING_LEN = 120;

/**
 * Format an event stream frame parser: SSE frames arrive as raw JSON strings
 * on either the "sample" or "finished" event; we normalise those into a
 * uniform bus.emit("sample", …).
 */
export function createBus({ instanceId, maxReconnect = 5 }) {
  const listeners = new Map(); // event name -> Set<cb>
  const ring = [];
  let state = "idle";
  let lastFailed = 0;
  let failedClimbCount = 0;

  const streamUrl =
    "/api/v1/runs/" + encodeURIComponent(instanceId) + "/metrics/stream";
  const latestUrl =
    "/api/v1/runs/" + encodeURIComponent(instanceId) + "/metrics/latest";

  function on(event, cb) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(cb);
    return () => listeners.get(event).delete(cb);
  }

  function emit(event, payload) {
    const set = listeners.get(event);
    if (!set) return;
    for (const cb of set) {
      try {
        cb(payload);
      } catch (e) {
        // Widgets must never crash the bus. Swallow + console.warn.
        // eslint-disable-next-line no-console
        console.warn("[ovid2.display] widget threw in", event, e);
      }
    }
  }

  function classify(sample) {
    if (!sample) return "idle";
    if (sample._finished === "failed") return "failed";
    if (sample._finished === "ok") return "done";
    const active = sample.tasks_active || 0;
    const failed = sample.tasks_failed_total || 0;
    if (failed > lastFailed) failedClimbCount += 1;
    else if (active > 0) failedClimbCount = 0;
    lastFailed = failed;
    if (failedClimbCount >= 2) return "retrying";
    if (active > 0) return "cooking";
    return "idle";
  }

  function pushSample(sample) {
    const prev = ring[ring.length - 1];
    ring.push(sample);
    while (ring.length > RING_LEN) ring.shift();
    const dtMs =
      prev && sample && sample.t_ms && prev.t_ms
        ? sample.t_ms - prev.t_ms
        : 0;
    emit("sample", { sample, prev, dtMs, ring });
    const nextState = classify(sample);
    if (nextState !== state) {
      const prevState = state;
      state = nextState;
      emit("state", { state, prevState });
    }
  }

  // ---- data source: EventSource + polling fallback ------------------

  let reconnects = 0;
  let polling = false;
  let closed = false;

  function startSSE() {
    if (closed) return;
    if (typeof EventSource === "undefined") {
      startPolling();
      return;
    }
    const es = new EventSource(streamUrl);
    es.addEventListener("sample", (ev) => {
      try {
        pushSample(JSON.parse(ev.data));
        reconnects = 0;
      } catch (e) {
        /* ignore malformed frame */
      }
    });
    es.addEventListener("finished", () => {
      const last = ring[ring.length - 1] || {};
      // Best-effort: we don't get success/fail on the frame itself.
      // Widgets can further narrow via the `state` classifier when
      // a `table.failed` event bumps the reason.
      pushSample({ ...last, _finished: "ok" });
      es.close();
    });
    es.onerror = () => {
      es.close();
      if (closed) return;
      if (reconnects < maxReconnect) {
        const backoff = Math.min(30_000, 500 * Math.pow(2, reconnects));
        reconnects += 1;
        setTimeout(startSSE, backoff);
      } else {
        startPolling();
      }
    };
  }

  function startPolling() {
    if (polling || closed) return;
    polling = true;
    const tick = () => {
      if (closed) return;
      fetch(latestUrl, { credentials: "same-origin" })
        .then((r) => (r.ok ? r.json() : null))
        .then((json) => {
          if (json && json.kind === "engine.metrics") pushSample(json);
        })
        .catch(() => {
          /* ignore */
        })
        .finally(() => {
          if (!closed) setTimeout(tick, 2000);
        });
    };
    tick();
  }

  // ---- rAF loop -----------------------------------------------------

  let rafHandle = null;
  function frame(nowMs) {
    if (closed) return;
    emit("tick", { nowMs, latest: ring[ring.length - 1] });
    rafHandle = requestAnimationFrame(frame);
  }
  rafHandle = requestAnimationFrame(frame);

  function close() {
    closed = true;
    if (rafHandle != null) cancelAnimationFrame(rafHandle);
  }

  startSSE();

  return {
    on,
    emit,
    close,
    getRing: () => ring.slice(),
    getState: () => state,
    getLatest: () => ring[ring.length - 1],
    instanceId,
  };
}

export const RING_LENGTH = RING_LEN;
