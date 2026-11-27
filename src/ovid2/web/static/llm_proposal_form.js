/* Editable assistant proposal cards (L2 addendum "Proposal forms").
 *
 * The assistant (llm_panel.js) shows a PROPOSE tool call as a card the
 * human runs. For the config-creating proposals listed in FORMS the card
 * is a small form instead of read-only JSON:
 *
 * - the model's arguments are only a PREFILL — every field is editable;
 * - fields come from the same web-safe catalogues the Storage / Address
 *   pages use (GET /api/v1/connections/types for the type list,
 *   /api/v1/atlas/forms/{connection,location}/<type> for the fields with
 *   their choices, /api/v1/connections), so a closed set is a <select>
 *   (UI rule U2) and catalogue help hangs off a "?" tip (U3);
 * - secret fields are password inputs the HUMAN types. Their values live
 *   only in a WeakMap here: never in the proposal body, never in the saved
 *   session meta, never in the follow-up message sent to the model;
 * - Run stays disabled until every required field is filled; missing ones
 *   are highlighted.
 *
 * Renaming a proposed connection / address in its form rewrites the same
 * name in sibling proposals and held drafts of that answer, so the chain
 * (account → address → workflow) stays consistent.
 *
 * Exposes window.Ovid2ProposalForm = { editable, render, request,
 * redactedBody, rename }. Makes only GET catalogue reads itself; the write
 * is executed by llm_panel.js runProposal with the user's own session.
 */
