/*
 * Ovid2 control panel — command palette (Ctrl-K).
 *
 * A strictly client-only, zero-backend fuzzy launcher living in the
 * right tools rail beside the Appearance tool. It does two things:
 *
 *   1. Builds its search index at open time from the navigation links
 *      already rendered in the left drawer (`#site-nav a.nav-item`).
 *      Because those links are gated by RBAC capabilities in base.html,
 *      the palette inherits the same access control for free — it can
 *      only ever surface destinations the current user can already see.
 *
 *   2. Keeps a small "recent pages" list (the last handful of
 *      destinations the user jumped to) in localStorage
 *      (`ovid2RecentPages`) so the palette opens with useful shortcuts
 *      even before the user types.
 *
 * Interaction: Ctrl-K / Cmd-K (or the rail search button) toggles a
 * centred modal dialog with a text input and a ranked result list.
 * Arrow keys move the active row, Enter navigates (recording the
 * destination), Escape closes, and a backdrop click closes. Focus is
 * trapped in the input while open and restored to the trigger on close.
 *
 * No third-party imports; classic (non-module) script loaded with
 * <script defer> after appearance.js. Mirrors the localStorage-guard
 * and window-namespace conventions of appearance.js. Exposes
 * window.Ovid2Palette = { open, close, toggle }.
 */

