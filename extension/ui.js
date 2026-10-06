(() => {
  "use strict";
  let settings, applications = [], selected, refreshing = false, detailRevision = "";
  const $ = id => document.getElementById(id);
  const isPopup = document.body.classList.contains("popup");
  function node(tag, text, className) {const e = document.createElement(tag); if (text !== undefined) e.textContent = String(text ?? ""); if (className) e.className = className; return e;}
  const array = (data, key) => Array.isArray(data) ? data : data?.[key] || data?.items || [];
  function displayTime(value) {if (!value) return "—"; const date = new Date(typeof value === "number" ? value * (value < 1e12 ? 1000 : 1) : value); return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();}
  function notice(message) {$("notice").textContent = message; $("notice").hidden = !message;}
  async function api(path, payload) {
    if (!settings?.token) throw new Error("Add your backend token in Connection settings.");
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(`${settings.backend_url}${path}`, {method: payload ? "POST" : "GET", headers: {Authorization: `Bearer ${settings.token}`, ...(payload ? {"Content-Type": "application/json"} : {})}, body: payload ? JSON.stringify(payload) : undefined, signal: controller.signal, credentials: "omit", cache: "no-store"});
      const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.error?.message || `Backend returned ${response.status}`); return data;
    } finally {clearTimeout(timeout);}
  }
  function appInfo(app) {const job = app.job || {}; return {id: app.id || app.application_id, company: app.company || job.company || "Unknown company", role: app.title || app.role || job.title || job.role || "Application", status: app.state || app.status || "QUEUED", updated: app.updated_at || app.updated || app.created_at};}
  function actionButton(text, action, applicationId, className = "secondary") {const b = node("button", text, className); b.dataset.action = action; b.dataset.application = applicationId; return b;}
  function renderCounts(counts = {}) {
    const map = [["Queued", counts.queued ?? counts.queue ?? counts.remaining ?? counts.QUEUED ?? 0], ["Submitted", counts.submitted ?? counts.SUBMITTED ?? 0], ["Skipped", counts.skipped ?? counts.SKIPPED ?? 0], ["Needs attention", counts.needs_user ?? counts.NEEDS_USER ?? counts.attention ?? 0], ["Active", counts.active ?? counts.running ?? 0]];
    $("counts").replaceChildren(...map.map(([label, value]) => {const c = node("div", undefined, "count"); c.append(node("strong", value), node("span", label)); return c;}));
  }
  function renderWorkers(workers) {
    const rows = Array.isArray(workers) ? workers : Object.entries(workers || {}).map(([id, w]) => ({id, ...w}));
    $("worker-count").textContent = `${rows.filter(w => w.application_id || w.application).length} active / ${rows.length} workers`;
    $("workers").replaceChildren(...rows.map(worker => {
      const app = worker.application || applications.find(a => appInfo(a).id === worker.application_id) || worker;
      const info = appInfo(app), card = node("article", undefined, "worker");
      card.append(node("p", worker.id || worker.worker_id || "Worker", "eyebrow"), node("h3", worker.application_id || worker.application ? info.company : "Ready for work"), node("p", worker.application_id || worker.application ? info.role : "No application claimed", "muted"), node("p", worker.stage || worker.state || worker.status || "Idle"));
      if (worker.application_id || info.id) {const controls = node("div", undefined, "controls"); controls.append(actionButton("Open tab", "open", worker.application_id || info.id), actionButton("Details", "details", worker.application_id || info.id)); card.append(controls);}
      return card;
    }));
    if (!rows.length) $("workers").append(node("p", "No workers are active. Resume to process the queue.", "empty"));
  }
  function renderApplications() {
    if (!$("applications")) return;
    const query = ($("filter").value || "").toLowerCase();
    const rows = applications.filter(app => {const i = appInfo(app); return `${i.company} ${i.role} ${i.status}`.toLowerCase().includes(query);});
    $("applications").replaceChildren(...rows.map(app => {
      const info = appInfo(app), row = node("tr", undefined, selected === info.id ? "selected" : "");
      const title = node("td"); title.append(node("strong", info.company), node("div", info.role, "muted"));
      const status = node("td"); status.append(node("span", info.status.replaceAll("_", " "), `badge ${info.status}`));
      const actions = node("td"); actions.append(actionButton("Details", "details", info.id));
      row.append(title, status, node("td", displayTime(info.updated)), actions); return row;
    }));
    if (!rows.length) {const tr = node("tr"), td = node("td", "No applications found. Ingest jobs through MCP or run a local fixture.", "muted"); td.colSpan = 4; tr.append(td); $("applications").append(tr);}
  }
  async function details(id, force = false) {
    if (isPopup) {await browser.tabs.create({url: `${browser.runtime.getURL("dashboard.html")}#${encodeURIComponent(id)}`}); return;}
    selected = id; location.hash = encodeURIComponent(id); renderApplications();
    const [raw, eventData, answerData] = await Promise.all([api(`/api/applications/${encodeURIComponent(id)}`), api(`/api/applications/${encodeURIComponent(id)}/events`), api(`/api/applications/${encodeURIComponent(id)}/answers`)]);
    if (selected !== id) return;
    const revision = JSON.stringify([raw, eventData, answerData]); if (!force && revision === detailRevision) return; detailRevision = revision;
    const app = raw.application || raw, info = appInfo(app), panel = $("details");
    panel.replaceChildren(node("p", info.status.replaceAll("_", " "), "eyebrow"), node("h2", `${info.company} · ${info.role}`));
    const controls = node("div", undefined, "controls detail-actions");
    for (const [label, action] of [["Open application", "open"], ["Inspect page", "inspect"], ["Retry", "retry"], ["Resume", "resume"], ["Skip job", "skip"]]) controls.append(actionButton(label, action, id));
    panel.append(controls);
    const data = app.data || {};
    const summary = node("div", undefined, "answer");
    for (const [label, value] of [["Application", id], ["Platform", app.platform || data.platform], ["Resume", app.resume_path || data.resume?.name || data.resume?.path || data.resume], ["Final URL", app.final_url || data.final_url || data.url], ["Eligibility", app.eligibility_reason || data.eligibility], ["Submission evidence", data.success_evidence || data.submission_result]]) if (value) summary.append(node("strong", `${label}: `), node("pre", typeof value === "object" ? JSON.stringify(value, null, 2) : value));
    panel.append(summary, node("h3", "Exact questions and answers"));
    const answers = array(answerData, "answers");
    if (!answers.length) panel.append(node("p", "No answers recorded yet.", "muted"));
    for (const answer of answers) {
      const box = node("article", undefined, "answer");
      box.append(node("strong", answer.original_question || answer.question || answer.label || answer.normalized_question), node("pre", answer.final_submitted_value ?? answer.submitted_value ?? answer.answer ?? answer.value ?? "Unresolved"));
      const metadata = Object.fromEntries(Object.entries(answer).filter(([key]) => !["answer", "value", "final_submitted_value", "submitted_value", "original_question", "question", "label"].includes(key)));
      const disclosure = node("details"); disclosure.append(node("summary", "Method, sources, and limits"), node("pre", JSON.stringify(metadata, null, 2))); box.append(disclosure); panel.append(box);
    }
    panel.append(node("h3", "Timeline"));
    const timeline = node("ol", undefined, "timeline");
    for (const event of array(eventData, "events")) {
      const li = node("li"); li.append(node("time", displayTime(event.created_at || event.timestamp || event.at)), node("h3", event.event_type || event.type || event.event || "Event"));
      if (event.message) li.append(node("p", event.message));
      li.append(node("p", [event.component, event.worker_id].filter(Boolean).join(" · "), "muted"));
      const payload = event.data || event.payload || event.details;
      if (payload && Object.keys(payload).length) li.append(node("pre", typeof payload === "string" ? payload : JSON.stringify(payload, null, 2)));
      timeline.append(li);
    }
    panel.append(timeline);
  }
  async function refresh() {
    if (refreshing || document.hidden) return; refreshing = true;
    try {
      const [state, bridge, appData] = await Promise.all([api("/api/status"), browser.runtime.sendMessage({type: "connection_status"}), api("/api/applications")]);
      applications = array(appData, "applications");
      const control = state.control || {}; $("autopilot").textContent = `Autopilot: ${control.state || control.status || state.status || (control.paused ? "paused" : control.stopped ? "stopped" : "running")}`;
      $("connection").textContent = `Bridge ${bridge.connection.replaceAll("_", " ")}`; $("indicator").className = `indicator ${bridge.connection === "connected" ? "connected" : ""}`;
      if (control.mode) $("mode").value = control.mode;
      renderCounts(state.counts); renderWorkers(state.workers); renderApplications();
      $("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`; notice("");
      if (selected && !isPopup) await details(selected);
    } catch (error) {notice(error.name === "AbortError" ? "Backend request timed out. Check that the local server is running." : error.message); $("autopilot").textContent = "Backend unavailable";}
    finally {refreshing = false;}
  }
  document.addEventListener("click", async event => {
    const b = event.target.closest("button"); if (!b) return;
    try {
      b.disabled = true;
      if (b.dataset.control) await api("/api/control", {action: b.dataset.control, mode: $("mode").value});
      else if (b.dataset.action === "details") await details(b.dataset.application, true);
      else if (b.dataset.action) {const result = await api(`/api/applications/${encodeURIComponent(b.dataset.application)}/action`, {action: b.dataset.action}); if (b.dataset.action === "inspect") {notice("Current browser inspection was recorded. Refreshing the application timeline."); if (result.snapshot && $("details")) {const pre = node("pre", JSON.stringify(result.snapshot, null, 2)); $("details").append(pre);}}}
      else if (b.id === "open-dashboard") await browser.runtime.sendMessage({type: "open_dashboard"});
      await refresh();
    } catch (error) {notice(error.message);} finally {b.disabled = false;}
  });
  $("filter")?.addEventListener("input", renderApplications);
  browser.storage.onChanged.addListener((changes, area) => {if (area === "local" && changes.settings) {settings = changes.settings.newValue; refresh();}});
  (async () => {const data = await browser.storage.local.get("settings"); settings = data.settings || {backend_url: "http://127.0.0.1:8765", token: ""}; selected = decodeURIComponent(location.hash.slice(1)) || undefined; await refresh(); setInterval(refresh, 2500);})();
})();
