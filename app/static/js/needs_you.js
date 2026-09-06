// Needs-You only mirrors truthful Navigator outcomes in the local toast.
// It deliberately has no submit endpoint and no generic completion message.
(function () {
  "use strict";

  var toast = document.getElementById("handoff-result");
  var text = document.getElementById("handoff-text");

  function show(message) {
    if (!toast || !text) return;
    text.textContent = message;
    toast.classList.add("is-visible");
    window.setTimeout(function () { toast.classList.remove("is-visible"); }, 8000);
  }

  document.querySelectorAll("[data-navigator-panel]").forEach(function (panel) {
    panel.addEventListener("argus:navigator-state", function (event) {
      var detail = event.detail || {};
      var state = String(detail.state || "").toUpperCase();
      if (state === "UNKNOWN") show("Outcome unknown. Reconcile first; do not retry.");
      if (state === "BLOCKED" || state === "ERROR" || state === "FAILED") {
        show("Navigator blocked or errored: " + String(detail.reason || "no action was issued"));
      }
    });
  });
}());
