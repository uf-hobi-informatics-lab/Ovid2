/*
 * Ovid2 terminal-pane event formatter.
 *
 * Turns one JSONL event record from the pipeline event log into one
 * rendered terminal line ``{ kind, className, ts, text, meta }``.
 *
 * The formatter is a pure function — no DOM, no side effects — so the
 * mount module can render (and re-render) freely.
 *
 * The set of kinds mirrors ``ovid2.core.events.EVENT_KINDS``. Any
 * unknown kind is rendered as a dim ``· <kind>`` line so future
 * additions surface without a code change.
 *
 * The ``engine.metrics`` kind is intentionally filtered out
 * (returns ``null``) — those samples are already visualised in the
 * compact metrics canvas above the terminal, and re-rendering them
 * as text lines would swamp the log.
 */

const KIND_INSTANCE_STARTED = "instance.started";
const KIND_INSTANCE_FINISHED = "instance.finished";
const KIND_BATCH_RECEIVED = "batch.received";
const KIND_BATCH_DISPATCHED = "batch.dispatched";
const KIND_BATCH_COMPLETED = "batch.completed";
const KIND_BATCH_FAILED = "batch.failed";
const KIND_TABLE_STARTED = "table.started";
const KIND_TABLE_COMPLETED = "table.completed";
const KIND_TABLE_FAILED = "table.failed";
const KIND_STAGE_PROGRESS = "stage.progress";
const KIND_FIX_APPLIED = "fix.applied";
const KIND_FIX_SKIPPED = "fix.skipped";
const KIND_FIX_FAILED = "fix.failed";
const KIND_ENGINE_METRICS = "engine.metrics";
const KIND_LOG = "log";
const KIND_WARNING = "warning";
const KIND_ERROR = "error";

/**
 * Format one event record.
 *
 * @param {object} rec
 *   JSONL record with at minimum a ``kind`` field.
 * @returns {null | { kind: string, className: string, ts: string, text: string, meta: object }}
 *   ``null`` when the event should be suppressed (currently just
 *   ``engine.metrics``).
 */
export function formatEvent(rec) {
  if (!rec || typeof rec !== "object") return null;
  const kind = String(rec.kind || "");
  if (kind === KIND_ENGINE_METRICS) return null;

  const ts = shortTs(rec.ts);
  const meta = {
    pipeline: str(rec.pipeline),
    batch_id: str(rec.batch_id),
    table: str(rec.table),
    path: str(rec.path),
  };

  switch (kind) {
    case KIND_INSTANCE_STARTED: {
      const pipeline = meta.pipeline ? " pipeline=" + meta.pipeline : "";
      return line("tl-banner", ts, "▮ instance.started" + pipeline, meta);
    }
    case KIND_INSTANCE_FINISHED: {
      const ok = getExtra(rec, "success", true);
      return line(
        ok ? "tl-ok" : "tl-err",
        ts,
        "■ instance.finished " + (ok ? "success" : "failure"),
        meta,
      );
    }
    case KIND_BATCH_RECEIVED:
      return line("tl-info", ts, "» batch.received " + fmtBatch(meta), meta);
    case KIND_BATCH_DISPATCHED:
      return line("tl-info", ts, "» batch.dispatched " + fmtBatch(meta), meta);
    case KIND_BATCH_COMPLETED: {
      const el = fmtElapsedMs(rec.elapsed_ms);
      return line(
        "tl-ok",
        ts,
        "✓ batch.completed " + fmtBatch(meta) + (el ? " in " + el : ""),
        meta,
      );
    }
    case KIND_BATCH_FAILED:
      return line(
        "tl-err",
        ts,
        "✗ batch.failed " + fmtBatch(meta) +
          (rec.error ? " — " + trim(rec.error, 200) : ""),
        meta,
      );
    case KIND_TABLE_STARTED:
      return line(
        "tl-info",
        ts,
        "▸ table.started " + fmtTable(meta),
        meta,
      );
    case KIND_TABLE_COMPLETED: {
      const el = fmtElapsedMs(rec.elapsed_ms);
      return line(
        "tl-ok",
        ts,
        "✓ table.completed " + fmtTable(meta) + (el ? " in " + el : ""),
        meta,
      );
    }
    case KIND_TABLE_FAILED:
      return line(
        "tl-err",
        ts,
        "✗ table.failed " + fmtTable(meta) +
          (rec.error ? " — " + trim(rec.error, 200) : ""),
        meta,
      );
    case KIND_STAGE_PROGRESS: {
      const stage = str(rec.stage) || str(getExtra(rec, "stage", ""));
      const current = numOr(getExtra(rec, "current", null), null);
      const total = numOr(getExtra(rec, "total", null), null);
      const suffix =
        current !== null && total !== null ? " " + current + "/" + total : "";
      return line(
        "tl-dim",
        ts,
        "· stage=" + (stage || "?") + suffix,
        meta,
      );
    }
    case KIND_FIX_APPLIED: {
      const fix = str(rec.fix);
      const cur = numOr(rec.current_fix, 0);
      const tot = numOr(rec.total_fixes, 0);
      const cnt = tot ? " (" + cur + "/" + tot + ")" : "";
      return line(
        "tl-fix",
        ts,
        "▲ fix.applied " + fix + cnt + " on " + fmtTable(meta),
        meta,
      );
    }
    case KIND_FIX_SKIPPED:
      return line(
        "tl-dim",
        ts,
        "○ fix.skipped " + str(rec.fix) + " on " + fmtTable(meta),
        meta,
      );
    case KIND_FIX_FAILED:
      return line(
        "tl-err",
        ts,
        "✗ fix.failed " + str(rec.fix) + " on " + fmtTable(meta) +
          (rec.error ? " — " + trim(rec.error, 200) : ""),
        meta,
      );
    case KIND_WARNING:
      return line("tl-warn", ts, "! " + str(rec.message), meta);
    case KIND_ERROR:
      return line("tl-err", ts, "✗ " + str(rec.message), meta);
    case KIND_LOG: {
      const level = String(rec.level || "info").toLowerCase();
      const cls =
        level === "error"
          ? "tl-err"
          : level === "warning" || level === "warn"
          ? "tl-warn"
          : "tl-dim";
      return line(cls, ts, "· " + str(rec.message), meta);
    }
    default:
      return line("tl-dim", ts, "· " + kind, meta);
  }
}

