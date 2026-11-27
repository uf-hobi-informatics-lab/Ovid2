/* Ovid2 full screen.
 *
 * Real browser full screen (the browser's own bars and the desktop
 * disappear); the Ovid2 panel itself is unchanged — top bar, left menu,
 * right rail and footer all stay.
 *
 * Browsers end full screen on every full page load, so the app cannot
 * simply navigate while full screen. Instead, on entering, the page
 * ("host") puts the app in a same-origin frame (#fs-frame) that covers
 * the whole window and makes the host document full screen. Every link
 * then loads inside the frame, so full screen survives navigation. When
 * full screen ends (Esc, the toolbar button, the shortcut, browser UI) the
 * frame is dropped and the window reloads at the page the frame was on.
 *
 * Roles:
 *   host   — the top-level page (``<html>.fs-host`` while hosting);
 *   framed — the app inside #fs-frame (``<html>.fs-framed``): its
 *            toolbar button reads *Exit full screen* and asks the host
 *            to exit via postMessage. A framed page never nests a frame.
 *
 * Links that cannot work in a frame (Logout, sign-in, other sites,
 * ``target=_top``) leave full screen and open in the whole window; if the
 * frame lands on the sign-in page (expired session) the host exits too.
 *
 * Shortcut: Ctrl/Cmd+Shift+F toggles from anywhere. Esc is the browser's.
 * The toolbar button is hidden where the Fullscreen API is unavailable.
 *
 * window.Ovid2Fullscreen = { enter, exit, toggle, active }.
 */
