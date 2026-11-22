/*
 * Ovid2 — drives the header <ovid-mark> and the browser-tab favicon
 * from live job status.
 *
 * Status source, "page first, then global":
 *
 *   1. Run-detail page: the page already polls its job status partial
 *      (``#run-job-status`` → ``.job-status-marker[data-job-status]``)
 *      and its event stream (re-broadcast by live.js as
 *      ``ovid2:run-events``). We observe those — no extra request.
 *        queued / running → running (or the current stage:
 *                           read → extract, format…fix → transform,
 *                           write → load)
 *        a check.completed with exceptions → warning
 *        done → success · failed → error (sticky) · cancelled → idle
 *
 *   2. Every other page: poll ``GET /api/v1/jobs?status=running`` and
 *      ``?status=queued`` (limit 1) every 5 s while the tab is visible,
 *      only when the header carries ``data-poll-runs="1"`` (the user
 *      holds ``view_runs``). Any job → running, else idle.
 *
 * The favicon is re-drawn as an inline SVG data URI in the state's
 * colour (``--ovid-fav-*`` CSS variables, defaults in :root). While a
 * job runs it steps O → I → D once a second (browsers do not animate
 * SVG favicons); under prefers-reduced-motion it only changes colour.
 *
 * No new transport, no new endpoint. Classic script (``<script defer>``).
 */

