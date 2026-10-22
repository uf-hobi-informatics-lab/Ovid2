/*
 * Ovid2 timezone-aware time helpers for the live run console.
 *
 * Pipeline event ``ts`` values are stored (and streamed) as ISO-8601
 * *UTC* strings, e.g. ``2026-08-21T03:13:38.870967+00:00``. Server-
 * rendered timestamps go through the Jinja ``localtime`` filters
 * (see ovid2/web/templating.py) which honour the operator's configured
 * ``settings.ui.display_timezone``. The live console renders event ``ts``
 * values *client-side*, so the same timezone must be applied here rather
 * than showing raw UTC.
 *
 * The operator's IANA zone is handed to the browser on the console pane
 * via ``data-display-tz`` (see _terminal_pane.html); modules read it with
 * :func:`readDisplayTz` and format with :func:`formatClockTs`.
 *
 * Every function degrades gracefully — an unparseable timestamp, an
 * unknown zone, or a browser without ``Intl.DateTimeFormat`` all fall
 * back to a plain UTC ``HH:MM:SS`` slice so a display bug can never break
 * the console.
 */

/**
 * Read the operator's display timezone from a console pane's
 * ``data-display-tz`` attribute (or any element that carries it).
 *
 * @param {HTMLElement|null} [el] - element to read from; defaults to the
 *   first ``.terminal-pane`` in the document.
 * @returns {string} an IANA zone name (e.g. ``America/New_York``) or
 *   ``"UTC"`` when unset.
 */
export function readDisplayTz(el) {
  let node = el || null;
  if (!node && typeof document !== "undefined") {
    node = document.querySelector(".terminal-pane");
  }
  const tz = node && node.dataset ? node.dataset.displayTz : "";
  return tz && String(tz).trim() ? String(tz).trim() : "UTC";
}

/**
 * Parse an ISO-8601 timestamp (with or without a trailing ``Z`` /
 * offset) into a ``Date``. Naive strings are treated as UTC. Returns
 * ``null`` for empty / unparseable input.
 *
 * @param {string} iso
 * @returns {Date|null}
 */
export function parseTs(iso) {
  const s = String(iso || "").trim();
  if (!s) return null;
  // ``Date`` parses ISO-8601 with offsets natively. A naive string
  // (no ``Z`` / offset) is assumed UTC to match the server's storage
  // contract — append ``Z`` in that case.
  const hasZone = /[zZ]$|[+\-]\d{2}:?\d{2}$/.test(s);
  const d = new Date(hasZone ? s : s + "Z");
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * Parse an ISO-8601 UTC timestamp into epoch milliseconds, or ``null``
 * when unparseable. Used by the elapsed-clock anchors.
 *
 * @param {string} iso
 * @returns {number|null}
 */
export function parseTsMs(iso) {
  const d = parseTs(iso);
  return d ? d.getTime() : null;
}

// Cache formatters by zone — constructing an ``Intl.DateTimeFormat`` is
// not free and the console re-renders often.
const _fmtCache = new Map();

function _clockFormatter(tz) {
  const zone = tz || "UTC";
  let f = _fmtCache.get(zone);
  if (f !== undefined) return f;
  try {
    f = new Intl.DateTimeFormat("en-GB", {
      timeZone: zone,
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch (e) {
    f = null; // unknown zone / no Intl — signal the UTC fallback
  }
  _fmtCache.set(zone, f);
  return f;
}

/**
 * Format an ISO-8601 UTC timestamp as a ``HH:MM:SS`` wall-clock time in
 * the given IANA zone. Falls back to a plain UTC ``HH:MM:SS`` slice on
 * any error so the console never shows an empty / broken time.
 *
 * @param {string} iso - the event ``ts``.
 * @param {string} [tz] - IANA zone name; defaults to UTC.
 * @returns {string}
 */
export function formatClockTs(iso, tz) {
  const d = parseTs(iso);
  if (!d) return _utcSlice(iso);
  const f = _clockFormatter(tz);
  if (!f) return _utcSlice(iso);
  try {
    // ``en-GB`` 24h output is ``HH:MM:SS``. Normalise the odd
    // ``24:xx:xx`` some engines emit at midnight down to ``00``.
    return f.format(d).replace(/^24:/, "00:");
  } catch (e) {
    return _utcSlice(iso);
  }
}

function _utcSlice(iso) {
  // Last-resort: trim the raw ISO string to HH:MM:SS (UTC).
  const s = String(iso || "");
  const m = s.match(/T(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : s.slice(0, 8);
}

export default {
  readDisplayTz,
  parseTs,
  parseTsMs,
  formatClockTs,
};