(function () {
  "use strict";

  var NAME_RE = /^[A-Za-z0-9_.-]{1,96}$/;
  var FORMS = {
    create_connection: { base: "/api/v1/connections/" },
    create_address: { base: "/api/v1/addresses/" }
  };

  var secretValues = new WeakMap();   // proposal → {field: value}
  var cache = {};                     // url → Promise<json>

  function getJson(url) {
    if (!cache[url]) {
      cache[url] = fetch(url, { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; });
    }
    return cache[url];
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function tip(text) {
    return window.Ovid2HelpTip && text ? window.Ovid2HelpTip.tip(text) : document.createTextNode("");
  }

  function editable(p) { return !!(p && !p.draft && FORMS[p.name]); }

  /* The working form lives on p.form (non-secret, persisted with the
     session); it is seeded once from the model's body. */
  function formOf(p) {
    if (p.form) return p.form;
    var b = p.body || {};
    var name = "";
    var m = /\/([^\/]+)$/.exec(p.path || "");
    if (m) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
    if (p.name === "create_connection") {
      p.form = { name: name, type: String(b.type || ""), args: Object.assign({}, b.args || {}) };
    } else {
      p.form = { name: name, connection: String(b.connection || ""),
                 location: Object.assign({}, b.location || {}) };
    }
    return p.form;
  }

  function secretsOf(p) {
    var s = secretValues.get(p);
    if (!s) { s = {}; secretValues.set(p, s); }
    return s;
  }

  function present(v) { return !(v === undefined || v === null || String(v).trim() === ""); }

  /* Connection names proposed (not yet created) in the same answer. */
  function siblingConnections(m) {
    var out = [];
    ((m && m.meta && m.meta.proposals) || []).forEach(function (q) {
      if (q.name === "create_connection" && q.form && q.form.name && q.state !== "dismissed") {
        out.push({ name: q.form.name, type: q.form.type, proposed: q.state !== "done" });
      }
    });
    return out;
  }

  function fieldRow(label, required, help, control, missing) {
    var row = el("label", "llm-form-row" + (missing ? " llm-missing" : ""));
    var cap = el("span", "llm-form-label", label + (required ? " *" : ""));
    row.appendChild(cap);
    if (help) cap.appendChild(tip(help));
    row.appendChild(control);
    return row;
  }

  function textInput(value, onInput, opts) {
    var i = document.createElement("input");
    i.type = (opts && opts.secret) ? "password" : "text";
    i.className = "llm-form-input";
    i.value = value == null ? "" : String(value);
    if (opts && opts.placeholder) i.placeholder = opts.placeholder;
    if (opts && opts.secret) i.autocomplete = "new-password";
    i.addEventListener("input", function () { onInput(i.value); });
    i.addEventListener("change", function () { onInput(i.value, true); });
    return i;
  }

  function select(options, value, onChange, blank) {
    var s = document.createElement("select");
    s.className = "llm-form-input";
    if (blank !== false) {
      var o0 = document.createElement("option");
      o0.value = ""; o0.textContent = blank || "— choose —";
      s.appendChild(o0);
    }
    var found = false;
    options.forEach(function (opt) {
      var o = document.createElement("option");
      o.value = opt.value; o.textContent = opt.label || opt.value;
      if (opt.value === value) { o.selected = true; found = true; }
      s.appendChild(o);
    });
    if (!found && present(value)) {
      // A model-suggested value that is not in the catalogue: show it as
      // unavailable instead of silently accepting it.
      var bad = document.createElement("option");
      bad.value = ""; bad.textContent = value + " (not available)";
      bad.selected = true;
      s.insertBefore(bad, s.firstChild.nextSibling);
    }
    s.addEventListener("change", function () { onChange(s.value); });
    return s;
  }

  function specControl(spec, current, onValue, isSecret) {
    var widget = spec.widget || (spec.type === "bool" ? "bool" : "");
    if (!isSecret && widget === "choices" && Array.isArray(spec.choices)) {
      return select(spec.choices.map(function (c) { return { value: String(c) }; }),
                    present(current) ? String(current) : (spec.default != null ? String(spec.default) : ""),
                    onValue, spec.required ? undefined : "— default —");
    }
    if (!isSecret && widget === "bool") {
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = current === true || current === "true" || (current == null && spec.default === true);
      cb.addEventListener("change", function () { onValue(cb.checked); });
      return cb;
    }
    if (spec.multiline && isSecret) {
      var ta = document.createElement("textarea");
      ta.className = "llm-form-input";
      ta.rows = 3;
      ta.value = current == null ? "" : String(current);
      ta.addEventListener("input", function () { onValue(ta.value); });
      return ta;
    }
    return textInput(current, onValue, {
      secret: isSecret, placeholder: spec.default != null ? String(spec.default) : ""
    });
  }

  /* -------------------------------------------------- validation */

  function connectionMissing(p, spec) {
    var f = formOf(p), miss = [];
    if (!NAME_RE.test(f.name || "")) miss.push("name");
    if (!present(f.type)) miss.push("type");
    if (spec) {
      (spec.args || []).forEach(function (a) { if (a.required && !present(f.args[a.name])) miss.push(a.name); });
      var sec = secretsOf(p);
      (spec.secrets || []).forEach(function (s) { if (s.required && !present(sec[s.name])) miss.push(s.name); });
    }
    return miss;
  }

  function addressMissing(p, locSpec) {
    var f = formOf(p), miss = [];
    if (!NAME_RE.test(f.name || "")) miss.push("name");
    if (!present(f.connection)) miss.push("connection");
    (locSpec || []).forEach(function (a) { if (a.required && !present(f.location[a.name])) miss.push(a.name); });
    return miss;
  }

  /* -------------------------------------------------- request building */

  function clean(obj) {
    var out = {};
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k];
      if (v === "" || v === null || v === undefined) return;
      out[k] = v;
    });
    return out;
  }

  /* The exact request Run sends (secrets included — browser only). */
  function request(p) {
    var f = formOf(p);
    var path = FORMS[p.name].base + encodeURIComponent(f.name || "");
    var body;
    if (p.name === "create_connection") {
      body = { type: f.type, args: clean(f.args) };
      var sec = clean(secretsOf(p));
      if (Object.keys(sec).length) body.secrets = sec;
    } else {
      body = { connection: f.connection, location: clean(f.location) };
    }
    // Always the create verb of the /api/v1 route (FORMS[...].base).
    var req = { path: path, body: body };
    req.method = "POST";
    return req;
  }

  /* What may be persisted / shown / sent back to the model. */
  function redactedBody(p) {
    var r = request(p);
    if (r.body.secrets) {
      var masked = {};
      Object.keys(r.body.secrets).forEach(function (k) { masked[k] = "(entered by the user)"; });
      r.body.secrets = masked;
    }
    return r.body;
  }

  /* -------------------------------------------------- rename propagation */

  function replaceDeep(value, from, to) {
    if (typeof value === "string") return value === from ? to : value;
    if (Array.isArray(value)) return value.map(function (v) { return replaceDeep(v, from, to); });
    if (value && typeof value === "object") {
      var out = {};
      Object.keys(value).forEach(function (k) { out[k] = replaceDeep(value[k], from, to); });
      return out;
    }
    return value;
  }

  function rename(m, p, from, to) {
    if (!from || from === to) return;
    ((m && m.meta && m.meta.proposals) || []).forEach(function (q) {
      if (q === p) return;
      if (p.name === "create_connection" && q.name === "create_address") {
        var qf = formOf(q);
        if (qf.connection === from) qf.connection = to;
      }
      if (p.name === "create_address" && q.draft && q.body) {
        q.body = replaceDeep(q.body, from, to);
      }
    });
  }

  /* -------------------------------------------------- rendering */

  /* Render the form into ``host``. ``ctx`` = {m, busy, onChange(), onRun()}.
     Async catalogue reads re-render in place when they land. */
  function render(host, p, ctx) {
    var f = formOf(p);
    var form = el("div", "llm-form");
    host.appendChild(form);
    var actions = el("div", "llm-proposal-actions");
    var run = el("button", "button primary small", "Run");
    run.type = "button";
    var dis = el("button", "button small", "Dismiss");
    dis.type = "button";
    dis.addEventListener("click", function () { ctx.onDismiss(); });
    var status = el("span", "small muted");
    actions.appendChild(run); actions.appendChild(dis); actions.appendChild(status);

    function refresh(miss) {
      run.disabled = ctx.busy() || miss.length > 0;
      status.textContent = miss.length ? "Fill in: " + miss.join(", ") : "";
      run.title = miss.length ? "Complete the highlighted fields first" : "";
    }

    function nameRow(missing) {
      var old = f.name;
      return fieldRow("name", true, "Letters, digits, '-', '_', '.'.",
        textInput(f.name, function (v, committed) {
          f.name = v.trim();
          if (committed) { rename(ctx.m, p, old, f.name); old = f.name; ctx.onChange(true); }
          else ctx.onChange(false);
        }), missing);
    }

    if (p.name === "create_connection") {
      getJson("/api/v1/connections/types").then(function (types) {
        types = types || [];
        var known = types.some(function (t) { return t.type === f.type; });
        // The per-type field catalogue (with ``choices``) comes from the
        // Atlas form endpoint; /connections/types lists the type names + note.
        var note = null;
        types.forEach(function (t) { if (t.type === f.type) note = t.note; });
        return (known ? getJson("/api/v1/atlas/forms/connection/" + encodeURIComponent(f.type))
                      : Promise.resolve(null)).then(function (spec) {
          if (spec) spec.note = note;
          return { types: types, spec: spec };
        });
      }).then(function (res) {
        var types = res.types, spec = res.spec;
        var miss = connectionMissing(p, spec);
        form.innerHTML = "";
        form.appendChild(nameRow(miss.indexOf("name") !== -1));
        form.appendChild(fieldRow("type", true, spec && spec.note,
          select(types.map(function (t) { return { value: t.type }; }), f.type, function (v) {
            f.type = v; ctx.onChange(true);
          }), miss.indexOf("type") !== -1));
        if (spec) {
          var known = {};
          (spec.args || []).forEach(function (a) {
            known[a.name] = true;
            form.appendChild(fieldRow(a.name, a.required, a.help,
              specControl(a, f.args[a.name], function (v) {
                f.args[a.name] = v; ctx.onChange(false); refresh(connectionMissing(p, spec));
              }, false), miss.indexOf(a.name) !== -1));
          });
          Object.keys(f.args).forEach(function (k) { if (!known[k]) delete f.args[k]; });
          var sec = secretsOf(p);
          (spec.secrets || []).forEach(function (s) {
            form.appendChild(fieldRow(s.name + " (secret)", s.required,
              (s.help ? s.help + " " : "") + "Typed here only — never shown to the assistant or saved in the chat.",
              specControl(s, sec[s.name], function (v) {
                sec[s.name] = v; refresh(connectionMissing(p, spec));
              }, true), miss.indexOf(s.name) !== -1));
          });
        }
        refresh(miss);
      });
    } else {
      Promise.all([getJson("/api/v1/connections")]).then(function (res) {
        var conns = (res[0] || []).map(function (c) { return { name: c.name, type: c.type }; });
        siblingConnections(ctx.m).forEach(function (s) {
          if (!conns.some(function (c) { return c.name === s.name; })) conns.push(s);
        });
        var ctype = "";
        conns.forEach(function (c) { if (c.name === f.connection) ctype = c.type; });
        var draw = function (locSpec) {
          var miss = addressMissing(p, locSpec);
          form.innerHTML = "";
          form.appendChild(nameRow(miss.indexOf("name") !== -1));
          form.appendChild(fieldRow("storage account", true,
            "Accounts proposed above are listed too; create them first.",
            select(conns.map(function (c) {
              return { value: c.name, label: c.name + (c.proposed ? " (proposed above)" : "") + (c.type ? " · " + c.type : "") };
            }), f.connection, function (v) { f.connection = v; ctx.onChange(true); }),
            miss.indexOf("connection") !== -1));
          var known = {};
          (locSpec || []).forEach(function (a) {
            known[a.name] = true;
            form.appendChild(fieldRow(a.name, a.required, a.help,
              specControl(a, f.location[a.name], function (v) {
                f.location[a.name] = v; ctx.onChange(false); refresh(addressMissing(p, locSpec));
              }, false), miss.indexOf(a.name) !== -1));
          });
          if (locSpec) Object.keys(f.location).forEach(function (k) { if (!known[k]) delete f.location[k]; });
          refresh(miss);
        };
        if (!ctype) { draw(null); return; }
        getJson("/api/v1/atlas/forms/location/" + encodeURIComponent(ctype)).then(function (spec) {
          draw((spec && spec.args) || []);
        });
      });
    }

    run.addEventListener("click", function () {
      if (run.disabled) return;
      ctx.onRun(request(p));
    });
    host.appendChild(actions);
    refresh(["…"]);
  }

  window.Ovid2ProposalForm = {
    editable: editable,
    render: render,
    request: request,
    redactedBody: redactedBody,
    rename: rename,
    _forTests: { formOf: formOf, connectionMissing: connectionMissing, addressMissing: addressMissing }
  };
})();
