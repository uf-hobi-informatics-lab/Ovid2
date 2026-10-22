/*
 * Ovid2 shared run-console split panel.
 *
 * This is the single implementation behind *every* pipeline type's live
 * run console (see _terminal_pane.html + live.js). It renders the
 * split lower region — a left summary list of the run's *items* and a
 * right free-flowing detail list — and is config-driven so the same
 * behaviour, layout, colours, status dots, live timer and follow-run
 * toggle are universal across pipeline types:
 *
 *   - ETL / analysis runs: one *item* per table (``table.*`` events).
 *   - health / dq_checks runs: one *item* per data-health check
 *     (``check.*`` events), grouped by CDC part.
 *   - any future pipeline type: drop in a small config (see the wrappers
 *     under display/tables/panel.js and display/checks/panel.js).
 *
 * Left column: one clickable row per item, showing the item name and a
 *   small colour-coded *status dot* (queued / running / done / failed /
 *   exception) rather than a status word. The dot colour encodes state
 *   (a soft, pale, static indicator like the run button — no animation).
 *   The row also carries a ``status-<status>`` class so its
 *   background/text tint can encode state. The left list is a
 *   *controller* — clicking a row scrolls the right pane to that item's
 *   section and highlights it. Rows may be grouped (checks group by
 *   part; tables are flat) via the optional ``group`` config.
 *
 *   When the config supplies a roster (explicit list or a plan
 *   endpoint), the full manifest is *pre-seeded* on mount so every item
 *   the run will touch appears immediately as ``queued`` before any
 *   event arrives.
 * Right column: a *free list* of every *activated* item's detail — name,
 *   a live/frozen MM:SS.s clock (only meaningful for timed items; blank
 *   otherwise), an optional error banner, and its event log — rendered
 *   continuously as items start.
 *
 * There is no extra network traffic for events: the terminal mount tails
 * ``/api/v1/runs/{id}/events`` and forwards every batch here via its
 * ``onEvents`` hook (see mount.js). We keep an in-memory aggregate per
 * item and re-render the affected DOM on each batch.
 *
 * Timestamps rendered in the detail list are converted to the operator's
 * configured display timezone (``data-display-tz`` on the console pane)
 * via the shared tz helper.
 */

import {
  readDisplayTz,
  formatClockTs,
  parseTsMs,
} from "/static/display/tz.js";

const CLOCK_RAF = typeof requestAnimationFrame === "function";
const AUTOSCROLL_SLOP_PX = 12;

/**
 * Mount the shared run panel.
 *
 * @param {object} config - per-pipeline-type wiring.
 * @param {string}   config.eventPrefix  - ``"table."`` or ``"check."``.
 * @param {(ev:object)=>(string|undefined)} config.itemKey - extract the
 *   item id/name from an event (returns falsy to ignore the event).
 * @param {object}   config.dom - DOM id map:
 *   ``{ summary, detail, detailName, followToggle }``.
 * @param {string}   [config.rowClass="run-row"] - base class for summary
 *   rows.
 * @param {string}   [config.dotClass="run-row-dot"] - base class for the
 *   status dot.
 * @param {(status:string)=>string} [config.statusLabel] - map internal
 *   status -> human/aria label.
 * @param {(item:object)=>string} [config.detailBody] - build the
 *   right-pane detail body HTML for an item (defaults to the table event
 *   log renderer).
 * @param {boolean}  [config.timed=true] - show the live MM:SS.s clock in
 *   the detail header (tables) vs. omit it (checks have no per-item
 *   duration).
 * @param {(ev:object,item:object)=>void} [config.apply] - fold an event
 *   into the item aggregate; must set ``item.status`` and may set
 *   ``item.error`` / ``item.elapsedMs``. Defaults to the table folder.
 * @param {(item:object)=>string} [config.group] - group key for a row
 *   (checks -> part). When omitted, rows render as a flat list.
 * @param {(key:string)=>string} [config.groupLabel] - human label for a
 *   group key.
 * @param {()=>Array<string|{name:string,displayName?:string}>|Promise<...>} [config.roster]
 *   - resolve the pre-seed roster (queued items shown up front). Each
 *   entry is a plain item name or a ``{ name, displayName }`` record.
 *   Omit for purely reactive panels.
 * @param {HTMLElement} [config.paneEl] - the ``.terminal-pane`` (for
 *   ``data-display-tz`` etc.); defaults to the summary element's closest.
 * @returns {{ handleEvents:(events:Array<object>)=>void, close:()=>void }|null}
 */