(function () {
  "use strict";

  var RECENT_KEY = "ovid2RecentPages";
  var RECENT_MAX = 6;

  var html = document.documentElement;

  /* The element that had focus before the palette opened, so we can
   * restore it on close (accessibility). */
  var lastFocus = null;

  /* Currently-rendered result rows and the active (highlighted) index. */
  var currentResults = [];
  var activeIndex = 0;

  var mobileMql = null;
  try {
    mobileMql = window.matchMedia("(max-width: 899.98px)");
  } catch (e) {
    /* ignore */
  }

  /* ---- small DOM helper (mirrors appearance.js el()) --------------- */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    if (text != null) {
      node.textContent = text;
    }
    return node;
  }

  /* ---- persistence ------------------------------------------------- */

  function readRecents() {
    try {
      var raw = window.localStorage.getItem(RECENT_KEY);
      if (!raw) {
        return [];
      }
      var parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      return [];
    }
  }

  function recordRecent(href, label) {
    if (!href) {
      return;
    }
    var next = [{ href: href, label: label || href }];
    var prior = readRecents();
    for (var i = 0; i < prior.length; i += 1) {
      if (prior[i] && prior[i].href !== href) {
        next.push(prior[i]);
      }
      if (next.length >= RECENT_MAX) {
        break;
      }
    }
    try {
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(next));
    } catch (e) {
      /* ignore quota / disabled storage */
    }
  }

  /* ---- index building --------------------------------------------- */

  /* Snapshot the drawer's nav links into a flat command list. RBAC is
   * enforced upstream: links the user lacks a capability for are never
   * rendered, so they never enter the index. */
  function buildIndex() {
    var items = [];
    var seen = {};
    var links = document.querySelectorAll("#site-nav a.nav-item");
    for (var i = 0; i < links.length; i += 1) {
      var a = links[i];
      var href = a.getAttribute("href");
      if (!href || href === "#") {
        continue;
      }
      var labelNode = a.querySelector(".nav-label");
      var label = (labelNode ? labelNode.textContent : a.textContent) || href;
      label = label.trim();
      if (seen[href]) {
        continue;
      }
      seen[href] = true;
      items.push({ href: href, label: label });
    }
    return items;
  }

  /* ---- fuzzy matching --------------------------------------------- */

  /* Subsequence match with a light score: contiguous runs and matches
   * at word boundaries rank higher. Returns null when the query is not
   * a subsequence of the candidate. */
  function fuzzyScore(query, text) {
    if (!query) {
      return 0;
    }
    var q = query.toLowerCase();
    var t = text.toLowerCase();
    var qi = 0;
    var score = 0;
    var streak = 0;
    var prevWasSep = true;
    for (var ti = 0; ti < t.length && qi < q.length; ti += 1) {
      var ch = t.charAt(ti);
      if (ch === q.charAt(qi)) {
        score += 1 + streak;
        if (prevWasSep) {
          score += 2;
        }
        if (ti === qi) {
          score += 1;
        }
        streak += 1;
        qi += 1;
      } else {
        streak = 0;
      }
      prevWasSep = ch === " " || ch === "-" || ch === "/" || ch === "_";
    }
    return qi === q.length ? score : null;
  }

  function rankItems(query, index) {
    if (!query) {
      var recents = readRecents();
      if (recents.length) {
        var known = {};
        for (var r = 0; r < index.length; r += 1) {
          known[index[r].href] = index[r].label;
        }
        var out = [];
        var used = {};
        for (var i = 0; i < recents.length; i += 1) {
          var href = recents[i] && recents[i].href;
          if (href && known[href] && !used[href]) {
            out.push({ href: href, label: known[href] });
            used[href] = true;
          }
        }
        for (var j = 0; j < index.length; j += 1) {
          if (!used[index[j].href]) {
            out.push(index[j]);
          }
        }
        return out;
      }
      return index.slice();
    }
    var scored = [];
    for (var k = 0; k < index.length; k += 1) {
      var s = fuzzyScore(query, index[k].label);
      if (s !== null) {
        scored.push({ item: index[k], score: s });
      }
    }
    scored.sort(function (a, b) {
      return b.score - a.score;
    });
    return scored.map(function (entry) {
      return entry.item;
    });
  }

  /* ---- rendering --------------------------------------------------- */

  function container() {
    return document.getElementById("command-palette");
  }

  function renderResults(results) {
    currentResults = results;
    if (activeIndex >= results.length) {
      activeIndex = results.length ? results.length - 1 : 0;
    }
    if (activeIndex < 0) {
      activeIndex = 0;
    }
    var list = container().querySelector(".cmdk-results");
    list.innerHTML = "";
    if (!results.length) {
      var empty = el("li", "cmdk-empty", "No matching pages");
      empty.setAttribute("role", "presentation");
      list.appendChild(empty);
      return;
    }
    for (var i = 0; i < results.length; i += 1) {
      var row = el("li", "cmdk-row", null);
      row.setAttribute("role", "option");
      row.setAttribute("data-index", String(i));
      row.setAttribute("data-href", results[i].href);
      if (i === activeIndex) {
        row.classList.add("active");
        row.setAttribute("aria-selected", "true");
      }
      row.appendChild(el("span", "cmdk-row-label", results[i].label));
      row.appendChild(el("span", "cmdk-row-href", results[i].href));
      list.appendChild(row);
    }
  }

  function highlightActive() {
    var rows = container().querySelectorAll(".cmdk-row");
    for (var i = 0; i < rows.length; i += 1) {
      var on = i === activeIndex;
      rows[i].classList.toggle("active", on);
      if (on) {
        rows[i].setAttribute("aria-selected", "true");
        rows[i].scrollIntoView({ block: "nearest" });
      } else {
        rows[i].removeAttribute("aria-selected");
      }
    }
  }

  function buildShell(index) {
    var root = container();
    root.innerHTML = "";

    var backdrop = el("div", "cmdk-backdrop", null);
    backdrop.setAttribute("data-cmdk-close", "");
    root.appendChild(backdrop);

    var box = el("div", "cmdk-box", null);

    var input = el("input", "cmdk-input", null);
    input.type = "text";
    input.setAttribute("placeholder", "Search pages…  (\u2191\u2193 to move, Enter to open)");
    input.setAttribute("aria-label", "Search pages");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("spellcheck", "false");
    box.appendChild(input);

    var list = el("ul", "cmdk-results", null);
    list.setAttribute("role", "listbox");
    box.appendChild(list);

    root.appendChild(box);

    input.addEventListener("input", function () {
      activeIndex = 0;
      renderResults(rankItems(input.value.trim(), index));
    });

    list.addEventListener("click", function (ev) {
      var target = ev.target;
      var row = target && typeof target.closest === "function"
        ? target.closest(".cmdk-row")
        : null;
      if (row) {
        activeIndex = parseInt(row.getAttribute("data-index"), 10) || 0;
        activate();
      }
    });

    renderResults(rankItems("", index));
    return input;
  }

  /* ---- navigation -------------------------------------------------- */

  function activate() {
    var choice = currentResults[activeIndex];
    if (!choice) {
      return;
    }
    recordRecent(choice.href, choice.label);
    closePalette();
    window.location.href = choice.href;
  }

  /* ---- open / close ------------------------------------------------ */

  function setTriggerExpanded(on) {
    var btn = document.getElementById("tool-palette");
    if (btn) {
      btn.setAttribute("aria-expanded", on ? "true" : "false");
      btn.classList.toggle("active", on);
    }
  }

  function isOpen() {
    return html.classList.contains("cmdk-open");
  }

  function openPalette() {
    if (isOpen()) {
      return;
    }
    lastFocus = document.activeElement;
    var input = buildShell(buildIndex());
    container().setAttribute("aria-hidden", "false");
    html.classList.add("cmdk-open");
    setTriggerExpanded(true);
    // Focus after the class flips so the element is displayed.
    window.setTimeout(function () {
      try {
        input.focus();
        input.select();
      } catch (e) {
        /* ignore */
      }
    }, 0);
  }

  function closePalette() {
    if (!isOpen()) {
      return;
    }
    html.classList.remove("cmdk-open");
    container().setAttribute("aria-hidden", "true");
    container().innerHTML = "";
    setTriggerExpanded(false);
    currentResults = [];
    activeIndex = 0;
    if (lastFocus && typeof lastFocus.focus === "function") {
      try {
        lastFocus.focus();
      } catch (e) {
        /* ignore */
      }
    }
    lastFocus = null;
  }

  function togglePalette() {
    if (isOpen()) {
      closePalette();
    } else {
      openPalette();
    }
  }

  /* ---- wiring ------------------------------------------------------ */

  function moveActive(delta) {
    if (!currentResults.length) {
      return;
    }
    activeIndex = (activeIndex + delta + currentResults.length) % currentResults.length;
    highlightActive();
  }

  function init() {
    // Rail button toggles the palette.
    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (!t || typeof t.closest !== "function") {
        return;
      }
      if (t.closest("#tool-palette")) {
        ev.preventDefault();
        togglePalette();
        return;
      }
      if (isOpen() && t.closest("[data-cmdk-close]")) {
        closePalette();
      }
    });

    // Global keyboard: Ctrl/Cmd-K toggles; palette-scoped keys navigate.
    document.addEventListener("keydown", function (ev) {
      var key = (ev.key || "").toLowerCase();
      if (key === "k" && (ev.metaKey || ev.ctrlKey)) {
        ev.preventDefault();
        togglePalette();
        return;
      }
      if (!isOpen()) {
        return;
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        closePalette();
      } else if (ev.key === "ArrowDown") {
        ev.preventDefault();
        moveActive(1);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        moveActive(-1);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        activate();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.Ovid2Palette = {
    open: openPalette,
    close: closePalette,
    toggle: togglePalette
  };
})();
