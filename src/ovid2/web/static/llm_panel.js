/* Ovid2 assistant panel (L1).
 *
 * Right-rail button #tool-llm toggles #llm-panel: a canvas (chat turns,
 * answer blocks, System 1 decision cards) above a chat box.
 *
 * - Status:   GET  /api/v1/llm/status
 * - Chat:     POST /api/v1/llm/chat/stream  (SSE over a fetch body stream;
 *             EventSource cannot POST)
 * - History:  sessionStorage only ("ovid2LlmConversation"); the server
 *             stores nothing but metadata audit rows.
 * - Tools:    tool_call / tool_result events render as collapsible chips
 *             (read-only GETs the executor ran). proposal events render as
 *             a confirmation card; "Run" executes the request from THIS
 *             browser session (the user's own permissions + approval gate),
 *             then feeds the outcome back as the next turn.
 *             open_draft events (create_job_draft) are executed from this
 *             browser session WITHOUT a click once the stream ends: they only
 *             create a draft job (never run by the worker), then navigate to
 *             /runs/<instance_id> where the human presses ▶ to launch it.
 *             ui_action events (set_appearance / save_appearance /
 *             apply_saved_appearance) change ONLY this browser's look. They
 *             make no HTTP call: every argument is re-validated here against
 *             a strict allow-list (theme slugs from Ovid2Theme, CSS vars from
 *             Ovid2Appearance.ALLOWED_VARS, typed value patterns), applied
 *             immediately as the event arrives, and rendered as a chip with
 *             Undo (restores the snapshot taken just before). Replayed
 *             history never re-applies an action.
 * - Context:  every turn sends ui_context (current theme, overrides, saved
 *             theme names) so the model can answer "make it darker".
 *
 * All model output is rendered through a small escaping Markdown
 * renderer — raw HTML from the model is never injected.
 */
