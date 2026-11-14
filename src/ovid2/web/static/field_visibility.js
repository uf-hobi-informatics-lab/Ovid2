/*
 * Declarative field visibility — UI rule U1 ("only relevant fields are
 * shown"). One implementation for every pipeline create / edit / run form.
 *
 * Mark a row / block with one or more of:
 *
 *   data-show-kind="copy move"          visible only for these kinds
 *   data-show-kind-flag="uses_cdm"       visible when every listed KindSpec
 *                                        flag holds. Terms are space-separated:
 *                                        ``flag`` (truthy), ``!flag`` (falsy),
 *                                        ``flag=value`` / ``flag!=value``
 *                                        (e.g. ``tables_mode!=none``);
 *                                        ``a|b`` holds when either flag does.
 *   data-show-engine-family="snowpark"   visible only for these engine families
 *   data-hide-engine-family="snowpark"   hidden for these engine families
 *   data-show-when="trigger_type=watch"  visible when a sibling field matches;
 *                                        ``name=v1|v2`` or ``name!=v``; several
 *                                        space-separated terms must all hold.
 *
 * The root element carries ``data-visibility-root`` and optionally
 * ``data-kind-field`` (default ``kind``) and ``data-engine-field`` (default
 * ``engine``). Kind flags come from ``<script type="application/json"
 * id="kind-flags">`` (``ovid2.web.kinds.kind_choices()``); an engine's
 * family comes from its ``<option>`` / radio ``data-family`` attribute.
 *
 * Hidden elements get the ``hidden`` attribute (never ``style.display``)
 * and their ``required`` controls are relaxed so browser validation never
 * blocks a submit on a field the operator cannot see. An element that also
 * carries ``data-disable-hidden`` has its controls *disabled* while hidden
 * (so two alternative widgets can share one form ``name``). Fires
 * ``ovid2:visibility-change`` on the root after every pass.
 *
 * Classic script; exposes ``window.Ovid2Visibility``. No dependencies.
 */
(function () {
  "use strict";

  var SELECTOR = "[data-show-kind],[data-show-kind-flag],[data-show-engine-family]," +
    "[data-hide-engine-family],[data-show-when]";

  function readKinds() {
    var node = document.getElementById("kind-flags");
    var out = {};
    if (!node) return out;
    try {
      JSON.parse(node.textContent || "[]").forEach(function (k) { out[k.name] = k; });
    } catch (_e) { /* keep empty */ }
    return out;
  }

  function words(s) { return String(s || "").split(/\s+/).filter(Boolean); }

  function fieldValue(root, name) {
    var nodes = root.querySelectorAll('[name="' + name + '"]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.type === "radio" || n.type === "checkbox") {
        if (n.checked) return String(n.value || "");
        continue;
      }
      return String(n.value || "");
    }
    return "";
  }

  function selectedEngineNode(root, name) {
    var nodes = root.querySelectorAll('[name="' + name + '"]');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.tagName === "SELECT") return n.options[n.selectedIndex] || null;
      if ((n.type === "radio" && n.checked) || n.type !== "radio") return n;
    }
    return null;
  }

  function flagHolds(spec, term) {
    if (!spec) return true;
    if (term.indexOf("|") >= 0 && term.indexOf("=") < 0) {
      return term.split("|").some(function (t) { return flagHolds(spec, t); });
    }
    var neg = term.charAt(0) === "!";
    var m = term.match(/^!?([A-Za-z_]+)(!?=)(.*)$/);
    if (m) {
      var cur = String(spec[m[1]]);
      var vals = m[3].split("|");
      var hit = vals.indexOf(cur) >= 0;
      return m[2] === "=" ? hit : !hit;
    }
    var key = neg ? term.slice(1) : term;
    var v = Boolean(spec[key]);
    return neg ? !v : v;
  }

  function whenHolds(root, term) {
    var m = term.match(/^([A-Za-z0-9_.-]+)(!?=)(.*)$/);
    if (!m) return true;
    var cur = fieldValue(root, m[1]);
    var hit = m[3].split("|").indexOf(cur) >= 0;
    return m[2] === "=" ? hit : !hit;
  }

  function setVisible(el, on) {
    var disable = el.hasAttribute("data-disable-hidden");
    if (el.hidden === !on && !disable) return;
    el.hidden = !on;
    var controls = el.querySelectorAll("input,select,textarea");
    for (var i = 0; i < controls.length; i++) {
      var c = controls[i];
      if (!on && c.required) { c.dataset.visRequired = "1"; c.required = false; }
      else if (on && c.dataset.visRequired === "1") { c.required = true; delete c.dataset.visRequired; }
      if (disable) c.disabled = !on;
    }
  }

  function Visibility(root) {
    this.root = root;
    this.kinds = readKinds();
    this.kindField = root.getAttribute("data-kind-field") || "kind";
    this.engineField = root.getAttribute("data-engine-field") || "engine";
    var self = this;
    root.addEventListener("change", function () { self.apply(); });
    root.addEventListener("input", function (e) {
      if (e.target && e.target.tagName === "SELECT") self.apply();
    });
    this.apply();
  }

  Visibility.prototype.kind = function () {
    return fieldValue(this.root, this.kindField) || this.root.getAttribute("data-kind") || "etl";
  };

  Visibility.prototype.engineFamily = function () {
    var n = selectedEngineNode(this.root, this.engineField);
    return n ? String(n.getAttribute("data-family") || "") : "";
  };

  Visibility.prototype.apply = function () {
    var root = this.root;
    var kind = this.kind();
    var spec = this.kinds[kind] || null;
    var fam = this.engineFamily();
    var els = root.querySelectorAll(SELECTOR);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var on = true;
      var a = el.getAttribute("data-show-kind");
      if (a != null) on = on && words(a).indexOf(kind) >= 0;
      a = el.getAttribute("data-show-kind-flag");
      if (a != null) on = on && words(a).every(function (t) { return flagHolds(spec, t); });
      a = el.getAttribute("data-show-engine-family");
      if (a != null) on = on && (!fam || words(a).indexOf(fam) >= 0);
      a = el.getAttribute("data-hide-engine-family");
      if (a != null) on = on && !(fam && words(a).indexOf(fam) >= 0);
      a = el.getAttribute("data-show-when");
      if (a != null) on = on && words(a).every(function (t) { return whenHolds(root, t); });
      setVisible(el, on);
    }
    root.setAttribute("data-kind", kind);
    root.dispatchEvent(new CustomEvent("ovid2:visibility-change", {
      bubbles: true, detail: { kind: kind, spec: spec, engineFamily: fam },
    }));
  };

  var api = {
    attach: function (root) {
      if (!root) return null;
      if (!root.__ovid2Visibility) root.__ovid2Visibility = new Visibility(root);
      return root.__ovid2Visibility;
    },
    refresh: function (root) {
      var roots = root ? [root] : document.querySelectorAll("[data-visibility-root]");
      for (var i = 0; i < roots.length; i++) {
        var v = api.attach(roots[i]);
        if (v) v.apply();
      }
    },
    kindSpec: function (name) { return readKinds()[name] || null; },
  };
  window.Ovid2Visibility = api;

  function boot() {
    var roots = document.querySelectorAll("[data-visibility-root]");
    for (var i = 0; i < roots.length; i++) api.attach(roots[i]);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
