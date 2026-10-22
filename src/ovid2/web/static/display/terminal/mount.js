/*
 * Ovid2 terminal-pane mount module.
 *
 * Renders the right-side "peek at the backend" terminal window on the
 * run-detail page. Combines:
 *
 *  - :func:`createEventReader` — polls the JSON events endpoint using
 *    a byte-offset cursor.
 *  - :func:`formatEvent` — turns each JSONL record into a colour-
 *    coded terminal line.
 *  - a lightweight D3 join that appends new lines and evicts the
 *    oldest once the buffer fills.
 *  - a rAF-driven elapsed counter with deci-second resolution so the
 *    header strip feels continuously alive without hammering the
 *    server.
 *  - a "peek but stay put" auto-scroll: we stick to the bottom
 *    unless the operator has scrolled up (matches ``tail -f``).
 *  - a tiny status bus (``window.dispatchEvent``) that fires when
 *    an ``instance.finished`` / ``batch.failed`` event lands so the
 *    HTMX job-status partial can refresh immediately instead of
 *    waiting for its next 2-second poll.
 *
 * The mount is idempotent — calling ``boot()`` twice on the same
 * element is a no-op.
 */

import { createEventReader } from "/static/display/terminal/reader.js";
import { formatEvent, TERMINAL_KINDS } from "/static/display/terminal/formatter.js";
import { parseTsMs } from "/static/display/tz.js";

const DEFAULT_BUFFER = 400;
const AUTOSCROLL_SLOP_PX = 12;

// Lucide "play" / "stop" glyphs, kept byte-for-byte in sync with
// _icons.html (size=15) so a live prompt-cursor flip is indistinguish-
// able from a server-rendered one.
const _SVG_ATTRS =
  'width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
  'stroke-linejoin="round"';
const PLAY_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" ' +
  _SVG_ATTRS +
  ' class="icon icon-play" aria-hidden="true">' +
  '<polygon points="6 3 20 12 6 21 6 3"/></svg>';
const STOP_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" ' +
  _SVG_ATTRS +
  ' class="icon icon-stop" aria-hidden="true">' +
  '<rect x="5" y="5" width="14" height="14"/></svg>';

/**
 * Boot the terminal pane inside the given ``.terminal-pane`` element.
 *
 * @param {HTMLElement} pane
 *   The ``<aside class="terminal-pane" data-instance-id="…">`` root.
 * @param {object} [opts]
 * @param {object} [opts.d3] - global d3; falls back to ``window.d3``.
 * @param {(events: Array<object>) => void} [opts.onEvents]
 *   Optional consumer invoked with every non-empty batch of events the
 *   reader fetches, *before* they are rendered as terminal lines. Used
 *   by the per-table summary/detail panel so it can share this single
 *   event stream rather than opening a second poll. Errors thrown by
 *   the consumer are swallowed so they can never break the terminal.
 * @returns {object|null} handle with ``.close()`` or ``null`` when
 *   the pane cannot be mounted (already mounted / missing instance
 *   id / d3 unavailable).
 */