(function () {
  "use strict";

  var html = document.documentElement;
  var OPEN_KEY = "ovid2LlmPanelOpen";
  var CONVO_KEY = "ovid2LlmConversation";
  var MAX_TURNS = 40;
  var WELCOME = "How can I help?";

  var state = {
    messages: [],      // [{role, content, meta?}]
    status: null,
    controller: null,
    busy: false,
    pendingDrafts: []  // open_draft events to execute once the stream ends
  };

  /* ---------------- utilities ---------------- */

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function save() {
    try {
      var keep = state.messages.slice(-MAX_TURNS);
      sessionStorage.setItem(CONVO_KEY, JSON.stringify(keep));
    } catch (e) { /* ignore quota */ }
  }

  function load() {
    try {
      var raw = sessionStorage.getItem(CONVO_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      if (Array.isArray(parsed)) state.messages = parsed.filter(function (m) {
        return m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string";
      });
    } catch (e) { state.messages = []; }
  }

  /* ---------------- markdown (escape-first) ---------------- */

  function inline(text) {
    var s = esc(text);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
                  '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return s;
  }

  function renderTableRows(lines) {
    var rows = lines.map(function (l) {
      return l.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(function (c) { return c.trim(); });
    });
    var head = rows[0];
    var body = rows.slice(2);
    var out = "<table><thead><tr>" + head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") + "</tr></thead><tbody>";
    body.forEach(function (r) {
      out += "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
    });
    return out + "</tbody></table>";
  }

  function blockHtml(json) {
    var b;
    try { b = JSON.parse(json); } catch (e) { return null; }
    if (!b || typeof b !== "object") return null;
    var title = b.title ? '<div class="llm-block-title">' + esc(b.title) + "</div>" : "";
    if (b.type === "table" && Array.isArray(b.columns) && Array.isArray(b.rows)) {
      var t = "<table><thead><tr>" + b.columns.map(function (c) { return "<th>" + esc(c) + "</th>"; }).join("") + "</tr></thead><tbody>";
      b.rows.slice(0, 200).forEach(function (r) {
        if (!Array.isArray(r)) return;
        t += "<tr>" + r.map(function (c) { return "<td>" + esc(c) + "</td>"; }).join("") + "</tr>";
      });
      return '<div class="llm-block">' + title + t + "</tbody></table></div>";
    }
    if (b.type === "kpi-row" && Array.isArray(b.items)) {
      return '<div class="llm-block">' + title + '<div class="llm-kpis">' + b.items.slice(0, 12).map(function (k) {
        return '<div class="llm-kpi"><div class="llm-kpi-value">' + esc(k && k.value) +
               '</div><div class="llm-kpi-label">' + esc(k && k.label) + "</div></div>";
      }).join("") + "</div></div>";
    }
    if (b.type === "status-list" && Array.isArray(b.items)) {
      var cls = { ok: "severity-ok", warn: "severity-warning", error: "severity-error" };
      return '<div class="llm-block">' + title + '<ul class="llm-status-list">' + b.items.slice(0, 50).map(function (i) {
        var st = (i && i.status) || "ok";
        return '<li><span class="badge ' + (cls[st] || "severity-ok") + '">' + esc(st) + "</span><span>" +
               esc(i && i.label) + '</span><span class="small muted">' + esc(i && i.detail) + "</span></li>";
      }).join("") + "</ul></div>";
    }
    return null;
  }

  function markdown(src) {
    var lines = String(src || "").split("\n");
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      var fence = line.match(/^\s*```\s*([\w-]*)\s*$/);
      if (fence) {
        var lang = fence[1];
        var buf = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++; // closing fence (or EOF while streaming)
        var rendered = lang === "ovid-block" ? blockHtml(buf.join("\n")) : null;
        out.push(rendered || "<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        var tbl = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { tbl.push(lines[i]); i++; }
        out.push(renderTableRows(tbl));
        continue;
      }
      var h = line.match(/^(#{1,4})\s+(.*)$/);
      if (h) { out.push("<p><strong>" + inline(h[2]) + "</strong></p>"); i++; continue; }
      if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line);
        var items = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
          items.push("<li>" + inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "")) + "</li>");
          i++;
        }
        out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
        continue;
      }
      if (!line.trim()) { i++; continue; }
      var para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*```/.test(lines[i]) &&
             !/^(#{1,4})\s+/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s+/.test(lines[i]) &&
             !/^\s*\|.*\|\s*$/.test(lines[i])) {
        para.push(inline(lines[i]));
        i++;
      }
      out.push("<p>" + para.join("<br>") + "</p>");
    }
    return out.join("");
  }

  /* ---------------- UI actions (browser-only, cosmetic) ---------------- */

  var UI_ACTIONS = ["set_appearance", "save_appearance", "apply_saved_appearance"];
  var HEX = /^#[0-9a-fA-F]{6}$/;
  var FONT = /^(var\(--(font-sans|font-serif|mono|font-display)\)|[A-Za-z0-9 ,"'-]{1,160})$/;
  var SIZE = /^[0-9]{1,2}(\.[0-9]{1,3})?(px|rem|em)$/;
  var WEIGHT = /^[1-9]00$/;
  var COLOR_KEYS = ["accent", "bg", "surface", "fg"];
  var DERIVED = ["--accent-hover", "--accent-subtle", "--accent-fg"];
  var MIN_CONTRAST = 3;

  function themeLib() { return window.Ovid2Theme || null; }
  function appLib() { return window.Ovid2Appearance || null; }

  function savedNames() {
    var a = appLib();
    return a && a.readSaved ? a.readSaved().map(function (e) { return e && e.name; })
      .filter(function (n) { return typeof n === "string"; }).slice(0, 30) : [];
  }

  function uiContext() {
    var t = themeLib(), a = appLib();
    if (!t || !a) return null;
    return { theme: t.currentTheme(), colors: t.readCustom ? t.readCustom() : {},
             appearance: a.readAppearance ? a.readAppearance() : {},
             saved_themes: savedNames() };
  }

  // Validate one CSS-var override; returns an error string or "".
  function checkVar(k, v) {
    var a = appLib();
    if (!a || (a.ALLOWED_VARS || []).indexOf(k) === -1 || DERIVED.indexOf(k) !== -1) {
      return "not an allowed style variable: " + k;
    }
    if (typeof v !== "string") return k + " must be a string";
    var ok = /-ff$/.test(k) ? FONT.test(v) : /-fs$/.test(k) ? SIZE.test(v) :
             /-fw$/.test(k) ? WEIGHT.test(v) : HEX.test(v);
    return ok ? "" : "bad value for " + k + ": " + v;
  }

  // Full argument check, independent of the executor's schema validation.
  function checkUiArgs(name, args) {
    var t = themeLib(), a = appLib();
    if (!t || !a || !a.repaint) return "appearance controls are not loaded on this page";
    if (UI_ACTIONS.indexOf(name) === -1) return "unknown UI action " + name;
    if (!args || typeof args !== "object" || Array.isArray(args)) return "arguments must be an object";
    var keys = Object.keys(args);
    if (name !== "set_appearance") {
      if (keys.length !== 1 || typeof args.name !== "string" || !args.name.trim() ||
          args.name.length > 60 || /[<>]/.test(args.name)) return "a theme name is required";
      if (name === "apply_saved_appearance" && savedNames().indexOf(args.name) === -1) {
        return "no saved theme called \u201c" + args.name + "\u201d";
      }
      return "";
    }
    if (!keys.length) return "nothing to change";
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i], v = args[k];
      if (k === "theme") { if (!t.isKnownSlug(v)) return "unknown theme " + v; }
      else if (k === "reset") { if (typeof v !== "boolean") return "reset must be true/false"; }
      else if (k === "colors") {
        if (!v || typeof v !== "object" || Array.isArray(v)) return "colors must be an object";
        for (var c in v) {
          if (COLOR_KEYS.indexOf(c) === -1) return "unknown colour " + c;
          if (!HEX.test(v[c])) return "bad colour for " + c + ": " + v[c];
        }
      } else if (k === "appearance") {
        if (!v || typeof v !== "object" || Array.isArray(v)) return "appearance must be an object";
        for (var p in v) { var e = checkVar(p, v[p]); if (e) return e; }
      } else return "unknown argument " + k;
    }
    return "";
  }

  function snapshot(withSaved) {
    var t = themeLib(), a = appLib();
    var s = { theme: t.currentTheme(), custom: t.readCustom(), appearance: a.readAppearance() };
    if (withSaved) s.saved = a.readSaved();
    return s;
  }

  function restore(s) {
    var t = themeLib(), a = appLib();
    if (!t || !a || !s) return;
    t.writeCustom(s.custom || {});
    a.writeAppearance(s.appearance || {});
    if (s.saved) a.writeSaved(s.saved);
    if (t.isKnownSlug(s.theme)) t.applyTheme(s.theme);
    a.repaint();
    if (a.refresh) a.refresh();
  }

  function computedHex(name) {
    try {
      var v = getComputedStyle(html).getPropertyValue(name).trim();
      return HEX.test(v) ? v : null;
    } catch (e) { return null; }
  }

  // Fold quick colours into appearance vars so a saved theme reproduces
  // exactly what is on screen (appearance overrides win, as when painted).
  function foldedOverrides() {
    var t = themeLib(), a = appLib();
    var map = {}, c = t.readCustom(), ap = a.readAppearance();
    if (c.accent) map["--accent"] = c.accent;
    if (c.bg) map["--bg"] = c.bg;
    if (c.surface) { map["--bg-elev"] = c.surface; map["--card-bg"] = c.surface; }
    if (c.fg) map["--fg"] = c.fg;
    Object.keys(ap).forEach(function (k) { map[k] = ap[k]; });
    return map;
  }

  function describeUi(name, args) {
    if (name === "save_appearance") return "Saved the current look as \u201c" + args.name + "\u201d";
    if (name === "apply_saved_appearance") return "Applied saved theme \u201c" + args.name + "\u201d";
    var bits = [];
    if (args.theme) {
      var meta = themeLib().getTheme ? themeLib().getTheme(args.theme) : null;
      bits.push("theme \u2192 " + (meta ? meta.label : args.theme));
    }
    if (args.reset) bits.push("cleared overrides");
    var n = Object.keys(args.colors || {}).length + Object.keys(args.appearance || {}).length;
    if (n) bits.push(n + " style override" + (n === 1 ? "" : "s"));
    return "Appearance: " + bits.join(", ");
  }

  // Apply a ui_action event. ``u`` is the record stored on the message.
  function applyUiAction(u) {
    var err = checkUiArgs(u.name, u.args);
    if (err) { u.state = "refused"; u.outcome = "Refused: " + err + "."; return; }
    var t = themeLib(), a = appLib(), args = u.args;
    var before = snapshot(u.name === "save_appearance");
    try {
      if (u.name === "save_appearance") {
        var arr = a.readSaved().filter(function (e) { return e && e.name !== args.name; });
        arr.push({ name: args.name, theme: t.currentTheme(), overrides: foldedOverrides() });
        a.writeSaved(arr);
      } else if (u.name === "apply_saved_appearance") {
        var entry = a.readSaved().filter(function (e) { return e && e.name === args.name; })[0];
        var ov = {};
        Object.keys((entry && entry.overrides) || {}).forEach(function (k) {
          if (!checkVar(k, entry.overrides[k])) ov[k] = entry.overrides[k];
        });
        t.writeCustom({});
        a.writeAppearance(ov);
        if (entry && t.isKnownSlug(entry.theme)) t.applyTheme(entry.theme);
      } else {
        if (args.theme || args.reset) { t.writeCustom({}); a.writeAppearance({}); }
        if (args.theme) t.applyTheme(args.theme);
        if (args.colors) {
          var c = t.readCustom();
          Object.keys(args.colors).forEach(function (k) { c[k] = args.colors[k].toLowerCase(); });
          t.writeCustom(c);
        }
        if (args.appearance) {
          var map = a.readAppearance();
          Object.keys(args.appearance).forEach(function (k) { map[k] = args.appearance[k]; });
          if (args.appearance["--bg"] && !args.appearance["--bg-elev"]) delete map["--bg-elev"];
          a.writeAppearance(map);
        }
      }
      a.repaint();
      // Legibility guard: never leave the page (and this Undo button)
      // unreadable. Skipped when a token is not a plain hex value.
      var fg = computedHex("--fg"), bg = computedHex("--bg");
      if (fg && bg && t.contrastRatio && t.contrastRatio(fg, bg) < MIN_CONTRAST) {
        restore(before);
        u.state = "refused";
        u.outcome = "Refused: text colour " + fg + " on " + bg + " would be unreadable (contrast " +
          t.contrastRatio(fg, bg).toFixed(1) + ":1, need " + MIN_CONTRAST + ":1).";
        return;
      }
      if (a.refresh) a.refresh();
      u.before = before;
      u.state = "applied";
      u.outcome = describeUi(u.name, args) + ".";
    } catch (e) {
      restore(before);
      u.state = "refused";
      u.outcome = "Failed: " + ((e && e.message) || "error") + ".";
    }
  }

  function undoUiAction(u) {
    if (u.state !== "applied" || !u.before) return;
    restore(u.before);
    u.state = "undone";
    u.outcome = describeUi(u.name, u.args) + " \u2014 undone.";
    save();
    renderAll();
  }

  function uiActionChip(u) {
    var box = el("div", "llm-ui-action " + (u.state || "applied"));
    var badge = { applied: "severity-ok", undone: "", refused: "severity-warning" }[u.state] || "";
    var label = { applied: "applied", undone: "undone", refused: "refused" }[u.state] || "\u2026";
    var head = el("span", "llm-ui-action-text");
    head.innerHTML = '<span class="badge ' + badge + '">' + esc(label) + "</span> " + esc(u.outcome || u.name);
    box.appendChild(head);
    if (u.state === "applied" && u.before) {
      var undo = el("button", "button small", "Undo");
      undo.type = "button";
      undo.title = "Restore the look from just before this change";
      undo.addEventListener("click", function () { undoUiAction(u); });
      box.appendChild(undo);
    }
    return box;
  }

  /* ---------------- canvas rendering ---------------- */

  function decisionCard(d) {
    var card = el("div", "llm-block llm-decision" + (d.accepted ? "" : " rejected"));
    var pct = Math.round(Math.max(0, Math.min(1, Number(d.confidence) || 0)) * 100);
    card.innerHTML =
      '<div class="llm-block-title">System 1 decision ' +
      (d.accepted ? '<span class="badge severity-ok">accepted</span>'
                  : '<span class="badge severity-warning">deferred</span>') + "</div>" +
      (d.decision ? "<div><strong>" + esc(d.decision) + "</strong></div>" : "") +
      (d.action ? '<div class="small">Proposed action: <code>' + esc(d.action) + "</code> (not executed)</div>" : "") +
      (d.rationale ? '<div class="small muted">' + esc(d.rationale) + "</div>" : "") +
      '<div class="small muted">confidence ' + pct + "% &middot; " + esc(d.connection) + "</div>" +
      '<div class="llm-conf"><span style="width:' + pct + '%"></span></div>';
    return card;
  }

  function metaLine(meta) {
    if (!meta) return "";
    var parts = [];
    if (meta.route) parts.push(meta.route === "system1" ? "System 1" : "General");
    if (meta.model) parts.push(meta.model);
    if (meta.elapsed_ms) parts.push((meta.elapsed_ms / 1000).toFixed(1) + "s");
    if (meta.completion_tokens) parts.push(meta.completion_tokens + " tok");
    return parts.join(" · ");
  }

  function toolChip(t) {
    var rejected = t.result && (t.result.rejected || !t.result.status);
    var det = el("details", "llm-tool" + (t.result && !t.result.ok ? " failed" : "") +
                 (rejected ? " rejected" : ""));
    if (rejected) det.open = true;
    var st = t.result ? (t.result.ok ? "ok" : "error") : "running";
    var badge = { ok: "severity-ok", error: "severity-error", running: "severity-warning" }[st];
    var sum = el("summary");
    if (rejected) {
      sum.innerHTML = '<span class="badge severity-warning">rejected</span> <code>' + esc(t.name) +
        "</code> " + '<span class="small muted">' + esc(t.result.summary || "") + "</span>";
      det.appendChild(sum);
      return det;
    }
    sum.innerHTML = '<span class="badge ' + badge + '">' + esc(t.result ? (t.result.status || "err") : "…") +
      "</span> <code>" + esc(t.name) + "</code> " +
      '<span class="small muted">' + esc((t.method || "") + " " + (t.path || "")) +
      (t.result && t.result.elapsed_ms != null ? " · " + esc(t.result.elapsed_ms) + " ms" : "") + "</span>";
    det.appendChild(sum);
    if (t.arguments && Object.keys(t.arguments).length) {
      det.appendChild(el("div", "small muted", "args: " + JSON.stringify(t.arguments)));
    }
    if (t.result) {
      var pre = el("pre", "llm-tool-out");
      pre.textContent = t.result.preview || t.result.summary || "";
      det.appendChild(pre);
    }
    return det;
  }

  function proposalCard(m, p) {
    if (p.draft) {
      var dc = el("div", "llm-block llm-proposal llm-draft " + (p.state || "running"));
      var dh = el("div", "llm-block-title");
      dh.innerHTML = "Draft job <code>" + esc((p.body && p.body.pipeline) || p.name) + "</code>" +
        (p.state === "done" ? ' <span class="badge severity-ok">created</span>' :
         p.state === "failed" ? ' <span class="badge severity-error">failed</span>' :
         ' <span class="badge severity-warning">creating…</span>');
      dc.appendChild(dh);
      if (p.body && Object.keys(p.body).length) {
        var dpre = el("pre", "llm-tool-out");
        dpre.textContent = JSON.stringify(p.body, null, 2);
        dc.appendChild(dpre);
      }
      if (p.outcome) dc.appendChild(el("div", "small muted", p.outcome));
      if (p.url) {
        var a = el("a", "button primary small", "Open run page");
        a.href = p.url;
        dc.appendChild(a);
      }
      return dc;
    }
    var card = el("div", "llm-block llm-proposal " + (p.state || "pending"));
    var head = el("div", "llm-block-title");
    head.innerHTML = "Proposed action <code>" + esc(p.name) + "</code>" +
      (p.state === "done" ? ' <span class="badge severity-ok">ran</span>' :
       p.state === "failed" ? ' <span class="badge severity-error">failed</span>' :
       p.state === "awaiting" ? ' <span class="badge severity-warning">awaiting approval</span>' :
       p.state === "dismissed" ? ' <span class="badge">dismissed</span>' : "");
    card.appendChild(head);
    card.appendChild(el("div", "small", (p.method || "") + " " + (p.path || "")));
    if (p.body && Object.keys(p.body).length) {
      var pre = el("pre", "llm-tool-out");
      pre.textContent = JSON.stringify(p.body, null, 2);
      card.appendChild(pre);
    }
    if (p.outcome) card.appendChild(el("div", "small muted", p.outcome));
    if (!p.state || p.state === "pending") {
      var row = el("div", "llm-proposal-actions");
      var run = el("button", "button primary small", "Run");
      run.type = "button";
      run.disabled = state.busy;
      run.addEventListener("click", function () { runProposal(m, p); });
      var dis = el("button", "button small", "Dismiss");
      dis.type = "button";
      dis.addEventListener("click", function () { p.state = "dismissed"; save(); renderAll(); });
      row.appendChild(run);
      row.appendChild(dis);
      card.appendChild(row);
      card.appendChild(el("div", "small muted",
        "Runs with your own permissions. Nothing happens until you click Run."));
    }
    return card;
  }

  function renderMessage(m) {
    var box = el("div", "llm-msg " + m.role + (m.meta && m.meta.error ? " error" : "") +
                 (m.meta && m.meta.kind === "action" ? " action" : ""));
    if (m.role === "user") {
      box.textContent = m.content;
      return box;
    }
    var meta = m.meta || {};
    if (meta.decision) box.appendChild(decisionCard(meta.decision));
    (meta.notices || []).forEach(function (n) {
      // Older stored turns may still carry the "System 1 unavailable" notice
      // the router used to emit for an unimplemented provider; hide it.
      if (/^System 1 unavailable/.test(String(n))) return;
      box.appendChild(el("div", "llm-notice", n));
    });
    (meta.tools || []).forEach(function (t) { box.appendChild(toolChip(t)); });
    (meta.uiActions || []).forEach(function (u) { box.appendChild(uiActionChip(u)); });
    // Model / latency / token metadata is kept on the message (sessionStorage)
    // but no longer printed per answer — the routed models are shown once in
    // the small panel footer (#llm-route) for reference.
    var line = metaLine(meta);
    if (line) box.title = line;
    if (meta.reasoning) {
      var det = el("details", "llm-reasoning");
      det.appendChild(el("summary", null, "Reasoning"));
      det.appendChild(el("div", null, meta.reasoning));
      box.appendChild(det);
    }
    var body = el("div", "llm-body");
    body.innerHTML = markdown(m.content);
    box.appendChild(body);
    (meta.proposals || []).forEach(function (p) { box.appendChild(proposalCard(m, p)); });
    if (meta.error) box.appendChild(el("div", "llm-notice", "Error: " + meta.error));
    return box;
  }

  function renderAll() {
    var canvas = $("llm-canvas");
    if (!canvas) return;
    canvas.innerHTML = "";
    if (!state.messages.length) {
      var empty = el("div", "llm-empty");
      var welcome = emptyText();
      if (welcome === WELCOME) empty.classList.add("welcome");
      empty.innerHTML = welcome;
      canvas.appendChild(empty);
      return;
    }
    state.messages.forEach(function (m) { canvas.appendChild(renderMessage(m)); });
    canvas.scrollTop = canvas.scrollHeight;
  }

  function emptyText() {
    var s = state.status;
    var panel = $("llm-panel");
    var canManage = panel && panel.getAttribute("data-manage") === "true";
    if (!s) return "Loading assistant status…";
    if (!s.enabled) {
      return "The assistant is disabled." + (canManage
        ? ' Configure a model and enable it under <a href="/llms">LLMs</a>.'
        : " Ask an administrator to enable it.");
    }
    if (!s.general && !s.system1) {
      return "No model is routed." + (canManage ? ' Pick one under <a href="/llms">LLMs → Routing</a>.' : "");
    }
    return WELCOME;
  }

  function renderRoute() {
    var r = $("llm-route");
    if (!r) return;
    var s = state.status;
    if (!s || !s.enabled) { r.textContent = ""; r.title = ""; return; }
    var parts = [];
    if (s.system1) parts.push("System 1: " + (s.system1_model || s.system1));
    if (s.general) parts.push("General: " + (s.general_model || s.general));
    r.textContent = parts.join(" · ");
    r.title = r.textContent;
  }

  /* ---------------- networking ---------------- */

  function fetchStatus() {
    return fetch("/api/v1/llm/status", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) { state.status = s; renderRoute(); if (!state.messages.length) renderAll(); })
      .catch(function () { state.status = null; });
  }

  function setBusy(on) {
    state.busy = on;
    var send = $("llm-send"), stop = $("llm-stop"), input = $("llm-input");
    if (send) send.disabled = on;
    if (stop) stop.hidden = !on;
    if (input) input.disabled = on;
  }

  function parseSse(buffer, onEvent) {
    var idx;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      var frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      var data = [];
      frame.split("\n").forEach(function (l) {
        if (l.indexOf("data:") === 0) data.push(l.slice(5).replace(/^ /, ""));
      });
      if (!data.length) continue;
      try { onEvent(JSON.parse(data.join("\n"))); } catch (e) { /* skip */ }
    }
    return buffer;
  }

  function runProposal(m, p) {
    if (state.busy || (p.state && p.state !== "pending")) return;
    if (!/^\/api\/v1\/[A-Za-z0-9_\-%.:\/]+$/.test(p.path || "") ||
        ["POST", "PUT"].indexOf(p.method) === -1) {
      p.state = "failed";
      p.outcome = "Refused: not an Ovid2 API action.";
      save(); renderAll();
      return;
    }
    p.state = "running";
    renderAll();
    var opts = { method: p.method, headers: { "Accept": "application/json" }, credentials: "same-origin" };
    if (p.method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(p.body || {});
    }
    fetch(p.path, opts).then(function (resp) {
      return resp.text().then(function (txt) {
        var data;
        try { data = JSON.parse(txt); } catch (e) { data = txt; }
        return { status: resp.status, ok: resp.ok, data: data };
      });
    }).then(function (r) {
      var detail = r.data && r.data.detail;
      if (r.status === 202 && detail && detail.status === "awaiting_approval") {
        p.state = "awaiting";
        p.outcome = "Awaiting human approval (" + detail.approval_id + ").";
      } else {
        p.state = r.ok ? "done" : "failed";
        p.outcome = "HTTP " + r.status;
      }
      save();
      renderAll();
      var shown = typeof r.data === "string" ? r.data : JSON.stringify(r.data);
      if (shown.length > 4000) shown = shown.slice(0, 4000) + "…";
      send("[I ran the proposed action `" + p.name + "` (" + p.method + " " + p.path +
           ") → HTTP " + r.status + "]\n```json\n" + shown + "\n```\n" +
           "Briefly confirm the outcome and suggest a next step.", { kind: "action" });
    }).catch(function (err) {
      p.state = "failed";
      p.outcome = (err && err.message) || "Network error";
      save();
      renderAll();
    });
  }

  function uiBody(history) {
    var body = { messages: history };
    var ctx = null;
    try { ctx = uiContext(); } catch (e) { ctx = null; }
    if (ctx) body.ui_context = ctx;
    return body;
  }

  function send(text, opts) {
    if (state.busy || !text.trim()) return;
    var userMsg = { role: "user", content: text };
    if (opts && opts.kind) userMsg.meta = { kind: opts.kind };
    state.messages.push(userMsg);
    var reply = { role: "assistant", content: "",
                  meta: { notices: [], reasoning: "", tools: [], proposals: [], uiActions: [] } };
    state.messages.push(reply);
    renderAll();
    save();

    var canvas = $("llm-canvas");
    var node = canvas && canvas.lastChild;
    var pending = false;
    function repaint() {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(function () {
        pending = false;
        if (!canvas) return;
        var fresh = renderMessage(reply);
        if (state.busy) fresh.querySelector(".llm-body").classList.add("llm-cursor");
        if (node && node.parentNode === canvas) canvas.replaceChild(fresh, node);
        else canvas.appendChild(fresh);
        node = fresh;
        canvas.scrollTop = canvas.scrollHeight;
      });
    }

    var history = state.messages.slice(0, -1).filter(function (m) {
      return m.content && m.content.trim();
    }).slice(-MAX_TURNS).map(function (m) {
      return { role: m.role, content: m.content.slice(0, 32000) };
    });

    state.controller = new AbortController();
    setBusy(true);
    repaint();

    function onEvent(ev) {
      var m = reply.meta;
      switch (ev.type) {
        case "meta": m.route = ev.route; m.model = ev.model; m.connection = ev.connection; break;
        case "notice": m.notices.push(ev.message); break;
        case "decision": m.decision = ev; break;
        case "reasoning": m.reasoning += ev.text || ""; break;
        case "delta": reply.content += ev.text || ""; break;
        case "usage": m.completion_tokens = ev.completion_tokens; m.prompt_tokens = ev.prompt_tokens; break;
        case "error": m.error = ev.message || ev.code; break;
        case "tool_call":
          m.tools.push({ id: ev.id, name: ev.name, method: ev.method, path: ev.path, arguments: ev.arguments });
          break;
        case "tool_result":
          var hit = null;
          m.tools.forEach(function (t) { if (t.id === ev.id) hit = t; });
          if (!hit) { hit = { id: ev.id, name: ev.name }; m.tools.push(hit); }
          hit.result = { status: ev.status, ok: ev.ok, elapsed_ms: ev.elapsed_ms,
                         summary: ev.summary, preview: (ev.preview || "").slice(0, 4000),
                         rejected: !!ev.rejected };
          break;
        case "proposal":
          m.proposals.push({ id: ev.id, name: ev.name, method: ev.method, path: ev.path,
                             body: ev.body, state: "pending" });
          break;
        case "open_draft":
          var dp = { id: ev.id, name: ev.name, method: ev.method, path: ev.path,
                     body: ev.body, state: "running", draft: true };
          m.proposals.push(dp);
          state.pendingDrafts.push(dp);
          break;
        case "ui_action":
          // Applied once, live, as the event arrives — never on replay.
          var ua = { id: ev.id, name: ev.name, args: ev.args || {}, state: "pending" };
          applyUiAction(ua);
          m.uiActions.push(ua);
          save();
          break;
        case "done": m.elapsed_ms = ev.elapsed_ms; if (!m.route || m.route === "none") m.route = ev.route; break;
      }
      repaint();
    }

    fetch("/api/v1/llm/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify(uiBody(history)),
      signal: state.controller.signal
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; }).then(function (b) {
          var d = b && b.detail;
          onEvent({ type: "error", message: (typeof d === "string" ? d : JSON.stringify(d || {})) || ("HTTP " + resp.status) });
        });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) { parseSse(buffer + "\n\n", onEvent); return; }
          buffer = parseSse(buffer + decoder.decode(r.value, { stream: true }), onEvent);
          return pump();
        });
      }
      return pump();
    }).catch(function (err) {
      if (err && err.name === "AbortError") reply.meta.notices.push("Stopped.");
      else onEvent({ type: "error", message: (err && err.message) || "Network error" });
    }).then(function () {
      state.controller = null;
      setBusy(false);
      if (!reply.content && !reply.meta.error && !reply.meta.decision &&
          !reply.meta.proposals.length && !reply.meta.uiActions.length) {
        reply.content = "_(no answer)_";
      }
      save();
      repaint();
      var drafts = state.pendingDrafts.splice(0);
      if (drafts.length) { openDraft(drafts[0]); return; }
      var input = $("llm-input");
      if (input) input.focus();
    });
  }

  /* Create a draft job (opens_draft tool) with the user's own session and
     open its run page. A draft is never executed by the worker — the human
     presses the (breathing) ▶ on the run page to launch it. */
  function openDraft(p) {
    if (!/^\/api\/v1\/(job-drafts|pipelines\/[A-Za-z0-9_\-%.]+\/draft)$/.test(p.path || "") ||
        p.method !== "POST") {
      p.state = "failed";
      p.outcome = "Refused: not a draft-job route.";
      save(); renderAll();
      return;
    }
    fetch(p.path, {
      method: "POST", credentials: "same-origin",
      headers: { "Accept": "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(p.body || {})
    }).then(function (resp) {
      return resp.text().then(function (txt) {
        var data;
        try { data = JSON.parse(txt); } catch (e) { data = txt; }
        return { status: resp.status, ok: resp.ok, data: data };
      });
    }).then(function (r) {
      if (r.ok && r.data && r.data.instance_id) {
        var url = "/runs/" + encodeURIComponent(r.data.instance_id);
        p.state = "done";
        p.url = url;
        p.outcome = "Draft created — launch it with ▶ on the run page; its live status is shown there.";
        save(); renderAll();
        window.location.assign(url);
        return;
      }
      var d = r.data && r.data.detail;
      p.state = "failed";
      p.outcome = "HTTP " + r.status + (d ? ": " + (typeof d === "string" ? d : JSON.stringify(d)) : "");
      save(); renderAll();
      send("[Creating the draft `" + p.name + "` failed → " + p.outcome + "]\n" +
           "Fix the arguments using read tools and draft it again.", { kind: "action" });
    }).catch(function (err) {
      p.state = "failed";
      p.outcome = (err && err.message) || "Network error";
      save(); renderAll();
    });
  }

  /* ---------------- panel controller ---------------- */

  function setExpanded(on) {
    var b = $("tool-llm");
    if (b) { b.setAttribute("aria-expanded", on ? "true" : "false"); b.classList.toggle("active", on); }
    var p = $("llm-panel");
    if (p) p.setAttribute("aria-hidden", on ? "false" : "true");
  }

  function open() {
    if (window.Ovid2Appearance && html.classList.contains("right-panel-open")) {
      window.Ovid2Appearance.close();
    }
    html.classList.add("llm-panel-open");
    try { localStorage.setItem(OPEN_KEY, "1"); } catch (e) { /* ignore */ }
    setExpanded(true);
    fetchStatus();
    renderAll();
    var input = $("llm-input");
    if (input) window.setTimeout(function () { input.focus(); }, 50);
  }

  function close() {
    html.classList.remove("llm-panel-open");
    try { localStorage.setItem(OPEN_KEY, "0"); } catch (e) { /* ignore */ }
    setExpanded(false);
  }

  function toggle() {
    if (html.classList.contains("llm-panel-open")) close(); else open();
  }

  function init() {
    if (!$("llm-panel")) return;
    load();
    var startsOpen = html.classList.contains("llm-panel-open");
    setExpanded(startsOpen);
    if (startsOpen) { fetchStatus(); renderAll(); }

    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || typeof t.closest !== "function") return;
      if (t.closest("#tool-llm")) { ev.preventDefault(); toggle(); return; }
      if (t.closest("#tool-appearance") && html.classList.contains("llm-panel-open")) { close(); return; }
      if (html.classList.contains("llm-panel-open") &&
          (t.closest("[data-tools-close]") || (t.classList && t.classList.contains("right-scrim")))) {
        close();
      }
    }, true);

    var form = $("llm-form"), input = $("llm-input");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var text = input.value;
      input.value = "";
      send(text);
    });
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit"));
      }
    });
    $("llm-stop").addEventListener("click", function () {
      if (state.controller) state.controller.abort();
    });
    $("llm-clear").addEventListener("click", function () {
      if (state.busy) return;
      state.messages = [];
      save();
      renderAll();
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && html.classList.contains("llm-panel-open") &&
          window.matchMedia && window.matchMedia("(max-width: 899px)").matches) {
        close();
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.Ovid2Llm = { open: open, close: close, toggle: toggle, _markdown: markdown };
})();
