/* Minimal vanilla JS. No frameworks, no network calls to anywhere but this
   gateway, nothing loaded remotely. Every page works with JavaScript disabled;
   this only adds two conveniences. */
(function () {
  "use strict";

  // ---------------------------------------------------------------------
  // 1. Confirm destructive actions before the form posts.
  // ---------------------------------------------------------------------
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) { return; }
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  // ---------------------------------------------------------------------
  // 2. Sign out a page that has been left idle.
  //
  // The server already expires an idle session; without this the operator only
  // finds out at the next click, and a signed-in gateway sits open on an
  // unattended screen until then. This mirrors the *server's* timeout rather
  // than inventing a second one, and it never extends a session on its own: the
  // keepalive fires only after real interaction, so a page polling on a timer
  // cannot keep a session alive next to an empty chair.
  //
  // If JavaScript is off, or a background tab throttles these timers, nothing
  // is weakened - the server still enforces the same timeout on the next
  // request.
  // ---------------------------------------------------------------------
  var body = document.body;
  var timeoutSeconds = parseInt(body.getAttribute("data-idle-timeout") || "0", 10);
  var banner = document.getElementById("idle-warning");
  if (!timeoutSeconds || timeoutSeconds < 30 || !banner) { return; }

  var messageEl = document.getElementById("idle-message");
  var stayButton = document.getElementById("idle-stay");
  var template = banner.getAttribute("data-template") || "Signing out in {seconds}s.";
  var csrfToken = body.getAttribute("data-csrf-token") || "";

  // Warn for the last tenth of the window, between 20 and 120 seconds.
  var warnSeconds = Math.min(120, Math.max(20, Math.round(timeoutSeconds / 10)));
  // Refresh the server session at most this often, and only when in use.
  var keepaliveSeconds = Math.max(60, Math.round(timeoutSeconds / 4));
  var TICK_MS = 5000;

  var lastActivity = Date.now();
  var lastKeepalive = Date.now();
  var signingOut = false;

  function noteActivity() {
    lastActivity = Date.now();
    if (!banner.hidden) { banner.hidden = true; }
  }

  ["mousedown", "keydown", "wheel", "touchstart", "scroll", "pointermove"].forEach(
    function (name) {
      window.addEventListener(name, noteActivity, { passive: true });
    }
  );
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) { noteActivity(); }
  });

  function keepalive() {
    lastKeepalive = Date.now();
    fetch("/api/keepalive", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      credentials: "same-origin",
    }).then(function (response) {
      // 401 means the server already ended the session - stop guessing and go.
      if (response.status === 401) { signOut(); }
    }).catch(function () {
      /* Offline or the service restarted: the next real request will redirect. */
    });
  }

  function signOut() {
    if (signingOut) { return; }
    signingOut = true;
    var form = document.querySelector('form[action="/logout"]');
    if (form) {
      // Reuse the CSRF-protected logout form rather than adding a GET route.
      var reason = document.createElement("input");
      reason.type = "hidden";
      reason.name = "reason";
      reason.value = "timeout";
      form.appendChild(reason);
      form.submit();
    } else {
      // Pages that hide the navigation (a forced password change) have no
      // logout form; the server ends the session on its own timetable.
      window.location.href = "/login?timeout=1";
    }
  }

  window.setInterval(function () {
    if (signingOut) { return; }
    var idleMs = Date.now() - lastActivity;
    var remaining = Math.ceil((timeoutSeconds * 1000 - idleMs) / 1000);

    if (remaining <= 0) {
      signOut();
      return;
    }

    if (remaining <= warnSeconds) {
      if (messageEl) {
        messageEl.textContent = template.replace("{seconds}", String(remaining));
      }
      banner.hidden = false;
      return;
    }

    banner.hidden = true;
    if (Date.now() - lastKeepalive >= keepaliveSeconds * 1000
        && idleMs < keepaliveSeconds * 1000) {
      keepalive();
    }
  }, TICK_MS);

  if (stayButton) {
    stayButton.addEventListener("click", function () {
      noteActivity();
      keepalive();
    });
  }
})();