(function () {
  "use strict";

  var POLL_MS = 5000;
  var STEP_MS = 1000;

  var STAGE_MAP = {
    read: "extract",
    format: "transform",
    deduplicate: "transform",
    transform: "transform",
    map: "transform",
    fix: "transform",
    write: "load"
  };

  var FAV_DEFAULTS = {
    idle: "#537FD0",
    extract: "#487CE0",
    transform: "#916BD7",
    load: "#0C8C9C",
    running: "#487CE0",
    sync: "#7D8DAA",
    success: "#199059",
    warning: "#B26F00",
    error: "#D7554C"
  };

  var mark = null;
  var pageMode = false;
  var pageStage = null;
  var pageWarn = false;
  var lastStatus = null;
  var pollTimer = 0;
  var stepTimer = 0;
  var stepIdx = 0;
  var faviconLinks = [];
  var reducedMotion = false;

  try {
    reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch (e) { reducedMotion = false; }

  /* ---------- state mapping ---------- */

  function stageFromEvent(rec) {
    if (!rec) return null;
    var st = rec.stage ? String(rec.stage).toLowerCase() : "";
    if (STAGE_MAP[st]) return STAGE_MAP[st];
    // engine.metrics carries a free-form engine stage string; DuckDB
    // labels its writer "write <table>".
    var cur = rec.current_stage ? String(rec.current_stage).toLowerCase() : "";
    if (cur.indexOf("write ") === 0 || cur.indexOf("export ") === 0) return "load";
    return null;
  }

  function stateFromJobStatus(status) {
    switch (status) {
      case "queued":
      case "running":
      case "stopping":
        if (pageWarn) return "warning";
        return pageStage || "running";
      case "done": return pageWarn ? "warning" : "success";
      case "failed": return "error";
      default: return "idle";
    }
  }

  function setState(state) {
    if (!mark) return;
    if (mark.getAttribute("state") !== state) mark.setAttribute("state", state);
    paintFavicon(state);
  }

  /* ---------- favicon ---------- */

  function cssVar(name) {
    try {
      return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    } catch (e) { return ""; }
  }

  function favColor(state) {
    return cssVar("--ovid-fav-" + state) || FAV_DEFAULTS[state] || FAV_DEFAULTS.idle;
  }

  function setFavicon(svg) {
    var href = "data:image/svg+xml," + encodeURIComponent(svg);
    if (!faviconLinks.length) {
      var link = document.createElement("link");
      link.rel = "icon";
      link.type = "image/svg+xml";
      document.head.appendChild(link);
      faviconLinks = [link];
    }
    faviconLinks.forEach(function (link) {
      if (link.getAttribute("href") !== href) link.setAttribute("href", href);
    });
  }

  function paintFavicon(state) {
    var Mark = window.customElements && window.customElements.get("ovid-mark");
    if (!Mark || typeof Mark.faviconSvg !== "function") return;
    clearInterval(stepTimer);
    stepTimer = 0;
    if (state === "idle") {
      setFavicon(Mark.faviconSvg({
        rest: [cssVar("--ovid-rest-1"), cssVar("--ovid-rest-2"), cssVar("--ovid-rest-3")]
      }));
      return;
    }
    if (state === "running" && !reducedMotion) {
      var leaves = ["o", "i", "d"];
      var colours = {
        o: favColor("extract"),
        i: favColor("transform"),
        d: favColor("load")
      };
      var draw = function () {
        var focus = leaves[stepIdx % 3];
        stepIdx += 1;
        setFavicon(Mark.faviconSvg({
          color: colours[focus], focus: focus, leafColors: colours, stemColor: colours[focus]
        }));
      };
      draw();
      stepTimer = setInterval(draw, STEP_MS);
      return;
    }
    var focusLeaf = { extract: "o", transform: "i", load: "d" }[state] || null;
    setFavicon(Mark.faviconSvg({ color: favColor(state), focus: reducedMotion ? null : focusLeaf }));
  }

  /* ---------- page mode (run detail) ---------- */

  function readPageStatus() {
    var marker = document.querySelector("#run-job-status .job-status-marker[data-job-status]");
    if (!marker) return null;
    return (marker.getAttribute("data-job-status") || "").toLowerCase();
  }

  function refreshFromPage() {
    var status = readPageStatus();
    if (!status) return;
    var changed = status !== lastStatus;
    if (changed) {
      // Leaving a terminal state (rerun) clears the stage / warning memory.
      if (lastStatus && ["done", "failed", "cancelled"].indexOf(lastStatus) !== -1) {
        pageStage = null;
        pageWarn = false;
      }
      lastStatus = status;
    }
    var next = stateFromJobStatus(status);
    // success is a one-shot: once the mark has auto-returned to idle, a
    // later re-render of the same "done" partial must not replay it.
    if (next === "success" && !changed) return;
    setState(next);
  }

  function onRunEvents(ev) {
    var events = (ev && ev.detail && ev.detail.events) || [];
    events.forEach(function (rec) {
      if (!rec) return;
      var st = stageFromEvent(rec);
      if (st) pageStage = st;
      if (rec.kind === "check.completed" && Number(rec.exception_count || 0) > 0) pageWarn = true;
      if (rec.kind === "instance.finished") pageStage = null;
    });
    refreshFromPage();
  }

  function startPageMode() {
    pageMode = true;
    refreshFromPage();
    var host = document.getElementById("run-job-status");
    if (host && typeof MutationObserver === "function") {
      new MutationObserver(refreshFromPage).observe(host, {
        childList: true, subtree: true, attributes: true, attributeFilter: ["data-job-status"]
      });
    }
    window.addEventListener("ovid2:run-events", onRunEvents);
    window.addEventListener("ovid2:job-status-nudge", function () {
      setTimeout(refreshFromPage, 300);
    });
  }

  /* ---------- global mode (every other page) ---------- */

  function countJobs(status) {
    return fetch("/api/v1/jobs?limit=1&status=" + status, {
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (body) {
      return Number((body && body.total) || 0);
    });
  }

  function pollGlobal() {
    clearTimeout(pollTimer);
    if (document.hidden) return;
    Promise.all([countJobs("running"), countJobs("queued")]).then(function (n) {
      setState(n[0] + n[1] > 0 ? "running" : "idle");
    }).catch(function () {
      /* keep the last state; a 401/403 or network blip is not a run state */
    }).then(function () {
      pollTimer = setTimeout(pollGlobal, POLL_MS);
    });
  }

  function startGlobalMode() {
    pollGlobal();
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) pollGlobal();
      else clearTimeout(pollTimer);
    });
  }

  /* ---------- boot ---------- */

  function init() {
    mark = document.getElementById("brand-sprout");
    if (!mark) return;
    faviconLinks = Array.prototype.slice.call(
      document.querySelectorAll('link[rel="icon"][type="image/svg+xml"]')
    );
    // Re-tint the favicon when the theme changes.
    document.addEventListener("ovid2:themechange", function () {
      paintFavicon(mark.getAttribute("state") || "idle");
    });
    // success auto-returns to idle inside the component; mirror it.
    mark.addEventListener("ovid-state-change", function (ev) {
      if (ev.detail && ev.detail.to === "idle") paintFavicon("idle");
    });

    if (readPageStatus() !== null) {
      startPageMode();
    } else if (mark.getAttribute("data-poll-runs") === "1") {
      startGlobalMode();
    } else {
      setState("idle");
    }
  }

  function ready() {
    if (window.customElements && typeof window.customElements.whenDefined === "function") {
      window.customElements.whenDefined("ovid-mark").then(init);
    } else {
      init();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", ready);
  } else {
    ready();
  }

  // Exposed for the dev demo page and tests.
  window.Ovid2MarkStatus = {
    stateFromJobStatus: stateFromJobStatus,
    stageMap: STAGE_MAP,
    isPageMode: function () { return pageMode; }
  };
})();
