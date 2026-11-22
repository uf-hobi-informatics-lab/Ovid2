/*
 * Ovid2 control panel — navigation drawer controller.
 *
 * Responsibilities:
 *
 *   1. Toggle the mobile drawer (viewport < 900 px) via ``html.drawer-open``.
 *      ARIA-correct, close on ESC or scrim click, and re-hide when the
 *      viewport crosses back above 900 px. Never persisted.
 *
 *   2. Toggle the wide-viewport (>= 900 px) icon rail via
 *      ``html.drawer-rail``. Persisted to ``localStorage`` under
 *      ``ovid2DrawerWideCollapsed`` so a collapsed rail survives
 *      navigation; a pre-paint snippet in ``base.html`` restores it
 *      before first paint. The retired medium-only key
 *      ``ovid2DrawerCollapsed`` (and the old ``drawer-collapsed`` class)
 *      is migrated once and then removed.
 *
 *      Both states are driven by the same ``[data-drawer-toggle]``
 *      buttons: ``#drawer-toggle`` sits in a fixed rail-width slot at
 *      the top of the drawer (never moves), ``#drawer-toggle-mobile`` in
 *      the topbar (phones only).
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

  // Wide-viewport (>= 900px) rail state. Persisted so that navigating via
  // a rail icon keeps the drawer collapsed on the destination page.
  var RAIL_KEY = "ovid2DrawerWideCollapsed";
  // Retired medium-only (900-1199px) key; migrated into RAIL_KEY once.
  var LEGACY_KEY = "ovid2DrawerCollapsed";
  var MOBILE_QUERY = "(max-width: 899.98px)";

  var html = document.documentElement;
  var mobileMql = null;
  try { mobileMql = window.matchMedia(MOBILE_QUERY); } catch (e) { /* ignore */ }

  function isMobile() { return mobileMql ? mobileMql.matches : false; }

  function toggles() {
    return document.querySelectorAll("[data-drawer-toggle]");
  }

  /* Keep every toggle's ARIA in sync with the state it controls. */
  function syncAria() {
    var mobile = isMobile();
    var expanded = mobile
      ? html.classList.contains("drawer-open")
      : !html.classList.contains("drawer-rail");
    toggles().forEach(function (btn) {
      btn.setAttribute("aria-expanded", expanded ? "true" : "false");
      if (btn.id === "drawer-toggle") {
        btn.setAttribute("aria-label", expanded ? "Collapse navigation" : "Expand navigation");
      }
    });
  }

  /* ---------- Mobile drawer ---------- */

  function openDrawer() {
    html.classList.add("drawer-open");
    syncAria();
    // Move focus to the first nav item so keyboard users can navigate.
    var firstLink = document.querySelector(".drawer-nav .nav-item");
    if (firstLink) firstLink.focus();
  }

  function closeDrawer() {
    html.classList.remove("drawer-open");
    syncAria();
  }

  /* ---------- Wide-viewport icon rail ---------- */

  function setRail(on) {
    html.classList.toggle("drawer-rail", on);
    try { localStorage.setItem(RAIL_KEY, on ? "1" : "0"); } catch (e) { /* ignore */ }
    syncAria();
  }

  function toggleDrawer() {
    if (isMobile()) {
      if (html.classList.contains("drawer-open")) closeDrawer();
      else openDrawer();
    } else {
      setRail(!html.classList.contains("drawer-rail"));
    }
  }

  /* One-time migration from the retired medium-only rail key/class. */
  function migrateLegacyRail() {
    html.classList.remove("drawer-collapsed");
    try {
      var legacy = localStorage.getItem(LEGACY_KEY);
      if (legacy === null) return;
      if (localStorage.getItem(RAIL_KEY) === null) {
        localStorage.setItem(RAIL_KEY, legacy === "1" ? "1" : "0");
      }
      localStorage.removeItem(LEGACY_KEY);
    } catch (e) { /* ignore */ }
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
    migrateLegacyRail();
    syncAria();

    /* Delegated click handler. Using ``document`` guarantees the buttons
     * keep working even if a future htmx swap (or a test harness that
     * mounts the shell late) replaces them after init runs. It also
     * resolves the target when the click lands on the inner <svg>. */
    document.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || typeof target.closest !== "function") return;

      if (target.closest("[data-drawer-toggle]")) {
        event.preventDefault();
        toggleDrawer();
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

    // ESC closes the mobile drawer.
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && html.classList.contains("drawer-open")) {
        closeDrawer();
        var mobileToggle = document.getElementById("drawer-toggle-mobile");
        if (mobileToggle) mobileToggle.focus();
      }
    });

    // Crossing the 900 px breakpoint: close the transient mobile drawer
    // and re-sync ARIA (the toggles control a different state each side).
    if (mobileMql) {
      var mobileHandler = function () {
        if (!mobileMql.matches) closeDrawer();
        else syncAria();
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
