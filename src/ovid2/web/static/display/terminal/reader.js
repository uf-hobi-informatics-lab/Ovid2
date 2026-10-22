/*
 * Ovid2 terminal-pane event reader.
 *
 * Polls the JSON events endpoint at ``/api/v1/runs/{id}/events`` using
 * the byte-offset cursor already used by the HTMX events partial. The
 * endpoint returns a ``RunEventPage`` — ``{ instance_id, next_cursor,
 * finished, events: [...] }`` — so we just carry ``next_cursor``
 * across polls.
 *
 * Design notes:
 *
 *  - Poll cadence: default 1500 ms, jittered ±300 ms so multiple open
 *    pages don't hammer in phase. Short enough to feel live, long
 *    enough that a paused run doesn't cost real cycles.
 *  - Backoff: on network / HTTP errors we escalate to 3× the base
 *    interval up to 10 s max, then reset on the next successful call.
 *  - Termination: when the endpoint reports ``finished=true`` (an
 *    ``instance.finished`` event has been observed), we do one final
 *    poll and stop.
 *  - Cancellation: ``close()`` cancels any in-flight timer and
 *    guarantees no further ``onEvents`` calls will fire.
 *  - No SSE here — the metrics stream already burns one SSE
 *    connection per open page; we keep this one on a cheap poll.
 */

const DEFAULT_POLL_MS = 1500;
const JITTER_MS = 300;
const MAX_BACKOFF_MS = 10_000;
const DEFAULT_LIMIT = 200;

/**
 * Create a byte-cursor-tailing event reader.
 *
 * @param {object} opts
 * @param {string} opts.instanceId
 * @param {(events: Array<object>) => void} opts.onEvents
 *   Called with every non-empty batch of events fetched. May be
 *   invoked multiple times before ``onFinished``.
 * @param {() => void} [opts.onFinished]
 *   Called exactly once when the run reaches a terminal state.
 * @param {number} [opts.pollMs=1500]
 * @param {number} [opts.limit=200]
 */
export function createEventReader({
  instanceId,
  onEvents,
  onFinished,
  pollMs = DEFAULT_POLL_MS,
  limit = DEFAULT_LIMIT,
}) {
  if (!instanceId) return { close() {} };

  const baseUrl =
    "/api/v1/runs/" +
    encodeURIComponent(instanceId) +
    "/events?limit=" +
    encodeURIComponent(limit) +
    "&after=";

  let cursor = 0;
  let closed = false;
  let finished = false;
  let timer = null;
  let backoff = 1;

  function schedule(afterMs) {
    if (closed) return;
    const jitter = (Math.random() - 0.5) * 2 * JITTER_MS;
    const delay = Math.min(MAX_BACKOFF_MS, Math.max(200, afterMs + jitter));
    timer = setTimeout(pollOnce, delay);
  }

  async function pollOnce() {
    if (closed) return;
    timer = null;
    try {
      const res = await fetch(baseUrl + cursor, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const page = await res.json();
      backoff = 1;
      if (page && typeof page.next_cursor === "number") {
        cursor = page.next_cursor;
      }
      if (page && Array.isArray(page.events) && page.events.length) {
        try {
          onEvents(page.events);
        } catch (e) {
          /* keep the reader alive even if the consumer throws */
          // eslint-disable-next-line no-console
          console.warn("[ovid2.terminal] onEvents threw", e);
        }
      }
      if (page && page.finished === true) {
        finished = true;
        try {
          onFinished && onFinished();
        } catch (e) {
          /* ignore */
        }
        return;
      }
    } catch (e) {
      backoff = Math.min(backoff * 2, MAX_BACKOFF_MS / pollMs || 6);
    }
    if (!finished) schedule(pollMs * backoff);
  }

  function close() {
    closed = true;
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  // Fire the first poll straight away so the terminal doesn't start
  // completely empty — historical events (everything before the page
  // was opened) surface immediately.
  timer = setTimeout(pollOnce, 0);

  return {
    close,
    isFinished: () => finished,
    getCursor: () => cursor,
  };
}
