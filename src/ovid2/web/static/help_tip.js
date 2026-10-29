/*
 * "?" help tips — progressive enhancement for templates/_help.html.
 *
 * CSS alone already reveals the bubble on hover / keyboard focus. This
 * script adds:
 *   - tap / click to pin a bubble open (touch screens have no hover);
 *   - outside-click and Esc to close the pinned bubble;
 *   - edge flipping: a bubble that would overflow the viewport on the
 *     right is anchored to the right instead (``.help-tip-flip``).
 *
 * Uses event delegation on ``document`` so tips rendered later by HTMX
 * swaps work without re-binding. No dependencies.
 */
(function () {
  "use strict";

  function closeAll(except) {
    document.querySelectorAll(".help-tip.is-open").forEach(function (tip) {
      if (tip === except) return;
      tip.classList.remove("is-open");
      var btn = tip.querySelector(".help-tip-btn");
      if (btn) btn.setAttribute("aria-expanded", "false");
    });
  }

  /* Keep the bubble inside the viewport: flip to right-anchored when it
   * overflows on the right, open upward near the bottom edge, and as a
   * last resort nudge it horizontally (narrow phones). */
  function place(tip) {
    var bubble = tip.querySelector(".help-tip-bubble");
    if (!bubble) return;
    tip.classList.remove("help-tip-flip", "help-tip-up");
    bubble.style.translate = "";
    var rect = bubble.getBoundingClientRect();
    if (!rect.width) return;
    if (rect.right > window.innerWidth - 8) {
      tip.classList.add("help-tip-flip");
      rect = bubble.getBoundingClientRect();
    }
    if (rect.left < 8) {
      bubble.style.translate = (8 - rect.left) + "px 0";
    } else if (rect.right > window.innerWidth - 8) {
      bubble.style.translate = (window.innerWidth - 8 - rect.right) + "px 0";
    }
    if (rect.bottom > window.innerHeight - 8 && rect.top - rect.height > 60) {
      tip.classList.add("help-tip-up");
    }
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest(".help-tip-btn");
    if (!btn) {
      if (!(ev.target.closest && ev.target.closest(".help-tip-bubble"))) closeAll(null);
      return;
    }
    ev.preventDefault();
    ev.stopPropagation();
    var tip = btn.closest(".help-tip");
    var open = !tip.classList.contains("is-open");
    closeAll(tip);
    tip.classList.toggle("is-open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) place(tip);
  });

  document.addEventListener("mouseover", function (ev) {
    var tip = ev.target.closest && ev.target.closest(".help-tip");
    if (tip) place(tip);
  });
  document.addEventListener("focusin", function (ev) {
    var tip = ev.target.closest && ev.target.closest(".help-tip");
    if (tip) place(tip);
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    var open = document.querySelector(".help-tip.is-open");
    if (open) {
      closeAll(null);
      var btn = open.querySelector(".help-tip-btn");
      if (btn) btn.focus();
    }
  });
})();
