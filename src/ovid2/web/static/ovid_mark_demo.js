/*
 * Dev demo page for <ovid-mark> (templates/dev_ovid_mark.html).
 *
 * State buttons set every sample mark's ``state``; the autoplay loop
 * walks through every state; the flick button replays the rustle.
 * Classic script, no dependencies.
 */

(function () {
  "use strict";

  var ORDER = ["idle", "extract", "transform", "load", "running",
               "sync", "success", "warning", "error"];
  var HOLD_MS = 3200;

  function init() {
    var root = document.querySelector("[data-ovm-demo]");
    if (!root) return;
    var marks = root.querySelectorAll("[data-ovm-sample]");
    var buttons = root.querySelectorAll("[data-ovm-state]");
    var log = root.querySelector("[data-ovm-log]");
    var auto = root.querySelector("[data-ovm-autoplay]");
    var timer = 0;
    var idx = 0;

    function show(state) {
      marks.forEach(function (m) { m.setAttribute("state", state); });
      buttons.forEach(function (b) {
        var on = b.getAttribute("data-ovm-state") === state;
        b.classList.toggle("primary", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      idx = Math.max(0, ORDER.indexOf(state));
    }

    if (marks.length) {
      marks[0].addEventListener("ovid-state-change", function (ev) {
        if (log) log.textContent = "ovid-state-change: " + ev.detail.from + " \u2192 " + ev.detail.to;
      });
    }

    buttons.forEach(function (b) {
      b.addEventListener("click", function () {
        if (auto) auto.checked = false;
        clearInterval(timer);
        show(b.getAttribute("data-ovm-state"));
      });
    });

    var flick = root.querySelector("[data-ovm-flick]");
    if (flick) {
      flick.addEventListener("click", function () {
        marks.forEach(function (m) { if (typeof m.flick === "function") m.flick(); });
      });
    }

    if (auto) {
      auto.addEventListener("change", function () {
        clearInterval(timer);
        if (!auto.checked) return;
        show(ORDER[(idx + 1) % ORDER.length]);
        timer = setInterval(function () { show(ORDER[(idx + 1) % ORDER.length]); }, HOLD_MS);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