/* ------------------------------------------------------------------ */
/* helpers                                                            */
/* ------------------------------------------------------------------ */

function line(className, ts, text, meta) {
  return { kind: "line", className, ts, text, meta };
}

function str(v) {
  return v == null ? "" : String(v);
}

function numOr(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function trim(s, max) {
  s = String(s);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

function fmtBatch(meta) {
  const parts = [];
  if (meta.pipeline) parts.push("pipeline=" + meta.pipeline);
  if (meta.batch_id) parts.push("batch=" + meta.batch_id);
  return parts.join(" ");
}

function fmtTable(meta) {
  const parts = [];
  if (meta.pipeline) parts.push(meta.pipeline);
  if (meta.batch_id) parts.push(meta.batch_id);
  if (meta.table) parts.push("table=" + meta.table);
  return parts.join(" ");
}

function fmtElapsedMs(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 10_000) return (n / 1000).toFixed(1) + "s";
  const s = Math.floor(n / 1000);
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return mm > 0 ? mm + "m" + String(ss).padStart(2, "0") + "s" : ss + "s";
}

function shortTs(iso) {
  if (!iso) return "";
  // ISO-8601 → HH:MM:SS (24h). We only need the wall-clock time; the
  // date is redundant on a run that lasts minutes to hours.
  const s = String(iso);
  const t = s.indexOf("T");
  if (t < 0) return "";
  const rest = s.slice(t + 1);
  const dot = rest.indexOf(".");
  const hhmmss = dot > 0 ? rest.slice(0, dot) : rest.slice(0, 8);
  return hhmmss;
}

function getExtra(rec, key, fallback) {
  if (rec && rec.extra && key in rec.extra) return rec.extra[key];
  if (rec && key in rec) return rec[key];
  return fallback;
}

export const TERMINAL_KINDS = Object.freeze({
  INSTANCE_STARTED: KIND_INSTANCE_STARTED,
  INSTANCE_FINISHED: KIND_INSTANCE_FINISHED,
  BATCH_COMPLETED: KIND_BATCH_COMPLETED,
  BATCH_FAILED: KIND_BATCH_FAILED,
  TABLE_STARTED: KIND_TABLE_STARTED,
  TABLE_COMPLETED: KIND_TABLE_COMPLETED,
  TABLE_FAILED: KIND_TABLE_FAILED,
  FIX_APPLIED: KIND_FIX_APPLIED,
});
