/*
 * Ovid2 control panel — console command palette / navigator.
 *
 * Reads a server-rendered JSON target list from
 * ``#ovid2-palette-data`` and turns the bottom command box into a
 * client-side navigator: type to fuzzy/substring-match pages and
 * recent runs, arrow keys to move the highlight, Enter to navigate,
 * Escape to clear. No backend call, no dependency.
 *
 * Everything is guarded so a missing/blank data element degrades to a
 * no-op — the server-rendered panels and links keep working. The boot
 * is idempotent (a repeated import or HTMX re-init cannot double-wire).
 *
 * This is the seat for the future LLM chat; the shape here
 * (input + results list) is deliberately chat-ready.
 */

"use strict";

const MAX_RESULTS = 8;

function parseTargets() {
  const node = document.getElementById("ovid2-palette-data");
  if (!node) return [];
  try {
    const parsed = JSON.parse(node.textContent || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    return [];
  }
}

function scoreMatch(target, query) {
  const haystack = ((target.label || "") + " " + (target.keywords || "")).toLowerCase();
  const idx = haystack.indexOf(query);
  if (idx === -1) return -1;
  // Earlier matches and label-prefix matches rank higher.
  const labelPrefix = (target.label || "").toLowerCase().startsWith(query) ? 100 : 0;
  return labelPrefix - idx;
}

function filterTargets(targets, rawQuery) {
  const query = rawQuery.trim().toLowerCase();
  if (!query) return [];
  return targets
    .map((t) => ({ target: t, score: scoreMatch(t, query) }))
    .filter((x) => x.score >= 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, MAX_RESULTS)
    .map((x) => x.target);
}

function init() {
  const form = document.querySelector(".console-cmd");
  const input = document.getElementById("console-cmd-input");
  const results = document.getElementById("console-cmd-results");
  if (!form || !input || !results) return;
  if (form.dataset.paletteReady === "1") return;
  form.dataset.paletteReady = "1";

  const targets = parseTargets();
  let matches = [];
  let highlight = -1;

  function closeResults() {
    matches = [];
    highlight = -1;
    results.innerHTML = "";
    results.hidden = true;
  }

  function render() {
    results.innerHTML = "";
    if (matches.length === 0) {
      results.hidden = true;
      return;
    }
    matches.forEach((target, i) => {
      const li = document.createElement("li");
      li.className = "console-cmd-option" + (i === highlight ? " is-active" : "");
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", i === highlight ? "true" : "false");
      li.dataset.url = target.url || "";
      const label = document.createElement("span");
      label.className = "console-cmd-option-label";
      label.textContent = target.label || target.url || "";
      const kind = document.createElement("span");
      kind.className = "console-cmd-option-kind";
      kind.textContent = target.kind || "page";
      li.appendChild(label);
      li.appendChild(kind);
      li.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        navigate(target.url);
      });
      results.appendChild(li);
    });
    results.hidden = false;
  }

  function navigate(url) {
    if (url) window.location.assign(url);
  }

  function chosenUrl() {
    if (highlight >= 0 && matches[highlight]) return matches[highlight].url;
    if (matches.length > 0) return matches[0].url;
    // Fall back to a bare verb: "submit foo" jumps to the submit page.
    const first = input.value.trim().toLowerCase().split(/\s+/)[0];
    const verb = targets.find(
      (t) => (t.label || "").toLowerCase().split(/\s+/)[0] === first
    );
    return verb ? verb.url : null;
  }

  input.addEventListener("input", () => {
    matches = filterTargets(targets, input.value);
    highlight = matches.length > 0 ? 0 : -1;
    render();
  });

  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      if (matches.length === 0) return;
      ev.preventDefault();
      highlight = (highlight + 1) % matches.length;
      render();
    } else if (ev.key === "ArrowUp") {
      if (matches.length === 0) return;
      ev.preventDefault();
      highlight = (highlight - 1 + matches.length) % matches.length;
      render();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      navigate(chosenUrl());
    } else if (ev.key === "Escape") {
      input.value = "";
      closeResults();
    }
  });

  input.addEventListener("blur", () => {
    // Delay so a mousedown on a result still fires before we hide it.
    window.setTimeout(closeResults, 120);
  });
}

try {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
} catch (e) {
  /* palette is optional — never break the page */
}
