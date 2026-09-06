const DEFAULTS = {baseUrl: "http://127.0.0.1:8787", token: "", cycle: "2027"};
const message = document.getElementById("message");

async function load() {
  const settings = await chrome.storage.local.get(DEFAULTS);
  document.getElementById("base-url").value = settings.baseUrl;
  document.getElementById("token").value = settings.token;
  document.getElementById("cycle").value = settings.cycle;
}

document.getElementById("options-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const baseUrl = document.getElementById("base-url").value.trim().replace(/\/$/, "");
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(baseUrl)) {
    message.textContent = "For safety, the base URL must be localhost or 127.0.0.1 over HTTP.";
    message.className = "message error";
    return;
  }
  await chrome.storage.local.set({
    baseUrl,
    token: document.getElementById("token").value.trim(),
    cycle: document.getElementById("cycle").value.trim()
  });
  message.textContent = "Connection saved locally in the browser.";
  message.className = "message success";
});

load().catch((error) => {
  message.textContent = error.message;
  message.className = "message error";
});