export function mountTerminal(pane, opts) {
  if (!pane || pane.dataset.mounted === "1") return null;
  const d3 = (opts && opts.d3) || (typeof window !== "undefined" ? window.d3 : null);
  if (!d3) {
    // eslint-disable-next-line no-console
    console.warn("[ovid2.terminal] d3 unavailable; terminal pane disabled");
    return null;
  }
  const instanceId = pane.dataset.instanceId;
  if (!instanceId) return null;

  const onEventsConsumer =
    opts && typeof opts.onEvents === "function" ? opts.onEvents : null;

  // The scrolling terminal body (`<pre class="term-body">`) is
  // optional. The run-detail page removed it (and the live meta
  // strip) — the reader still runs to drive the status pill, the
  // total-elapsed footer clock and the per-table summary/detail
  // panel, it just no longer renders terminal lines. When the body
  // is absent we skip the D3 line buffer entirely.
  const body = pane.querySelector(".term-body");

  const bufferSize = body
    ? numAttr(body, "data-buffer-lines", DEFAULT_BUFFER)
    : DEFAULT_BUFFER;
  const pollMs = body
    ? numAttr(body, "data-poll-ms", 1500)
    : numAttr(pane, "data-poll-ms", 1500);

  // Header refs — updated live off ``instance.started`` /
  // ``table.started`` events plus a rAF-driven elapsed field.
  const header = pane.querySelector(".term-header");
  const pipelineEl = header ? header.querySelector(".term-pipeline") : null;
  const batchEl = header ? header.querySelector(".term-batch") : null;
  const pathEl = header ? header.querySelector(".term-path") : null;
  const elapsedEl = header ? header.querySelector(".term-elapsed") : null;
  // Live per-table timer field (ovid parity — the "currently
  // running" job with its own MM:SS.s counter) and the footer
  // total-elapsed field.
  const currentEl = header ? header.querySelector(".term-current") : null;
  const totalEl = pane.querySelector(".term-total");
  // Status pill in the job-details header block. Updated on
  // terminal-state events so the operator sees running -> done /
  // failed without waiting for the HTMX status poll.
  //
  // The top-right status pill was removed; the "ovid2" prompt label is
  // now the run-status indicator. We drive its ``data-status`` so CSS
  // recolours it live (dim green queued, green running, dimmed while a
  // stop is pending, solid green/red/purple when terminal).
  const promptLabel = pane.querySelector(".term-prompt-label");
  // Prompt transport control: the leading ▶/■ glyph is a submit button
  // wrapped in a POST form. While the run is queued/running it is a ■
  // Stop (posts /cancel); once terminal it becomes a ▶ Play (posts
  // /rerun). We flip the shape live when the run crosses into a terminal
  // state so it matches the label without waiting for a page reload.
  const promptBtn = pane.querySelector("button.term-prompt-btn");
  const promptForm = promptBtn ? promptBtn.closest(".term-prompt-form") : null;
  const jobId = pane.dataset.jobId || null;
  // Set once the operator clicks ■ Stop. The backend keeps the job
  // ``running`` until the worker tears it down, so we surface a pending
  // "stopping" state on the label and, once the run finally terminates,
  // report it as ``cancelled`` (the user asked for it) rather than
  // whatever exit status the SIGTERM produced.
  let stopRequested = false;

  // ---- line buffer + D3 render ------------------------------------
  //
  // Only wired when the terminal body is present. When it's absent
  // (the run-detail page) ``render()`` becomes a no-op and the reader
  // still drives the status pill, clock, and table panel below.
  const container = body ? d3.select(body) : null;
  let cursor = null;
  const lines = []; // Array<FormattedLine>
  let stickyBottom = true;

  if (body && container) {
    container.selectAll("*").remove();
    cursor = container
      .append("span")
      .attr("class", "term-cursor")
      .attr("aria-hidden", "true");

    body.addEventListener("scroll", () => {
      const atBottom =
        body.scrollTop + body.clientHeight >=
        body.scrollHeight - AUTOSCROLL_SLOP_PX;
      stickyBottom = atBottom;
    });
  }

  function render(newBatch) {
    if (!body || !cursor) return;
    // Append new lines before the trailing cursor span. We keep the
    // buffer bounded by evicting from the front.
    for (const ln of newBatch) {
      lines.push(ln);
    }
    while (lines.length > bufferSize) lines.shift();

    // Data-join keyed by insertion index so we only touch the tail.
    // We render the full buffer only when we have to trim; otherwise
    // we append the new lines directly.
    if (newBatch.length && lines.length <= bufferSize && newBatch.length < 32) {
      // Fast path — append.
      for (const ln of newBatch) {
        const el = document.createElement("span");
        el.className = "term-line " + ln.className;
        el.innerHTML =
          '<span class="term-ts">' + escapeHtml(ln.ts) + "</span>" +
          escapeHtml(ln.text);
        body.insertBefore(el, cursor.node());
      }
      // Evict old rendered rows to match the buffer.
      const rendered = body.querySelectorAll(".term-line");
      const excess = rendered.length - lines.length;
      for (let i = 0; i < excess; i += 1) {
        body.removeChild(rendered[i]);
      }
    } else {
      // Slow path — full rebuild (buffer trim or big batch).
      const rendered = body.querySelectorAll(".term-line");
      for (const el of rendered) body.removeChild(el);
      const frag = document.createDocumentFragment();
      for (const ln of lines) {
        const el = document.createElement("span");
        el.className = "term-line " + ln.className;
        el.innerHTML =
          '<span class="term-ts">' + escapeHtml(ln.ts) + "</span>" +
          escapeHtml(ln.text);
        frag.appendChild(el);
      }
      body.insertBefore(frag, cursor.node());
    }

    if (stickyBottom) {
      body.scrollTop = body.scrollHeight;
    }
  }

  // ---- header live-update -----------------------------------------
  function applyMetaFromEvent(rec) {
    if (!rec) return;
    const pipeline = rec.pipeline;
    const batchId = rec.batch_id;
    const path = rec.path;
    if (pipelineEl && pipeline && pipelineEl.textContent !== pipeline) {
      pipelineEl.textContent = pipeline;
      pane.dataset.pipeline = pipeline;
    }
    if (batchEl && batchId && batchEl.textContent !== batchId) {
      batchEl.textContent = batchId;
      pane.dataset.batch = batchId;
    }
    if (pathEl && path && pathEl.getAttribute("title") !== path) {
      pathEl.textContent = path;
      pathEl.setAttribute("title", path);
      pane.dataset.path = path;
    }
  }

  // ---- currently-active table (ovid live per-job timer) -----------
  //
  // ovid2 runs lazily (one Spark DAG), so per-table timing only has
  // real anchors at ``table.started`` and ``table.completed`` /
  // ``table.failed``. Between those two we animate a MM:SS.s counter
  // anchored to the local wall clock at the moment we observed the
  // start, matching the "--Running: <table> ... MM:SS.s" feel of the
  // original ovid console. On completion we freeze the field to the
  // server-reported ``elapsed_ms`` when present.
  let activeTable = null; // { name, startedMs }
  let frozenCurrentText = "";

  function beginTable(name) {
    activeTable = { name: String(name || ""), startedMs: performance.now() };
    frozenCurrentText = "";
  }

  function endTable(name, elapsedMs) {
    // Only clear if the completed table is the one we're tracking
    // (out-of-order events are rare but cheap to guard against).
    if (activeTable && (!name || activeTable.name === String(name))) {
      const el = Number(elapsedMs);
      const secs = Number.isFinite(el) && el >= 0
        ? el / 1000
        : (performance.now() - activeTable.startedMs) / 1000;
      frozenCurrentText =
        (activeTable.name ? activeTable.name + " " : "") + fmtClock(secs * 1000);
      activeTable = null;
    }
  }

  // ---- run status -------------------------------------------------
  // Status is shown on the "ovid2" label (via data-status), not on the
  // transport glyph. The glyph stays a soft green and only switches
  // shape (■ Stop while running, ▶ Play once terminal — no animation).
  function setStatus(status) {
    if (!status) return;
    if (promptLabel && promptLabel.dataset.status !== status) {
      promptLabel.dataset.status = status;
    }
    // The "queued — waiting for …" note only makes sense while queued.
    const queueNote = pane.querySelector(".term-queue-note");
    if (queueNote && status !== "queued") queueNote.hidden = true;
    // The "stopping" pseudo-status keeps the ■ Stop glyph (the run is
    // still in-flight) but leaves it inert — don't run the shape swap.
    if (status === "stopping") return;
    // Keep the transport glyph in sync (Stop while in-flight, Play once
    // terminal). flipPrompt is a no-op when nothing changed.
    flipPrompt(status);
  }

  // Swap the prompt transport control to match the run status. Terminal
  // statuses => a steady ▶ Play triangle (/rerun); in-flight statuses =>
  // a static ■ Stop square (/cancel). The glyph is always a soft green
  // and shows no status. Only the glyph is clickable; the "ovid2" label
  // beside it is inert. No-op when there is no interactive prompt button
  // (no job) or no job id to rebuild the form action from.
  function flipPrompt(status) {
    if (!promptBtn || !promptForm || !jobId) return;
    // A draft keeps its (breathing) ▶ launch control until the operator
    // presses it; only the launch handler below changes it.
    if (promptBtn.dataset.action === "launch") return;
    const terminal = TERMINAL_STATUSES[status] === 1;
    // The glyph is a plain transport control — always green, no status
    // colour and no failure/warning shape: running => ■ Stop, terminal
    // => ▶ Play. The run status lives on the "ovid2" label, not here.
    const runState = terminal ? "done" : "running";
    const wantAction = terminal ? "rerun" : "cancel";
    // Nothing to do if both the action and the visual state are current.
    if (
      promptBtn.dataset.action === wantAction &&
      promptBtn.dataset.runState === runState
    ) {
      return;
    }
    promptBtn.dataset.action = wantAction;
    promptBtn.dataset.runState = runState;
    if (terminal) {
      promptForm.setAttribute("action", "/jobs/" + jobId + "/rerun");
      promptForm.onsubmit = null;
      // The run is over — clear any pending-stop lock so ▶ Play (rerun)
      // is clickable again.
      promptBtn.removeAttribute("data-pending");
      promptBtn.disabled = false;
      promptBtn.setAttribute("aria-label", "Rerun");
      promptBtn.setAttribute("title", "Rerun");
      // Always the upright green Play triangle — no inverted glyph.
      setPromptGlyph(PLAY_SVG);
    } else {
      promptForm.setAttribute("action", "/jobs/" + jobId + "/cancel");
      promptForm.onsubmit = null;
      promptBtn.setAttribute("aria-label", "Cancel");
      promptBtn.setAttribute("title", "Cancel");
      setPromptGlyph(STOP_SVG);
    }
  }

  function setPromptGlyph(svg) {
    const glyph = promptBtn
      ? promptBtn.querySelector(".term-prompt-glyph")
      : null;
    if (glyph) glyph.innerHTML = svg;
  }

  // Cancel the run in the background (no page reload) and surface a
  // pending "stopping" state: the run stays in-flight until the worker
  // tears it down, so the label dims and the ■ glyph goes inert.
  function requestStop() {
    if (!jobId || stopRequested) return;
    stopRequested = true;
    setStatus("stopping");
    if (promptBtn) {
      promptBtn.setAttribute("data-pending", "1");
      promptBtn.disabled = true;
      promptBtn.setAttribute("aria-label", "Stopping…");
      promptBtn.setAttribute("title", "Stopping…");
    }
    try {
      fetch("/jobs/" + jobId + "/cancel", {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      }).catch(function () {
        /* best-effort; the run will still stop on its own if this fails */
      });
    } catch (_e) {
      /* fetch unavailable — the plain form submit fallback handles it */
    }
  }

  // Rerun the finished run in the background (no full-document reload)
  // and hand off to the smooth in-place navigation in live.js. We POST
  // the same HTML ``/rerun`` route the plain form uses — so the session
  // cookie authorises it exactly as it does for the fallback submit —
  // but with an ``X-Requested-With: fetch`` header so the handler
  // returns the new run's ``instance_id`` as JSON instead of a 303.
  // On success we dispatch ``ovid2:rerun-navigate``; live.js swaps the
  // new run page into ``#main`` and pushes history. Any failure (no
  // fetch, non-2xx, missing id) rejects so the caller can fall back to
  // a plain navigation.
  let rerunRequested = false;
  function requestRerun() {
    if (!jobId || rerunRequested) return Promise.reject(new Error("no-job"));
    rerunRequested = true;
    if (promptBtn) {
      promptBtn.setAttribute("data-pending", "1");
      promptBtn.disabled = true;
      promptBtn.setAttribute("aria-label", "Rerunning…");
      promptBtn.setAttribute("title", "Rerunning…");
    }
    const clearPending = function () {
      rerunRequested = false;
      if (promptBtn) {
        promptBtn.removeAttribute("data-pending");
        promptBtn.disabled = false;
        promptBtn.setAttribute("aria-label", "Rerun");
        promptBtn.setAttribute("title", "Rerun");
      }
    };
    return fetch("/jobs/" + jobId + "/rerun", {
      method: "POST",
      headers: {
        "X-Requested-With": "fetch",
        Accept: "application/json",
      },
      credentials: "same-origin",
    })
      .then(function (r) {
        if (!r.ok) throw new Error("rerun-failed:" + r.status);
        return r.json();
      })
      .then(function (data) {
        const newId = data && data.instance_id;
        if (!newId) throw new Error("rerun-no-instance-id");
        // Hand off the smooth navigation to live.js (which owns the
        // terminal boot lifecycle). If nobody handles the event, fall
        // back to a plain navigation so the operator still lands on the
        // new run.
        let handled = false;
        try {
          const ev = new CustomEvent("ovid2:rerun-navigate", {
            detail: {
              instanceId: newId,
              url: "/runs/" + encodeURIComponent(newId),
            },
            cancelable: true,
          });
          window.dispatchEvent(ev);
          handled = ev.defaultPrevented;
        } catch (_e) {
          /* CustomEvent unavailable — fall through to hard nav */
        }
        if (!handled) {
          window.location.assign("/runs/" + encodeURIComponent(newId));
        }
        return newId;
      })
      .catch(function (err) {
        clearPending();
        throw err;
      });
  }

  // Launch a draft (draft -> queued) in the background, then re-render
  // this same run page in place via live.js so the console arms its
  // live status poll and the ▶ becomes a ■ Stop. Rejects on failure so
  // the caller falls back to the native form POST.
  let launchRequested = false;
  function requestLaunch() {
    if (!jobId || launchRequested) return Promise.reject(new Error("no-job"));
    launchRequested = true;
    if (promptBtn) {
      promptBtn.setAttribute("data-pending", "1");
      promptBtn.disabled = true;
      promptBtn.classList.remove("run-ready");
      promptBtn.setAttribute("aria-label", "Starting…");
      promptBtn.setAttribute("title", "Starting…");
    }
    return fetch("/jobs/" + jobId + "/launch", {
      method: "POST",
      headers: { "X-Requested-With": "fetch", Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) {
        if (!r.ok) throw new Error("launch-failed:" + r.status);
        return r.json();
      })
      .then(function (data) {
        const id = (data && data.instance_id) || pane.getAttribute("data-instance-id");
        const url = "/runs/" + encodeURIComponent(id);
        let handled = false;
        try {
          const ev = new CustomEvent("ovid2:rerun-navigate", {
            detail: { instanceId: id, url: url },
            cancelable: true,
          });
          window.dispatchEvent(ev);
          handled = ev.defaultPrevented;
        } catch (_e) {
          /* CustomEvent unavailable — fall through to hard nav */
        }
        if (!handled) window.location.assign(url);
        return id;
      })
      .catch(function (err) {
        launchRequested = false;
        if (promptBtn) {
          promptBtn.removeAttribute("data-pending");
          promptBtn.disabled = false;
          promptBtn.classList.add("run-ready");
          promptBtn.setAttribute("aria-label", "Run this job");
          promptBtn.setAttribute("title", "Ready — run this job");
        }
        throw err;
      });
  }

  // Intercept the ■ Stop / ▶ Rerun submit so we POST in the background
  // instead of navigating away with a full-document reload. Cancel stays
  // put (same page); Rerun hands off to a smooth in-place swap of the
  // new run page. If the background request cannot start (no fetch, or a
  // synchronous throw), we let the native form POST proceed as a
  // fallback.
  if (promptForm) {
    promptForm.addEventListener("submit", function (ev) {
      if (!promptBtn) return;
      const action = promptBtn.dataset.action;
      if (action === "cancel") {
        ev.preventDefault();
        requestStop();
      } else if (action === "launch") {
        if (typeof window.fetch !== "function") return; // native POST fallback
        ev.preventDefault();
        requestLaunch().catch(function () {
          try {
            promptForm.submit();
          } catch (_e) {
            /* give up silently */
          }
        });
      } else if (action === "rerun") {
        if (typeof window.fetch !== "function") return; // native POST fallback
        ev.preventDefault();
        requestRerun().catch(function () {
          // requestRerun already navigates on the happy path and only
          // rejects when it could not start / complete; fall back to a
          // plain navigation to the rerun route so the click is never a
          // dead end.
          try {
            promptForm.submit();
          } catch (_e) {
            /* give up silently — the button is re-enabled by clearPending */
          }
        });
      }
    });
  }

  function applyStatusFromEvent(rec) {
    const k = rec && rec.kind;
    // A run that was already terminal at page load must never be driven
    // back to a live state by replayed history. The event log of a
    // cancelled run still contains the ``instance.started`` from when it
    // was launched; replaying it here would flip the ovid2 label to
    // "running" and the transport glyph back to a ■ Stop square. The
    // server-provided ``data-status`` (queued/running only when the run
    // is genuinely in-flight) is authoritative, so ignore status
    // transitions from the historical stream for terminal-on-load runs.
    if (startedTerminal) return;
    if (k === "instance.started") {
      setStatus("running");
    } else if (k === "instance.finished") {
      const ok = getExtra(rec, "success", true);
      // If the operator asked to stop, report the terminal state as a
      // cancellation (purple) regardless of the SIGTERM exit status.
      setStatus(stopRequested ? "cancelled" : ok ? "done" : "failed");
    } else if (k === "batch.failed" || k === "table.failed") {
      // Don't override a later terminal status; a table failure does
      // not necessarily fail the whole run, so only reflect it if the
      // label is still pre-terminal.
      const cur = promptLabel ? promptLabel.dataset.status : "";
      if (cur !== "done" && cur !== "failed" && cur !== "cancelled") {
        setStatus(stopRequested ? "cancelled" : "failed");
      }
    }
  }

  // ---- elapsed counter (deci-second resolution) -------------------
  //
  // The clock is anchored to the *real job start* (server-provided
  // ``data-started-at-ms`` epoch on the pane) rather than to page-load
  // time, so a browser refresh mid-run resumes the counter instead of
  // restarting it from zero. We correct for client/server wall-clock
  // skew using ``data-server-now-ms`` (the server's epoch at render
  // time) so a mis-set client clock doesn't distort the value. When
  // the job hasn't started yet (queued) we render an em-dash; when it
  // has already ended we freeze at (ended - started).
  //
  // These anchors are mutable so the reader can back-fill them from the
  // streamed ``instance.started`` / ``instance.finished`` event ``ts``
  // when the page was opened *before* the worker recorded
  // ``started_at`` / ``ended_at``. Without this the counter shows an
  // em-dash until the operator refreshes the page.
  let startedAtEpochMs = numAttr(pane, "data-started-at-ms", 0);
  let endedAtEpochMs = numAttr(pane, "data-ended-at-ms", 0);
  const serverNowEpochMs = numAttr(pane, "data-server-now-ms", 0);
  // Skew = (client epoch at mount) - (server epoch at render). Subtract
  // it from the client clock so ``clientNow - skew ≈ serverNow``.
  const clientSkewMs = serverNowEpochMs > 0 ? Date.now() - serverNowEpochMs : 0;
  // The job status at render time. A *terminal* status (done / failed /
  // cancelled) means nothing is actually running, so the clock must be
  // frozen — never live-ticking. Previously the clock only froze when
  // BOTH ``started_at`` and ``ended_at`` were present; a terminal run
  // that never recorded ``ended_at`` (orphaned worker, crash-recovery)
  // would tick ``Date.now() - started`` forever, growing larger every
  // time the page was opened even though nothing was running.
  const TERMINAL_STATUSES = { done: 1, failed: 1, cancelled: 1 };
  const initialStatus = (pane.getAttribute("data-status") || "").toLowerCase();
  const startedTerminal = TERMINAL_STATUSES[initialStatus] === 1;
  // Seed the transport glyph from the server-provided status. When the
  // run is already terminal at load (done / failed / *cancelled*), force
  // the ▶ Play (rerun) shape up front and keep it there. This is the
  // authoritative signal: a cancelled worker that was SIGKILLed before
  // it could emit ``instance.finished`` leaves an event log that ends on
  // a stale ``instance.started``; without this seed the reader would
  // replay that ``instance.started`` and flip the glyph back to a ■ Stop
  // square, and — because ``page.finished`` never turns true — no
  // ``onFinished`` ever fires to correct it, leaving the button stuck as
  // Stop on a run the server already knows is cancelled.
  if (startedTerminal) {
    flipPrompt(initialStatus);
  }
  let frozenTotalMs =
    startedAtEpochMs > 0 && endedAtEpochMs > 0
      ? Math.max(0, endedAtEpochMs - startedAtEpochMs)
      : null;
  // Terminal on load with no explicit end anchor: freeze to a fixed
  // value (best available end minus start) instead of ticking live.
  if (frozenTotalMs == null && startedTerminal && startedAtEpochMs > 0) {
    const endAnchor =
      endedAtEpochMs > 0
        ? endedAtEpochMs
        : serverNowEpochMs > 0
          ? serverNowEpochMs
          : Date.now() - clientSkewMs;
    frozenTotalMs = Math.max(0, endAnchor - startedAtEpochMs);
  }
  let elapsedRaf = null;
  let elapsedLastText = "";
  let totalLastText = "";
  let currentLastText = "";
  // When the run is already terminal at load, stop the rAF loop after a
  // single paint so the frozen value is shown once and never animates.
  let finished = startedTerminal;

  // Back-fill the start anchor from an ``instance.started`` (or the
  // first ``table.started``) event ``ts`` when we don't already have
  // one. The event ``ts`` is server-UTC epoch, so it needs no skew
  // correction relative to itself — but ``tickElapsed`` compares it
  // against ``Date.now() - clientSkewMs`` (server-aligned), which is
  // exactly right.
  function backfillStartFromEvent(rec) {
    if (startedAtEpochMs > 0) return;
    const ms = parseTsMs(rec && rec.ts);
    if (ms != null && ms > 0) startedAtEpochMs = ms;
  }

  // Back-fill (and freeze) the end anchor from ``instance.finished``.
  function backfillEndFromEvent(rec) {
    if (endedAtEpochMs > 0) return;
    const ms = parseTsMs(rec && rec.ts);
    if (ms != null && ms > 0) {
      endedAtEpochMs = ms;
      if (startedAtEpochMs > 0) {
        frozenTotalMs = Math.max(0, endedAtEpochMs - startedAtEpochMs);
      }
    }
  }

  function fmtDeci(ms) {
    const totalMs = Math.max(0, Math.floor(ms));
    const secFloor = Math.floor(totalMs / 1000);
    const hh = Math.floor(secFloor / 3600);
    const mm = Math.floor((secFloor % 3600) / 60);
    const ss = secFloor % 60;
    const tenths = Math.floor((totalMs % 1000) / 100);
    if (hh > 0) {
      return (
        hh + "h" +
        String(mm).padStart(2, "0") + "m" +
        String(ss).padStart(2, "0") + "s"
      );
    }
    if (mm > 0) {
      return mm + "m" + String(ss).padStart(2, "0") + "." + tenths + "s";
    }
    return ss + "." + tenths + "s";
  }

  function fmtClock(ms) {
    // ovid-style MM:SS.s per-job timer (tenths of a second).
    const totalMs = Math.max(0, Math.floor(ms));
    const secFloor = Math.floor(totalMs / 1000);
    const mm = Math.floor(secFloor / 60);
    const ss = secFloor % 60;
    const tenths = Math.floor((totalMs % 1000) / 100);
    return (
      String(mm).padStart(2, "0") + ":" +
      String(ss).padStart(2, "0") + "." + tenths
    );
  }

  function paintElapsed(now) {
    // ``now`` is a ``performance.now()`` high-res timestamp (rAF) or a
    // fallback for the one-shot terminal paint; for the wall-clock
    // elapsed we use ``Date.now()`` corrected for server skew and
    // anchored to the job's real start epoch.
    let total;
    if (frozenTotalMs != null) {
      total = frozenTotalMs;
    } else if (startedAtEpochMs > 0 && !finished) {
      // Only tick live while the run is genuinely in-flight. A finished
      // run with no frozen total falls through to the em-dash rather
      // than counting up forever.
      total = Date.now() - clientSkewMs - startedAtEpochMs;
    } else {
      total = null; // job not started yet (or finished with no anchors)
    }
    const totalText = total == null ? "—" : fmtDeci(total);
    if (elapsedEl) {
      if (totalText !== elapsedLastText) {
        elapsedEl.textContent = totalText;
        elapsedLastText = totalText;
      }
    }
    // Footer total-elapsed (ovid parity — "Total Elapsed Time:").
    if (totalEl) {
      if (totalText !== totalLastText) {
        totalEl.textContent = totalText;
        totalLastText = totalText;
      }
      // Recolour the frozen clock when the run has stopped so the
      // operator can see at a glance that the timer is no longer
      // ticking. Live (in-flight) => amber; terminal => outcome colour
      // (done green / failed red / cancelled muted). Format is
      // unchanged — colour alone signals the stop.
      const stopped = frozenTotalMs != null || finished;
      if (stopped) {
        const finalStatus = promptLabel ? promptLabel.dataset.status || "" : "";
        if (!totalEl.classList.contains("is-stopped")) {
          totalEl.classList.add("is-stopped");
        }
        if (totalEl.dataset.finalStatus !== finalStatus) {
          totalEl.dataset.finalStatus = finalStatus;
        }
      } else if (totalEl.classList.contains("is-stopped")) {
        totalEl.classList.remove("is-stopped");
        delete totalEl.dataset.finalStatus;
      }
    }
    // Live per-table timer: animate while a table is active, freeze
    // to the last completed table's duration otherwise.
    if (currentEl) {
      let txt;
      if (activeTable && !finished) {
        txt =
          (activeTable.name ? activeTable.name + " " : "") +
          fmtClock((now || 0) - activeTable.startedMs);
      } else {
        txt = frozenCurrentText || "—";
      }
      if (txt !== currentLastText) {
        currentEl.textContent = txt;
        currentLastText = txt;
      }
    }
  }

  function tickElapsed(now) {
    if (finished) return;
    paintElapsed(now);
    elapsedRaf = requestAnimationFrame(tickElapsed);
  }
  // Always paint once so a run that is already terminal at load shows
  // its frozen elapsed value. Only drive the animation loop when the
  // run is still live.
  paintElapsed(typeof performance !== "undefined" ? performance.now() : 0);
  if (!finished) {
    elapsedRaf = requestAnimationFrame(tickElapsed);
  }

  // ---- terminal-state broadcast -----------------------------------
  //
  // When the run reaches a terminal state (or the ``job-status``
  // badge might have changed for another reason), we fire a
  // ``ovid2:job-status-nudge`` event on ``window`` so the HTMX
  // wrapper on the run-detail page can trigger an immediate
  // re-fetch of ``/partials/runs/{id}/status`` instead of waiting
  // for its next poll.
  function nudgeJobStatus(reason) {
    try {
      window.dispatchEvent(
        new CustomEvent("ovid2:job-status-nudge", {
          detail: { instanceId, reason },
        }),
      );
    } catch (e) {
      /* ignore — older browsers won't fire the event */
    }
  }

  // ---- reader -----------------------------------------------------
  const reader = createEventReader({
    instanceId,
    pollMs,
    limit: 200,
    onEvents(events) {
      // Share the raw batch with an external consumer (the per-table
      // panel) before we turn events into terminal lines.
      if (onEventsConsumer) {
        try {
          onEventsConsumer(events);
        } catch (e) {
          /* never let the panel break the terminal */
        }
      }
      const rendered = [];
      for (const rec of events) {
        applyMetaFromEvent(rec);
        applyStatusFromEvent(rec);
        // Drive the ovid-style live per-table timer.
        const k = rec && rec.kind;
        if (k === TERMINAL_KINDS.INSTANCE_STARTED) {
          // Back-fill the elapsed-clock start anchor so the counter
          // begins ticking without a page refresh when the page was
          // opened while the job was still queued.
          backfillStartFromEvent(rec);
        } else if (k === TERMINAL_KINDS.INSTANCE_FINISHED) {
          backfillEndFromEvent(rec);
        }
        if (k === TERMINAL_KINDS.TABLE_STARTED) {
          // Also a safety-net start anchor: some runs never emit a
          // distinct ``instance.started`` before the first table.
          backfillStartFromEvent(rec);
          beginTable(rec.table);
        } else if (
          k === TERMINAL_KINDS.TABLE_COMPLETED ||
          k === TERMINAL_KINDS.TABLE_FAILED
        ) {
          endTable(rec.table, rec.elapsed_ms);
        }
        const line = formatEvent(rec);
        if (line) rendered.push(line);
        // Terminal-state signals get an immediate nudge to the
        // job-status HTMX partial so the badge flips within one
        // network round-trip instead of within one poll interval.
        if (
          k === TERMINAL_KINDS.INSTANCE_FINISHED ||
          k === TERMINAL_KINDS.BATCH_COMPLETED ||
          k === TERMINAL_KINDS.BATCH_FAILED ||
          k === TERMINAL_KINDS.TABLE_FAILED
        ) {
          nudgeJobStatus(k);
        }
      }
      if (rendered.length) render(rendered);
    },
    onFinished() {
      // Freeze the total-elapsed clock. If no end anchor arrived (no
      // ``instance.finished`` event), snapshot the live value now so
      // the footer shows a stable duration instead of an em-dash.
      if (frozenTotalMs == null && startedAtEpochMs > 0) {
        frozenTotalMs = Math.max(0, Date.now() - clientSkewMs - startedAtEpochMs);
      }
      finished = true;
      // Freeze the per-table timer if a table was still marked
      // active when the stream ended.
      if (activeTable) endTable(activeTable.name, null);
      if (currentEl && frozenCurrentText) {
        currentEl.textContent = frozenCurrentText;
      }
      // Paint the frozen value one last time — the rAF loop stops once
      // ``finished`` is set, so without this the footer keeps its
      // last live text instead of the final frozen duration.
      paintElapsed(typeof performance !== "undefined" ? performance.now() : 0);
      nudgeJobStatus("stream.finished");
      // The stream is over, so the run is no longer in-flight: make sure
      // the prompt cursor shows ▶ Play / rerun even if we never observed
      // an explicit terminal status event (e.g. a crash-recovered run).
      const pillStatus = promptLabel ? promptLabel.dataset.status : "";
      if (TERMINAL_STATUSES[pillStatus] !== 1) {
        flipPrompt("done");
      }
      // Freeze the cursor into a "done" state.
      const c = pane.querySelector(".term-cursor");
      if (c) c.style.animation = "none";
    },
  });

  pane.dataset.mounted = "1";

  function close() {
    reader.close();
    if (elapsedRaf != null) cancelAnimationFrame(elapsedRaf);
    finished = true;
    delete pane.dataset.mounted;
  }

  return { close };
}

/* ------------------------------------------------------------------ */
/* small helpers                                                      */
/* ------------------------------------------------------------------ */

function numAttr(el, name, fallback) {
  const raw = el.getAttribute(name);
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function getExtra(rec, key, fallback) {
  if (rec && rec.extra && key in rec.extra) return rec.extra[key];
  if (rec && key in rec) return rec[key];
  return fallback;
}

function escapeHtml(s) {
  const str = String(s == null ? "" : s);
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export default { id: "terminal", mount: mountTerminal };
