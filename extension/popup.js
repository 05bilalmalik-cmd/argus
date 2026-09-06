const DEFAULTS = {baseUrl: "http://127.0.0.1:8787", token: "", cycle: "2027"};

function inferPage(tab) {
  const rawTitle = (tab.title || "").replace(/\s+/g, " ").trim();
  const pieces = rawTitle.split(/\s+[|–—-]\s+/).filter(Boolean);
  let hostname = "";
  try { hostname = new URL(tab.url || "").hostname.replace(/^www\./, ""); } catch (_) {}
  const employer = pieces.length > 1 ? pieces.at(-1) : hostname.split(".")[0] || "";
  const roleTitle = pieces.length > 1 ? pieces.slice(0, -1).join(" — ") : rawTitle;
  return {employer, roleTitle, url: tab.url || ""};
}

function setMessage(text, type = "") {
  const node = document.getElementById("message");
  node.textContent = text;
  node.className = `message ${type}`.trim();
}

async function initialise() {
  const settings = await chrome.storage.local.get(DEFAULTS);
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const page = inferPage(tab || {});
  document.getElementById("employer").value = page.employer;
  document.getElementById("role-title").value = page.roleTitle;
  document.getElementById("cycle").value = settings.cycle || DEFAULTS.cycle;
  document.getElementById("page-url").value = page.url;
  document.getElementById("url-preview").textContent = page.url;
  if (!settings.token) setMessage("Add the capture token in Connection settings before sending.");
}

document.getElementById("capture-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const settings = await chrome.storage.local.get(DEFAULTS);
  if (!settings.token) {
    setMessage("Capture token missing. Open Connection settings.", "error");
    return;
  }
  const button = document.getElementById("submit");
  button.disabled = true;
  button.textContent = "Sending…";
  setMessage("");
  const form = new FormData(event.currentTarget);
  const payload = {
    employer: String(form.get("employer") || "").trim(),
    role_title: String(form.get("role_title") || "").trim(),
    location: String(form.get("location") || "").trim(),
    cycle: String(form.get("cycle") || "").trim(),
    url: String(form.get("url") || "").trim(),
    source: "browser_capture"
  };
  try {
    const baseUrl = String(settings.baseUrl || DEFAULTS.baseUrl).replace(/\/$/, "");
    const response = await fetch(baseUrl + "/api/capture", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Argus-Token": settings.token},
      body: JSON.stringify(payload)
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `ARGUS returned ${response.status}`);
    setMessage(response.status === 201 ? "Captured. The role is in your ARGUS inbox." : "Already captured. No duplicate was created.", "success");
    await chrome.storage.local.set({cycle: payload.cycle});
  } catch (error) {
    setMessage(error.message || "Could not reach local ARGUS.", "error");
  } finally {
    button.disabled = false;
    button.textContent = "Send to opportunity inbox";
  }
});

initialise().catch((error) => setMessage(error.message, "error"));