export function mountRunPanel(config) {
  const cfg = config || {};
  const dom = cfg.dom || {};
  const summaryEl =
    cfg.summaryEl || document.getElementById(dom.summary || "");
  const detailEl = cfg.detailEl || document.getElementById(dom.detail || "");
  const detailNameEl =
    cfg.detailNameEl ||
    (dom.detailName ? document.getElementById(dom.detailName) : null);
  if (!summaryEl || !detailEl) return null;

  const rowClass = cfg.rowClass || "run-row";
  const dotClass = cfg.dotClass || "run-row-dot";
  const timed = cfg.timed !== false;
  const statusLabel =
    typeof cfg.statusLabel === "function" ? cfg.statusLabel : (s) => s || "";
  const applyEvent =
    typeof cfg.apply === "function" ? cfg.apply : defaultApply;
  const detailBody =
    typeof cfg.detailBody === "function"
      ? cfg.detailBody
      : (item) => defaultDetailBody(item, tz);
  const groupOf = typeof cfg.group === "function" ? cfg.group : null;
  const groupLabel =
    typeof cfg.groupLabel === "function" ? cfg.groupLabel : (k) => k || "Other";

  const paneEl =
    cfg.paneEl || summaryEl.closest(".terminal-pane") || null;
  const tz = cfg.tz || readDisplayTz(paneEl);

  // name -> aggregate record.
  const items = new Map();
  const order = []; // insertion order of item names
  let selected = null;
  // Whether the operator has manually clicked a row. Until they do, the
  // right pane auto-scrolls to the newest running item's section.
  let userPinned = false;
  let rafId = null;

  // The detail pane scroll is driven by auto-follow unless the operator
  // scrolls it themselves. Programmatic scrolls (our own
  // ``scrollTop = scrollHeight`` in renderDetail) are suppressed via the
  // ``detailSelfScroll`` guard so they never turn following off.
  let detailFollow = true;
  let detailSelfScroll = false;
  detailEl.addEventListener("scroll", () => {
    if (detailSelfScroll) return;
    const atBottom =
      detailEl.scrollTop + detailEl.clientHeight >=
      detailEl.scrollHeight - AUTOSCROLL_SLOP_PX;
    detailFollow = atBottom;
    // Scrolling the detail pane back to the bottom re-arms auto-follow
    // even after the operator pinned a row earlier — otherwise a single
    // inspection click would freeze the pane for the rest of the run.
    if (atBottom) userPinned = false;
  });

  // The summary list follows the run: when the roster is taller than the
  // pane it auto-scrolls to keep the currently-running item in view. The
  // follow toggle button (bottom-right of the left column) is universal
  // across pipeline types; it is optional only in the sense that the DOM
  // element may be absent on an older template.
  const followToggle = dom.followToggle
    ? document.getElementById(dom.followToggle)
    : null;
  let summaryFollow = true;
  let summarySelfScroll = false;
  summaryEl.addEventListener("scroll", () => {
    if (summarySelfScroll) return;
    summaryFollow =
      summaryEl.scrollTop + summaryEl.clientHeight >=
      summaryEl.scrollHeight - AUTOSCROLL_SLOP_PX;
    reflectFollowToggle();
  });

  function reflectFollowToggle() {
    if (!followToggle) return;
    followToggle.setAttribute("aria-pressed", summaryFollow ? "true" : "false");
  }

  if (followToggle) {
    followToggle.addEventListener("click", () => {
      summaryFollow = !summaryFollow;
      reflectFollowToggle();
      if (summaryFollow) renderSummary();
    });
    reflectFollowToggle();
  }

  function ensure(name) {
    const key = String(name || "");
    if (!key) return null;
    let rec = items.get(key);
    if (!rec) {
      rec = {
        name: key,
        status: "pending",
        startedMs: null,
        startedTsMs: null,
        elapsedMs: null,
        error: "",
        group: "",
        events: [],
      };
      items.set(key, rec);
      order.push(key);
    }
    return rec;
  }

  /* -------- event folding -------- */

  // The default event folder: table-shaped events. ``check.*`` panels
  // pass their own ``apply`` (see checks wrapper). Both mutate the same
  // aggregate shape.
  function defaultApply(ev, rec) {
    const kind = ev.kind || "";
    if (kind === "table.started") {
      rec.status = "running";
      rec.startedMs = performance.now();
      rec.startedTsMs = parseTsMs(ev.ts);
      if (!userPinned) {
        selected = rec.name;
        detailFollow = true;
      }
    } else if (kind === "table.completed") {
      rec.status = "completed";
      const el = resolveElapsed(rec, ev);
      if (el != null) rec.elapsedMs = el;
      rec.startedMs = null;
    } else if (kind === "table.failed") {
      rec.status = "failed";
      rec.error = String(extra(ev, "error", "") || "");
      const el = resolveElapsed(rec, ev);
      if (el != null) rec.elapsedMs = el;
      rec.startedMs = null;
    } else if (kind === "error" || kind === "fix.failed") {
      rec.status = "failed";
      const fix = String(extra(ev, "fix", "") || "");
      const msg = String(
        extra(ev, "error", "") || extra(ev, "message", "") || "",
      );
      rec.error = fix && msg ? fix + ": " + msg : msg || fix;
      const el = resolveElapsed(rec, ev);
      if (el != null) rec.elapsedMs = el;
      rec.startedMs = null;
    }
    // stage / fix / other events only grow the detail log.
  }

  function handleEvents(events) {
    if (!Array.isArray(events) || !events.length) return;
    let summaryDirty = false;
    let detailDirty = false;
    for (const ev of events) {
      const name = cfg.itemKey ? cfg.itemKey(ev) : itemKeyFromPrefix(ev);
      if (!name) continue;
      const rec = ensure(name);
      if (!rec) continue;
      const before = rec.status;
      rec.events.push(ev);
      applyEvent(ev, rec);
      if (groupOf) {
        const g = groupOf(ev);
        if (g) rec.group = g;
      }
      if (rec.status !== before) summaryDirty = true;
      // Any accepted event grows the detail; the started event also
      // moves selection.
      detailDirty = true;
      summaryDirty = summaryDirty || rec.status === "running";
    }
    if (summaryDirty) renderSummary();
    if (detailDirty) renderDetail();
  }

  // Fallback item-key extractor keyed off the config's event prefix.
  function itemKeyFromPrefix(ev) {
    return ev && (ev.table || (ev.extra && ev.extra.table));
  }

  /* -------- timer -------- */

  function fmtClock(ms) {
    const totalMs = Math.max(0, Math.floor(ms));
    const secFloor = Math.floor(totalMs / 1000);
    const mm = Math.floor(secFloor / 60);
    const ss = secFloor % 60;
    const tenths = Math.floor((totalMs % 1000) / 100);
    return (
      String(mm).padStart(2, "0") +
      ":" +
      String(ss).padStart(2, "0") +
      "." +
      tenths
    );
  }

  function timerText(rec, now) {
    if (!timed) return "";
    if (rec.status === "running" && rec.startedMs != null) {
      return fmtClock(now - rec.startedMs);
    }
    if (rec.elapsedMs != null) return fmtClock(rec.elapsedMs);
    return "";
  }

  function resolveElapsed(rec, ev) {
    const server = Number(extra(ev, "elapsed_ms", NaN));
    if (Number.isFinite(server) && server >= 0) return server;
    const endTsMs = parseTsMs(ev && ev.ts);
    if (rec.startedTsMs != null && endTsMs != null && endTsMs >= rec.startedTsMs) {
      return endTsMs - rec.startedTsMs;
    }
    if (rec.startedMs != null) return performance.now() - rec.startedMs;
    return null;
  }

  /* -------- scroll helpers -------- */

  // Scroll ``container`` so ``row`` is fully visible, advancing *down*
  // when the row sits below the fold. ``scrollIntoView({block:"nearest"})``
  // is unreliable here: it does the *minimum* move, so a running row that
  // is only partly below the fold (or a container whose own overflow the
  // browser resolves against the page rather than the pane) may not
  // advance at all. Computing the target scrollTop against the row's
  // offset within the scroll container is deterministic.
  function scrollRowIntoView(container, row) {
    const cTop = container.scrollTop;
    const cBottom = cTop + container.clientHeight;
    const rTop = row.offsetTop;
    const rBottom = rTop + row.offsetHeight;
    if (rBottom > cBottom) {
      // Row is below the fold — bring its bottom to the pane's bottom.
      container.scrollTop = rBottom - container.clientHeight;
    } else if (rTop < cTop) {
      // Row is above the fold — bring its top to the pane's top.
      container.scrollTop = rTop;
    }
  }

  // Clear a self-scroll guard after the browser has dispatched the
  // scroll event our programmatic scroll triggered. ``requestAnimationFrame``
  // fires after the scroll event settles (more reliable than
  // ``setTimeout(fn, 0)``, which can race the scroll dispatch and let a
  // programmatic scroll flip ``*Follow`` off).
  function clearSelfScroll(fn) {
    if (CLOCK_RAF) {
      requestAnimationFrame(() => requestAnimationFrame(fn));
    } else {
      setTimeout(fn, 32);
    }
  }

  /* -------- summary (left) -------- */

  function buildRow(rec) {
    const row = document.createElement("button");
    row.type = "button";
    row.className =
      rowClass + " status-" + rec.status + (selected === rec.name ? " selected" : "");
    row.setAttribute("data-item", rec.name);
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", selected === rec.name ? "true" : "false");

    const dotEl = document.createElement("span");
    dotEl.className = dotClass + " status-" + rec.status;
    const label = statusLabel(rec.status);
    dotEl.setAttribute("role", "img");
    dotEl.setAttribute("aria-label", label);
    dotEl.title = label;

    const nameEl = document.createElement("span");
    nameEl.className = "run-row-name mono";
    nameEl.textContent = rec.displayName || rec.name;

    // Dot leads the row (consistent across every pipeline type).
    row.appendChild(dotEl);
    row.appendChild(nameEl);
    row.addEventListener("click", () => select(rec.name));
    return row;
  }

  function renderSummary() {
    if (!order.length) return;
    const prevScroll = summaryEl.scrollTop;
    const frag = document.createDocumentFragment();
    if (groupOf) {
      const byGroup = new Map();
      for (const name of order) {
        const rec = items.get(name);
        if (!rec) continue;
        const g = rec.group || "";
        if (!byGroup.has(g)) byGroup.set(g, []);
        byGroup.get(g).push(rec);
      }
      for (const [g, list] of byGroup) {
        const group = document.createElement("div");
        group.className = "run-group";
        const exc = list.reduce((n, c) => n + (c.status === "exception" ? 1 : 0), 0);
        const err = list.reduce((n, c) => n + (c.status === "failed" ? 1 : 0), 0);
        const head = document.createElement("div");
        head.className = "run-group-head small mono";
        head.innerHTML =
          escapeHtml(groupLabel(g)) +
          ' <span class="run-group-counts">(' +
          list.length +
          (exc ? ", " + exc + " exc" : "") +
          (err ? ", " + err + " err" : "") +
          ")</span>";
        group.appendChild(head);
        for (const rec of list) group.appendChild(buildRow(rec));
        frag.appendChild(group);
      }
    } else {
      for (const name of order) {
        const rec = items.get(name);
        if (rec) frag.appendChild(buildRow(rec));
      }
    }
    summaryEl.innerHTML = "";
    summaryEl.appendChild(frag);
    if (summaryFollow) {
      // Track the *frontier* of the run: when several rows are running
      // at once (lazy Spark starts tables near-simultaneously) follow
      // the last one so the list keeps advancing down as work fans out.
      const runningRows = summaryEl.querySelectorAll(
        "." + rowClass + ".status-running",
      );
      const target = runningRows.length
        ? runningRows[runningRows.length - 1]
        : null;
      if (target) {
        // Guard the programmatic scroll so the scroll listener does not
        // mistake it for an operator scroll and turn following off.
        summarySelfScroll = true;
        scrollRowIntoView(summaryEl, target);
        clearSelfScroll(() => {
          summarySelfScroll = false;
        });
      } else {
        // Nothing running yet (all queued) or everything done: leave the
        // scroll position where it was so the roster doesn't jump.
        summaryEl.scrollTop = prevScroll;
      }
    } else {
      summaryEl.scrollTop = prevScroll;
    }
  }

  /* -------- detail (right) -------- */

  function buildSection(rec) {
    const section = document.createElement("div");
    section.className =
      "run-detail-section status-" +
      rec.status +
      (selected === rec.name ? " selected" : "");
    section.setAttribute("data-item", rec.name);

    const head = document.createElement("div");
    head.className = "run-detail-head";

    const nameEl = document.createElement("span");
    nameEl.className = "run-detail-name mono";
    nameEl.textContent = rec.displayName || rec.name;
    head.appendChild(nameEl);

    if (timed) {
      const dur = document.createElement("span");
      dur.className = "run-detail-dur mono small";
      dur.textContent = timerText(rec, performance.now());
      head.appendChild(dur);
    }
    section.appendChild(head);

    if (rec.error) {
      const errEl = document.createElement("div");
      errEl.className = "flash error mono small";
      errEl.textContent = rec.error;
      section.appendChild(errEl);
    }

    const bodyHtml = detailBody(rec);
    if (bodyHtml) {
      const body = document.createElement("div");
      body.className = "run-detail-body";
      body.innerHTML = bodyHtml;
      section.appendChild(body);
    }
    return section;
  }

  function renderDetail() {
    if (detailNameEl) detailNameEl.textContent = selected ? selected : "";
    if (!order.length) return;
    const frag = document.createDocumentFragment();
    let shown = 0;
    for (const name of order) {
      const rec = items.get(name);
      if (!rec) continue;
      // Still-queued items with no events don't get an empty section.
      if (rec.status === "pending" && !rec.events.length) continue;
      frag.appendChild(buildSection(rec));
      shown += 1;
    }
    if (!shown) return;
    detailEl.innerHTML = "";
    detailEl.appendChild(frag);
    if (detailFollow && !userPinned) {
      detailSelfScroll = true;
      detailEl.scrollTop = detailEl.scrollHeight;
      clearSelfScroll(() => {
        detailSelfScroll = false;
      });
    } else if (userPinned && selected) {
      const section = detailEl.querySelector(
        '.run-detail-section[data-item="' + cssEscape(selected) + '"]',
      );
      if (section && typeof section.scrollIntoView === "function") {
        detailSelfScroll = true;
        section.scrollIntoView({ block: "nearest" });
        clearSelfScroll(() => {
          detailSelfScroll = false;
        });
      }
    }
  }

  function tickTimers(now) {
    if (timed) {
      for (const name of order) {
        const rec = items.get(name);
        if (!rec || rec.status !== "running") continue;
        const dEl = detailEl.querySelector(
          '.run-detail-section[data-item="' + cssEscape(name) + '"] .run-detail-dur',
        );
        if (dEl) dEl.textContent = timerText(rec, now);
      }
    }
    rafId = CLOCK_RAF ? requestAnimationFrame(tickTimers) : null;
  }

  function select(name) {
    userPinned = true;
    selected = name;
    renderSummary();
    highlightSection(name);
    const section = detailEl.querySelector(
      '.run-detail-section[data-item="' + cssEscape(name) + '"]',
    );
    if (section && typeof section.scrollIntoView === "function") {
      section.scrollIntoView({ block: "nearest" });
    }
  }

  function highlightSection(name) {
    const sections = detailEl.querySelectorAll(".run-detail-section");
    for (const s of sections) {
      s.classList.toggle("selected", s.getAttribute("data-item") === name);
    }
  }

  /* -------- roster pre-seed -------- */

  function seedRoster(names) {
    if (!Array.isArray(names) || !names.length) return;
    const planned = [];
    const seen = new Set();
    for (const raw of names) {
      // A roster entry may be a plain name (tables) or a record
      // ``{ name, displayName }`` (checks pre-seed the human name so the
      // row isn't a raw id until its first event arrives).
      const isRec = raw && typeof raw === "object";
      const key = String((isRec ? raw.name : raw) || "");
      if (!key || seen.has(key)) continue;
      seen.add(key);
      planned.push(key);
      const rec = ensure(key);
      if (rec && isRec && raw.displayName) rec.displayName = String(raw.displayName);
    }
    const extras = order.filter((n) => !seen.has(n));
    const next = planned.concat(extras);
    order.length = 0;
    for (const n of next) order.push(n);
    renderSummary();
    renderDetail();
  }

  function initRoster() {
    if (typeof cfg.roster !== "function") return;
    let out;
    try {
      out = cfg.roster();
    } catch (_e) {
      return;
    }
    if (out && typeof out.then === "function") {
      out.then((names) => seedRoster(names)).catch(() => {
        /* endpoint unavailable / forbidden — stay reactive */
      });
    } else if (Array.isArray(out)) {
      seedRoster(out);
    }
  }

  function close() {
    if (rafId != null && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(rafId);
    }
    rafId = null;
  }

  // Initial paint + start the running-item timer loop.
  renderSummary();
  renderDetail();
  initRoster();
  rafId = CLOCK_RAF ? requestAnimationFrame(tickTimers) : null;

  return { handleEvents, close };
}

