/*
 * Ovid2 Manual — behaviour shared by the web panel and the static site.
 *
 *  - Search: lazily fetches the JSON index (data-om-search-url) on first
 *    focus, scores records client-side (title > heading > body; prefix
 *    match per term; all terms must match), keyboard navigable
 *    (↑ ↓ Enter Esc). "/" focuses the box from anywhere on the page.
 *  - Copy buttons on every code block.
 *  - On-this-page scroll-spy (IntersectionObserver).
 *
 * No dependencies, no storage, no network beyond the same-origin index.
 */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  /* ---------------- search ---------------- */

  function tokenize(q) {
    return q.toLowerCase().split(/[^\p{L}\p{N}_]+/u).filter(function (t) { return t.length > 1; });
  }

  function scoreDoc(doc, terms) {
    var t = doc._t, h = doc._h, x = doc._x;
    var score = 0;
    for (var i = 0; i < terms.length; i++) {
      var term = terms[i], s = 0;
      if (t.indexOf(term) !== -1) s += 12;
      if (h.indexOf(term) !== -1) s += 8;
      var pos = x.indexOf(term);
      if (pos !== -1) {
        s += 2;
        var hits = 0, at = pos;
        while (at !== -1 && hits < 5) { hits++; at = x.indexOf(term, at + term.length); }
        s += hits * 0.4;
      }
      if (!s) return 0;
      score += s;
    }
    if (!doc.h) score += 1.5; // page intro beats deep sections on ties
    return score;
  }

  function snippet(text, terms) {
    var lower = text.toLowerCase(), at = -1;
    for (var i = 0; i < terms.length && at === -1; i++) at = lower.indexOf(terms[i]);
    var start = Math.max(0, at - 50);
    var out = (start ? "… " : "") + text.slice(start, start + 160) + (text.length > start + 160 ? " …" : "");
    var html = escapeHtml(out);
    terms.forEach(function (term) {
      var re = new RegExp("(" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      html = html.replace(re, "<mark>$1</mark>");
    });
    return html;
  }

  function initSearch(root) {
    var box = root.querySelector("[data-om-search]");
    if (!box) return;
    var input = box.querySelector("input");
    var list = box.querySelector("ol");
    var base = root.getAttribute("data-om-root") || "";
    var url = root.getAttribute("data-om-search-url");
    var suffix = root.getAttribute("data-om-suffix") || "";
    var index = null, loading = null, selected = -1;

    function load() {
      if (index || loading) return loading;
      loading = fetch(url, { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          index = (data.docs || []).map(function (d) {
            d._t = d.t.toLowerCase(); d._h = (d.h || "").toLowerCase(); d._x = (d.x || "").toLowerCase();
            return d;
          });
          return index;
        })
        .catch(function () { index = []; return index; });
      return loading;
    }

    function hrefFor(u) {
      // Index URLs are "<chapter>/<page>#anchor"; join to the surface root.
      var hash = "", at = u.indexOf("#");
      if (at !== -1) { hash = u.slice(at); u = u.slice(0, at); }
      return base + u + suffix + hash;
    }

    function close() {
      list.hidden = true; selected = -1;
      input.setAttribute("aria-expanded", "false");
    }

    function render() {
      var q = input.value.trim();
      var terms = tokenize(q);
      if (!terms.length) { close(); return; }
      var hits = [];
      (index || []).forEach(function (d) {
        var s = scoreDoc(d, terms);
        if (s) hits.push([s, d]);
      });
      hits.sort(function (a, b) { return b[0] - a[0]; });
      hits = hits.slice(0, 12);
      if (!hits.length) {
        list.innerHTML = '<li class="om-sr-empty">No results for “' + escapeHtml(q) + '”</li>';
      } else {
        list.innerHTML = hits.map(function (h, i) {
          var d = h[1];
          return '<li role="option" id="om-sr-' + i + '"><a href="' + escapeHtml(hrefFor(d.u)) + '">' +
            '<span class="om-sr-title">' + escapeHtml(d.h || d.t) + "</span>" +
            '<span class="om-sr-path">' + escapeHtml(d.c + (d.h ? " › " + d.t : "")) + "</span>" +
            '<span class="om-sr-snip">' + snippet(d.x || "", terms) + "</span></a></li>";
        }).join("");
      }
      selected = -1;
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function move(delta) {
      var items = list.querySelectorAll("li[role=option]");
      if (!items.length) return;
      if (selected >= 0) items[selected].removeAttribute("aria-selected");
      selected = (selected + delta + items.length) % items.length;
      items[selected].setAttribute("aria-selected", "true");
      items[selected].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", items[selected].id);
    }

    input.addEventListener("focus", load);
    input.addEventListener("input", function () {
      var p = load();
      if (p && !index) p.then(render); else render();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") {
        var items = list.querySelectorAll("li[role=option] a");
        var target = items[selected >= 0 ? selected : 0];
        if (target) { e.preventDefault(); window.location.href = target.getAttribute("href"); }
      } else if (e.key === "Escape") { close(); input.blur(); }
    });
    document.addEventListener("click", function (e) { if (!box.contains(e.target)) close(); });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (document.activeElement && document.activeElement.isContentEditable)) return;
      e.preventDefault();
      input.focus();
    });
  }

  /* ---------------- copy buttons ---------------- */

  function initCopy(root) {
    root.querySelectorAll(".om-content pre.code").forEach(function (pre) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "om-copy";
      btn.textContent = "copy";
      btn.setAttribute("aria-label", "Copy code to clipboard");
      btn.addEventListener("click", function () {
        var code = pre.querySelector("code");
        var text = code ? code.innerText : pre.innerText;
        var done = function () { btn.textContent = "copied"; setTimeout(function () { btn.textContent = "copy"; }, 1400); };
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(text).then(done, function () { fallback(text); done(); });
        } else { fallback(text); done(); }
      });
      pre.appendChild(btn);
    });
    function fallback(text) {
      var ta = document.createElement("textarea");
      ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) { /* ignore */ }
      document.body.removeChild(ta);
    }
  }

  /* ---------------- scroll-spy ---------------- */

  function initSpy(root) {
    var links = root.querySelectorAll("[data-om-toc]");
    if (!links.length || !("IntersectionObserver" in window)) return;
    var byId = {};
    links.forEach(function (a) { byId[a.getAttribute("data-om-toc")] = a; });
    var visible = {};
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { visible[en.target.id] = en.isIntersecting; });
      var current = null;
      for (var i = 0; i < headings.length; i++) {
        if (visible[headings[i].id]) { current = headings[i].id; break; }
      }
      if (!current) return;
      links.forEach(function (a) { a.classList.toggle("is-active", a.getAttribute("data-om-toc") === current); });
    }, { rootMargin: "-10% 0px -70% 0px" });
    var headings = [];
    Object.keys(byId).forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { headings.push(el); obs.observe(el); }
    });
  }

  ready(function () {
    document.querySelectorAll("[data-om]").forEach(function (root) {
      initSearch(root);
      initCopy(root);
      initSpy(root);
    });
  });
})();
