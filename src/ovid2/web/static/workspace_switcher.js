/* Workspace switcher (adr-workspaces.md).
 *
 * The top-bar <select> submits its form as soon as the person picks another
 * workspace (the form also works without JS via a <noscript> button). The
 * server re-checks membership and redirects to the section the person was
 * on, since items of the old workspace do not exist in the new one.
 */
(function () {
  "use strict";
  function bind() {
    var forms = document.querySelectorAll("[data-ws-switcher]");
    Array.prototype.forEach.call(forms, function (form) {
      var select = form.querySelector("[data-ws-select]");
      if (!select || select.dataset.wsBound) { return; }
      select.dataset.wsBound = "1";
      var initial = select.value;
      select.addEventListener("change", function () {
        if (select.value === initial) { return; }
        select.disabled = false;
        form.submit();
      });
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
