/* Minimal vanilla JS. No frameworks, no network calls, nothing loaded remotely.
   Its only job is to confirm destructive actions before the form posts - the UI
   works fully without it. */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) { return; }
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });
})();