(function () {
  "use strict";

  var html = document.documentElement;
  var FRAME_ID = "fs-frame";
  var MSG = "ovid2Fullscreen";
  /* Paths that must load in the whole window, never in the frame. */
  var TOP_ONLY = /^\/(logout|login|auth)(\/|$|\?)/;

  var frame = null;          // host: the #fs-frame element while hosting
  var hostCleared = false;   // host: original page content removed
  var syncTimer = 0;

  function isFramed() {
    try {
      return window.top !== window && !!window.frameElement &&
        window.frameElement.id === FRAME_ID;
    } catch (e) {
      return false;  // cross-origin parent: not our frame
    }
  }

  function nativeElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function supported() {
    return !!(document.fullscreenEnabled || document.webkitFullscreenEnabled) &&
      typeof (html.requestFullscreen || html.webkitRequestFullscreen) === "function";
  }

  function settle(p, onReject) {
    if (p && typeof p.then === "function") {
      p.then(null, function () { if (onReject) { onReject(); } });
    }
  }

  function syncToggles(on) {
    var toggles = document.querySelectorAll("[data-fullscreen-toggle]");
    for (var i = 0; i < toggles.length; i++) {
      var t = toggles[i];
      t.setAttribute("aria-pressed", on ? "true" : "false");
      t.setAttribute("aria-label", on ? "Exit full screen" : "Enter full screen");
      t.setAttribute("title", on ? "Exit full screen (Esc)" : "Full screen (Ctrl+Shift+F)");
    }
  }

  /* ------------------------------------------------------------ host */

  function frameLocation() {
    try {
      var loc = frame && frame.contentWindow && frame.contentWindow.location;
      if (loc && loc.origin === window.location.origin) {
        return loc;
      }
    } catch (e) {
      /* navigated off-origin */
    }
    return null;
  }

  function syncFromFrame() {
    var loc = frameLocation();
    if (!loc) {
      return;
    }
    var href = loc.pathname + loc.search + loc.hash;
    if (TOP_ONLY.test(loc.pathname)) {
      leave(href);
      return;
    }
    if (window.location.pathname + window.location.search + window.location.hash !== href) {
      try { history.replaceState(history.state, "", href); } catch (e) { /* ignore */ }
    }
    try {
      var t = frame.contentDocument && frame.contentDocument.title;
      if (t && document.title !== t) { document.title = t; }
    } catch (e) { /* ignore */ }
  }

  function clearHost() {
    // The frame now shows the app; drop the host's own copy so its live
    // updates / polling stop and two copies never run at once.
    if (hostCleared) {
      return;
    }
    hostCleared = true;
    var kids = Array.prototype.slice.call(document.body.childNodes);
    for (var i = 0; i < kids.length; i++) {
      if (kids[i] !== frame) {
        document.body.removeChild(kids[i]);
      }
    }
  }

  function onFrameLoad() {
    var loc = frameLocation();
    if (!loc) {
      leave(null);
      return;
    }
    syncFromFrame();
    if (!frame) {
      return;  // syncFromFrame left full screen (sign-in page)
    }
    clearHost();
    try { frame.contentWindow.focus(); } catch (e) { /* ignore */ }
  }

  function enter() {
    if (isFramed() || frame || !supported()) {
      return;
    }
    frame = document.createElement("iframe");
    frame.id = FRAME_ID;
    frame.title = document.title || "Ovid2";
    frame.setAttribute("allow", "fullscreen; clipboard-read; clipboard-write");
    frame.addEventListener("load", onFrameLoad);
    frame.src = window.location.href;
    document.body.appendChild(frame);
    html.classList.add("fs-host");
    // Must run inside the user gesture (click / keydown) that called us.
    var fn = html.requestFullscreen || html.webkitRequestFullscreen;
    var p;
    try {
      p = fn.call(html);
    } catch (e) {
      teardown(null);
      return;
    }
    settle(p, function () { teardown(null); });
    syncTimer = window.setInterval(syncFromFrame, 1000);
  }

  /* Leave full screen; once it has ended, ``teardown`` runs. ``href``:
   * where the whole window should go afterwards (null → frame's page). */
  var pendingHref = null;
  function leave(href) {
    pendingHref = href;
    if (nativeElement()) {
      var fn = document.exitFullscreen || document.webkitExitFullscreen;
      try { settle(fn.call(document), function () { teardown(pendingHref); }); }
      catch (e) { teardown(pendingHref); }
    } else {
      teardown(href);
    }
  }

  function teardown(href) {
    if (!frame) {
      return;
    }
    var loc = frameLocation();
    var target = href || (loc ? loc.pathname + loc.search + loc.hash : null);
    window.clearInterval(syncTimer);
    if (frame.parentNode) {
      frame.parentNode.removeChild(frame);
    }
    frame = null;
    pendingHref = null;
    html.classList.remove("fs-host");
    if (hostCleared || href) {
      // The host's content was dropped: load the page the user is on.
      window.location.replace(target || window.location.href);
    }
  }

  function onNativeChange() {
    if (!nativeElement() && frame) {
      teardown(pendingHref);
    }
  }

  function onMessage(ev) {
    if (!frame || ev.source !== frame.contentWindow ||
        ev.origin !== window.location.origin) {
      return;
    }
    var d = ev.data || {};
    if (d[MSG] === "exit") {
      leave(null);
    } else if (d[MSG] === "top" && typeof d.href === "string") {
      var u;
      try { u = new URL(d.href, window.location.href); } catch (e) { return; }
      if (u.protocol !== "http:" && u.protocol !== "https:") {
        return;
      }
      if (u.origin === window.location.origin) {
        leave(u.pathname + u.search + u.hash);
      } else {
        leave(null);
        window.open(u.href, "_blank", "noopener");
      }
    }
  }

  /* ---------------------------------------------------------- framed */

  function askHost(data) {
    try {
      window.parent.postMessage(data, window.location.origin);
    } catch (e) {
      /* ignore */
    }
  }

  function onFramedClick(ev) {
    if (ev.defaultPrevented || ev.button !== 0 ||
        ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) {
      return;
    }
    var t = ev.target;
    var a = t && typeof t.closest === "function" ? t.closest("a[href]") : null;
    if (!a || a.hasAttribute("download")) {
      return;
    }
    var tgt = (a.getAttribute("target") || "").toLowerCase();
    if (tgt === "_blank") {
      return;  // new tab: unaffected by the frame
    }
    var u;
    try { u = new URL(a.href, window.location.href); } catch (e) { return; }
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return;
    }
    if (u.origin !== window.location.origin || TOP_ONLY.test(u.pathname) ||
        tgt === "_top" || tgt === "_parent") {
      ev.preventDefault();
      askHost({ ovid2Fullscreen: "top", href: u.href });
    }
  }

  /* ------------------------------------------------------------ both */

  function active() {
    return isFramed() || !!frame;
  }

  function exit() {
    if (isFramed()) {
      askHost({ ovid2Fullscreen: "exit" });
    } else {
      leave(null);
    }
  }

  function toggle() {
    if (active()) {
      exit();
    } else {
      enter();
    }
  }

  function isShortcut(ev) {
    return (ev.ctrlKey || ev.metaKey) && ev.shiftKey && !ev.altKey &&
      (ev.key === "F" || ev.key === "f" || ev.code === "KeyF");
  }

  function init() {
    var framed = isFramed();
    if (framed) {
      html.classList.add("fs-framed");
      document.addEventListener("click", onFramedClick, true);
    } else if (!supported()) {
      var toggles = document.querySelectorAll("[data-fullscreen-toggle]");
      for (var i = 0; i < toggles.length; i++) { toggles[i].hidden = true; }
    }
    syncToggles(framed);

    document.addEventListener("click", function (ev) {
      var t = ev.target;
      if (t && typeof t.closest === "function" && t.closest("[data-fullscreen-toggle]")) {
        ev.preventDefault();
        toggle();
      }
    });
    document.addEventListener("keydown", function (ev) {
      if (isShortcut(ev)) {
        ev.preventDefault();
        ev.stopPropagation();
        toggle();
      }
    }, true);

    if (!framed) {
      window.addEventListener("message", onMessage);
      document.addEventListener("fullscreenchange", onNativeChange);
      document.addEventListener("webkitfullscreenchange", onNativeChange);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.Ovid2Fullscreen = {
    enter: enter,
    exit: exit,
    toggle: toggle,
    active: active
  };
})();
