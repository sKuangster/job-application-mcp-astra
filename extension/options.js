(() => {
  "use strict";
  const form = document.getElementById("settings-form"), notice = document.getElementById("notice");
  browser.storage.local.get("settings").then(({settings}) => {if (settings) {document.getElementById("backend-url").value = settings.backend_url; document.getElementById("token").value = settings.token;}});
  form.addEventListener("submit", async event => {
    event.preventDefault(); notice.hidden = false;
    try {await browser.runtime.sendMessage({type: "save_settings", settings: {backend_url: document.getElementById("backend-url").value, token: document.getElementById("token").value}}); notice.textContent = "Connection settings saved. The extension is connecting to your backend.";}
    catch (error) {notice.textContent = error.message;}
  });
  async function status() {const state = await browser.runtime.sendMessage({type: "connection_status"}); document.getElementById("connection").textContent = `Bridge: ${state.connection.replaceAll("_", " ")}`;}
  status(); setInterval(status, 2500);
})();
