/* Silent session recheck (Wave 3, todo 12).
 *
 * Pings GET /auth/recheck on tab focus (visibilitychange) and on a slow
 * interval. When the server reports {"authenticated": false} the user's
 * session has expired or been revoked, so we send them to /login with a
 * `next` back to the current page.
 *
 * Fail-open: any fetch/parse error is swallowed. A transient network blip
 * must never bounce a working session to the login page — the next full
 * navigation is guarded server-side by the auth middleware anyway.
 *
 * Loaded by base.html only when auth is enabled.
 */
(function () {
  "use strict";

  var RECHECK_URL = "/auth/recheck";
  var INTERVAL_MS = 60000; // 1 minute

  function redirectToLogin() {
    try {
      var here = window.location.pathname + window.location.search;
      window.location.assign("/login?next=" + encodeURIComponent(here));
    } catch (e) {
      window.location.assign("/login");
    }
  }

  function recheck() {
    try {
      fetch(RECHECK_URL, {
        method: "GET",
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
        cache: "no-store"
      })
        .then(function (resp) {
          if (!resp.ok) { return null; } // fail-open on non-2xx
          return resp.json();
        })
        .then(function (data) {
          if (data && data.authenticated === false) {
            redirectToLogin();
          }
        })
        .catch(function () {
          /* fail-open: swallow network/parse errors */
        });
    } catch (e) {
      /* fail-open */
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      recheck();
    }
  });

  window.setInterval(recheck, INTERVAL_MS);
})();
