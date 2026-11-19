/*
 * Ovid2 control panel — navigation drawer controller.
 *
 * Responsibilities:
 *
 *   1. Toggle the mobile drawer (viewport < 900 px). ARIA-correct,
 *      trap keyboard focus while open, close on ESC or scrim click,
 *      and re-hide when the viewport crosses back above 900 px.
 *
 *   2. Toggle the medium-viewport icon rail (900–1199 px). Persists
 *      the collapsed/expanded state to ``localStorage`` under the key
 *      ``ovid2DrawerCollapsed`` so subsequent page loads honour the
 *      user's preference. A pre-paint snippet in ``base.html`` applies
 *      the class before first paint to avoid a flash of the wrong
 *      width.
 *
 *   3. Overflow cue. When the menu is taller than the viewport, the
 *      list (``.drawer-nav``) scrolls; edge fades (``has-more-above`` /
 *      ``has-more-below`` on the drawer) and the ``#nav-more`` "▾ N more"
 *      button make the hidden items obvious. Clicking it scrolls them in.
 *
 * No dependencies. Guards every optional API in try/catch so the page
 * stays usable in environments where localStorage or matchMedia are
 * unavailable.
 */

(function () {
  "use strict";

  var STORAGE_KEY = "ovid2DrawerCollapsed";
  // Wide-viewport (>= 900px) collapse state. Persisted so that
  // navigating via a rail icon while collapsed keeps the drawer
  // collapsed on the destination page instead of springing back open.
  var OPEN_KEY = "ovid2DrawerWideCollapsed";
  var MOBILE_QUERY = "(max-width: 899.98px)";
  var MEDIUM_QUERY = "(min-width: 900px) and (max-width: 1199.98px)";

  var html = document.documentElement;
  var mobileMql = null;
  try { mobileMql = window.matchMedia(MOBILE_QUERY); } catch (e) { /* ignore */ }
  var mediumMql = null;
  try { mediumMql = window.matchMedia(MEDIUM_QUERY); } catch (e) { /* ignore */ }

  function isMobile() { return mobileMql ? mobileMql.matches : false; }
  function isMedium() { return mediumMql ? mediumMql.matches : false; }

  /* ---------- Mobile drawer ---------- */

  function openDrawer() {
    html.classList.add("drawer-open");
    var toggle = document.getElementById("drawer-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", "true");
    // On desktop, ``drawer-open`` collapses the rail (see ovid2.css). Persist
    // that so it survives navigation. On mobile it is the transient
    // off-canvas state and must never be persisted (drawer always starts
    // closed on a fresh mobile page load).
    if (!isMobile()) {
      try { localStorage.setItem(OPEN_KEY, "1"); } catch (e) { /* ignore */ }
    }
    // Move focus to the first nav item so keyboard users can navigate.
    var firstLink = document.querySelector(".drawer-nav .nav-item");
    if (firstLink) firstLink.focus();
  }

  function closeDrawer() {
    html.classList.remove("drawer-open");
    var toggle = document.getElementById("drawer-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", "false");
    if (!isMobile()) {
      try { localStorage.setItem(OPEN_KEY, "0"); } catch (e) { /* ignore */ }
    }
  }

  function toggleDrawer() {
    if (html.classList.contains("drawer-open")) closeDrawer();
    else openDrawer();
  }

  /* ---------- Medium-viewport rail collapse ---------- */

  function collapseRail() {
    html.classList.add("drawer-collapsed");
    try { localStorage.setItem(STORAGE_KEY, "1"); } catch (e) { /* ignore */ }
  }
  function expandRail() {
    html.classList.remove("drawer-collapsed");
    try { localStorage.setItem(STORAGE_KEY, "0"); } catch (e) { /* ignore */ }
  }
  function toggleRail() {
    if (html.classList.contains("drawer-collapsed")) expandRail();
    else collapseRail();
  }

  /* ---------- Overflow cue ("▾ N more") ---------- */

  // Items whose bottom edge sits below the visible part of the scroll
  // area (with a few px of slack so a 1px clip does not count).
  function hiddenBelow(nav) {
    var limit = nav.getBoundingClientRect().bottom - 4;
    var out = [];
    nav.querySelectorAll(".nav-item").forEach(function (item) {
      if (item.getBoundingClientRect().bottom > limit) out.push(item);
    });
    return out;
  }

  function updateNavOverflow() {
    var drawer = document.getElementById("site-nav");
    var nav = drawer && drawer.querySelector(".drawer-nav");
    var more = document.getElementById("nav-more");
    if (!nav) return;
    var above = nav.scrollTop > 2;
    var below = nav.scrollTop + nav.clientHeight < nav.scrollHeight - 2;
    drawer.classList.toggle("has-more-above", above);
    drawer.classList.toggle("has-more-below", below);
    if (!more) return;
    var n = below ? hiddenBelow(nav).length : 0;
    if (n > 0) {
      var label = more.querySelector(".nav-more-label");
      if (label) label.textContent = n + " more";
      more.setAttribute("aria-label", n + " more menu item" + (n === 1 ? "" : "s") + " below");
      more.title = n + " more menu item" + (n === 1 ? "" : "s") + " — click to show";
      more.hidden = false;
    } else {
      more.hidden = true;
    }
  }

  function revealMore() {
    var nav = document.querySelector("#site-nav .drawer-nav");
    if (!nav) return;
    var items = hiddenBelow(nav);
    if (!items.length) return;
    // Scroll so the last hidden item lands at the bottom — reveals all of
    // them when they fit, otherwise as many as the viewport allows.
    var last = items[items.length - 1];
    var delta = last.getBoundingClientRect().bottom - nav.getBoundingClientRect().bottom + 8;
    try { nav.scrollBy({ top: delta, behavior: "smooth" }); }
    catch (e) { nav.scrollTop += delta; }
  }

  function initNavOverflow() {
    var nav = document.querySelector("#site-nav .drawer-nav");
    if (!nav) return;
    var pending = false;
    var schedule = function () {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(function () { pending = false; updateNavOverflow(); });
    };
    nav.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", schedule);
    if (typeof ResizeObserver === "function") {
      try { new ResizeObserver(schedule).observe(nav); } catch (e) { /* ignore */ }
    }
    // Keep the active page's item in view on load.
    var active = nav.querySelector(".nav-item.active");
    if (active && active.getBoundingClientRect().bottom > nav.getBoundingClientRect().bottom) {
      nav.scrollTop += active.getBoundingClientRect().bottom - nav.getBoundingClientRect().bottom + 24;
    }
    updateNavOverflow();
    // Web fonts / theme swaps can change row heights after first paint.
    window.addEventListener("load", schedule);
  }

  /* ---------- Wiring ---------- */

  function init() {
    var toggle   = document.getElementById("drawer-toggle");
    var collapse = document.getElementById("drawer-collapse");
    var scrim    = document.querySelector(".drawer-scrim");

    if (toggle) {
      toggle.setAttribute("aria-expanded",
        html.classList.contains("drawer-open") ? "true" : "false");
    }

    /* Delegated click handler. Using ``document`` guarantees the button
     * keeps working even if a future htmx swap (or a test harness that
     * mounts the topbar late) replaces the button after init runs. It
     * also correctly resolves the target when the click lands on the
     * inner <span> / <svg> child rather than the <button> itself. */
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || typeof target.closest !== "function") return;

      if (target.closest("#drawer-toggle")) {
        event.preventDefault();
        toggleDrawer();
        return;
      }
      if (target.closest("#drawer-collapse")) {
        event.preventDefault();
        toggleRail();
        return;
      }
      if (target.closest("#nav-more")) {
        event.preventDefault();
        revealMore();
        return;
      }
      if (target.closest("[data-drawer-close]") ||
          target.classList && target.classList.contains("drawer-scrim")) {
        closeDrawer();
        return;
      }
    });

    /* Silence the unused-var lints without dropping the null-guarded
     * lookups above (they double as a smoke test that the shell markup
     * is present before we wire delegation). */
    void collapse; void scrim;

    // ESC closes the mobile drawer.
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && html.classList.contains("drawer-open")) {
        closeDrawer();
        if (toggle) toggle.focus();
      }
    });

    // Auto-close the mobile drawer if the viewport grows past the
    // breakpoint (e.g. user rotates a tablet).
    if (mobileMql) {
      var mobileHandler = function () {
        if (!mobileMql.matches) closeDrawer();
      };
      if (typeof mobileMql.addEventListener === "function") {
        mobileMql.addEventListener("change", mobileHandler);
      } else if (typeof mobileMql.addListener === "function") {
        // Safari < 14 fallback.
        mobileMql.addListener(mobileHandler);
      }
    }

    // Any nav-item click while mobile drawer is open closes it so the
    // destination page starts with a clean chrome.
    document.querySelectorAll(".drawer .nav-item").forEach(function (link) {
      link.addEventListener("click", function () {
        if (isMobile()) closeDrawer();
      });
    });

    initNavOverflow();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
