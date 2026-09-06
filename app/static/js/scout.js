(() => {
  "use strict";

  const resultBox = document.getElementById("scout-result");
  const resultText = document.getElementById("scout-result-text");
  if (!resultBox || !resultText) return;

  const showResult = (message) => {
    resultText.textContent = message;
    resultBox.classList.add("is-visible");
    resultBox.setAttribute("aria-hidden", "false");
  };

  const describeResult = (payload) => {
    if (typeof payload === "string") return payload.slice(0, 400);
    if (!payload || typeof payload !== "object") return "Action returned no structured result.";
    if (payload.live_scrape !== undefined) {
      return `Trackr scrape recorded ${payload.live_scrape} listing(s); ${payload.with_application_url || 0} had an application URL. No application was submitted.`;
    }
    const details = Array.isArray(payload.details) ? payload.details : [];
    const awaiting = details.filter((item) => item && item.result === "awaiting_action_time_confirmation").length;
    const blocked = Number(payload.blocked || 0);
    const needsUser = Number(payload.needs_user || 0);
    const processed = Number(payload.processed || 0);
    const submitted = Number(payload.submitted || 0);
    if (awaiting || needsUser || blocked) {
      return `Review pass recorded ${processed} application(s): ${needsUser} need you, ${blocked} blocked, ${awaiting} awaiting exact confirmation, ${submitted} submitted. No batch submission was authorised.`;
    }
    return `Review pass recorded ${processed} application(s); ${submitted} submitted. No batch confirmation was used.`;
  };

  document.querySelectorAll("[data-scout-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const action = button.dataset.scoutAction;
      const runs = button.dataset.runs || "";
      const confirmation = button.dataset.confirm || `Run ${action || "this"} action?`;
      if (!window.confirm(confirmation)) return;
      const url = action === "scrape-live"
        ? "/api/scout/scrape-live"
        : `/api/scout/autopilot?max_runs=${encodeURIComponent(runs)}`;
      const original = button.textContent;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "Working…";
      try {
        const response = await fetch(url, { method: "POST", headers: { Accept: "application/json" } });
        const type = response.headers.get("content-type") || "";
        const payload = type.includes("json") ? await response.json() : await response.text();
        if (!response.ok) {
          const detail = typeof payload === "object" ? payload.detail : payload;
          throw new Error(detail || `Request failed (${response.status})`);
        }
        showResult(describeResult(payload));
      } catch (error) {
        showResult(`Failed: ${error instanceof Error ? error.message : String(error)}`);
      } finally {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = original;
      }
    });
  });
})();