/* ------------------------------------------------------------------ */
/* shared helpers                                                     */
/* ------------------------------------------------------------------ */

function extra(rec, key, fallback) {
  if (rec && rec.extra && key in rec.extra) return rec.extra[key];
  if (rec && key in rec) return rec[key];
  return fallback;
}

/**
 * Default right-pane body: the table-style event log. Each event renders
 * as a timestamped line; errors/warnings carry a status colour.
 *
 * @param {object} item
 * @param {string} tz
 * @returns {string} HTML
 */
function defaultDetailBody(item, tz) {
  const lines = [];
  for (const ev of item.events || []) {
    const line = eventDetailLine(ev);
    lines.push(
      '<div class="run-detail-line mono' +
        (line.error ? " is-error" : "") +
        (line.warning ? " is-warning" : "") +
        '"><span class="run-detail-ts">' +
        escapeHtml(formatClockTs(ev.ts || "", tz)) +
        '</span><span>' +
        escapeHtml(line.text) +
        "</span></div>",
    );
  }
  if (!lines.length) {
    return '<p class="run-detail-empty small mono">No events for this item yet.</p>';
  }
  return '<div class="run-detail-log">' + lines.join("") + "</div>";
}

function eventDetailLine(ev) {
  const kind = ev.kind || "event";
  const bits = [];
  if (ev.stage) bits.push(ev.stage);
  const fix = extra(ev, "fix", "");
  if (fix) bits.push(fix);
  const err = extra(ev, "error", "");
  const msg = extra(ev, "message", "");
  const ms = extra(ev, "elapsed_ms", "");
  let suffix = bits.length ? " " + bits.join(" · ") : "";
  if (ms !== "" && ms != null) suffix += " (" + ms + "ms)";
  if (err) suffix += " — " + err;
  else if (msg) suffix += " — " + msg;
  const isError = !!err || kind === "error" || kind === "fix.failed";
  const isWarning = !isError && kind === "warning";
  return { kind, text: kind + suffix, error: isError, warning: isWarning };
}

export function escapeHtml(s) {
  const str = String(s == null ? "" : s);
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function cssEscape(s) {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(s);
  }
  return String(s).replace(/["\\\]]/g, "\\$&");
}

/**
 * Fetch a pipeline's formatter-backed table roster from the run-plan
 * endpoint (the list ``--tables all`` expands to). Shared helper for the
 * tables wrapper's ``roster`` config.
 *
 * @param {string} pipeline
 * @returns {Promise<string[]>}
 */
export function fetchPlanTables(pipeline) {
  const url =
    "/api/v1/pipeline-packages/" + encodeURIComponent(pipeline) + "/plan";
  return fetch(url, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then((r) => {
      if (!r.ok) throw new Error("plan " + r.status);
      return r.json();
    })
    .then((plan) => {
      const rows = plan && Array.isArray(plan.tables) ? plan.tables : [];
      return rows
        .filter((t) => t && t.has_formatter)
        .map((t) => String(t.name || ""))
        .filter(Boolean);
    });
}

export default { mount: mountRunPanel };
