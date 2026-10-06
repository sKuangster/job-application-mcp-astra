/* Runs only in the privileged persistent background page. No arbitrary JS bridge. */
(() => {
  "use strict";
  const defaults = {backend_url: "http://127.0.0.1:8765", token: ""};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const error = (code, message) => Object.assign(new Error(message), {code});
  const digest = async value => [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)))].map(b => b.toString(16).padStart(2, "0")).join("");
  const manager = new ApplicationOwnership.OwnershipManager(browser.storage.local, digest);
  let socket, reconnectTimer, attempts = 0, connection = "unconfigured", settings = defaults, clientId;
  const inspected = new Map();
  function validateSettings(input) {
    const url = new URL(input.backend_url);
    if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== "/") throw error("INVALID_BACKEND", "Backend must be an HTTP loopback origin, for example http://127.0.0.1:8765");
    return {backend_url: url.origin, token: String(input.token || "").trim()};
  }
  async function ready(tabId, timeout = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {const tab = await browser.tabs.get(tabId); if (tab.status === "complete") {await sleep(200); return tab;} await sleep(100);}
    throw error("PAGE_TIMEOUT", "Page did not finish loading before the timeout");
  }
  async function send(tabId, frameId, action, payload = {}) {
    const response = await browser.tabs.sendMessage(tabId, {channel: "application-copilot", action, payload}, {frameId});
    if (!response?.ok) throw error(response?.error?.code || "CONTENT_UNAVAILABLE", response?.error?.message || "The page did not respond");
    return response.result;
  }
  function parseId(value) {const m = /^(\d+):(.+)$/.exec(value || ""); if (!m) throw error("INVALID_ELEMENT", "An element ID from the latest inspection is required"); return {frameId: Number(m[1]), id: m[2]};}
  async function inspect(tabId, applicationId) {
    await ready(tabId);
    const frames = await browser.webNavigation.getAllFrames({tabId}) || [{frameId: 0}];
    const snapshots = await Promise.all(frames.map(async frame => {
      try {return {frameId: frame.frameId, snapshot: await send(tabId, frame.frameId, "inspect")};}
      catch (e) {return {frameId: frame.frameId, error: e.code || "INACCESSIBLE_FRAME"};}
    }));
    const main = snapshots.find(s => s.frameId === 0)?.snapshot;
    if (!main) throw error("CONTENT_UNAVAILABLE", "Cannot inspect the main page. Firefox restricted pages cannot be automated.");
    const fields = [], buttons = [], warnings = [], frameMap = {};
    for (const frame of snapshots) {
      if (!frame.snapshot) {warnings.push(`Frame ${frame.frameId} could not be inspected`); continue;}
      frameMap[frame.frameId] = {snapshot_id: frame.snapshot.snapshot_id, url: frame.snapshot.url};
      fields.push(...frame.snapshot.fields.map(f => ({...f, id: `${frame.frameId}:${f.id}`})));
      buttons.push(...frame.snapshot.buttons.map(b => ({...b, id: `${frame.frameId}:${b.id}`})));
      warnings.push(...(frame.snapshot.unsupported || []));
    }
    const snapshotId = await digest(JSON.stringify(frameMap));
    inspected.set(applicationId, {snapshot_id: snapshotId, frames: frameMap, url: main.url});
    const available = snapshots.filter(s => s.snapshot).map(s => s.snapshot);
    const activeForm = available.find(s => s.buttons.some(b => b.kind === "submit")) || available.find(s => s.fields.length) || main;
    return {...main, stage: activeForm.stage, platform: activeForm.platform !== "generic" ? activeForm.platform : main.platform, fields: fields.slice(0, 250), buttons: buttons.slice(0, 250), text: available.map(s => s.text).join("\n").slice(0, 20000), captcha: available.some(s => s.captcha), validation_errors: available.flatMap(s => s.validation_errors), success: available.some(s => s.success), success_evidence: available.filter(s => s.success).map(s => s.success_evidence).join(" "), snapshot_id: snapshotId, unsupported: [...new Set(warnings)], tab_id: tabId};
  }
  async function execute(command) {
    const p = command.payload || {}, action = command.action;
    if (action === "reassign") {inspected.delete(command.application_id); return manager.reassign(command);}
    if (action === "open_url") {
      const url = new URL(p.url);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw error("INVALID_URL", "Only ordinary HTTP(S) application URLs are allowed");
      let owner = manager.owners[command.application_id];
      if (owner) {owner = manager.check(command); const tab = await browser.tabs.get(owner.tab_id); if (tab.url !== url.href) await browser.tabs.update(owner.tab_id, {url: url.href});}
      else {const tab = await browser.tabs.create({url: "about:blank", active: false}); owner = await manager.assign(command, tab.id, url.href); await browser.tabs.update(tab.id, {url: url.href});}
      inspected.delete(command.application_id);
      const tab = await ready(owner.tab_id); owner.url = tab.url; await manager.save(); return {tab_id: tab.id, url: tab.url};
    }
    const owner = manager.check(command);
    if (action === "inspect") return inspect(owner.tab_id, command.application_id);
    if (action === "focus") {const tab = await browser.tabs.get(owner.tab_id); await browser.windows.update(tab.windowId, {focused: true}); await browser.tabs.update(owner.tab_id, {active: true}); return {tab_id: owner.tab_id};}
    if (action === "screenshot") return {data_url: await browser.tabs.captureTab(owner.tab_id, {format: "png"}), tab_id: owner.tab_id};
    if (action === "release" || action === "close") {if (action === "close") await browser.tabs.remove(owner.tab_id); await manager.remove(command.application_id); inspected.delete(command.application_id); return {released: true};}
    if (action === "fill") {
      const groups = new Map();
      for (const item of p.values || []) {const parsed = parseId(item.field_id); if (!groups.has(parsed.frameId)) groups.set(parsed.frameId, []); groups.get(parsed.frameId).push({field_id: parsed.id, value: item.value});}
      const results = [];
      for (const [frameId, values] of groups) {manager.check(command); const result = await send(owner.tab_id, frameId, "fill", {values}); results.push(...result.fields.map(f => ({...f, field_id: `${frameId}:${f.field_id}`})));}
      inspected.delete(command.application_id); return {fields: results};
    }
    if (action === "upload") {const parsed = parseId(p.field_id); inspected.delete(command.application_id); return send(owner.tab_id, parsed.frameId, "upload", {...p, field_id: parsed.id});}
    if (action === "locate") {
      const known = inspected.get(command.application_id);
      if (!known || p.snapshot_id !== known.snapshot_id || p.expected_url !== known.url) throw error("STALE_SNAPSHOT", "Inspect the page before locating a target");
      const frame = known.frames[0];
      const result = await send(owner.tab_id, 0, "locate", {...p, snapshot_id: frame.snapshot_id, expected_url: frame.url});
      return {field: result.field ? {...result.field, id: `0:${result.field.id}`} : null, button: result.button ? {...result.button, id: `0:${result.button.id}`} : null};
    }
    if (action === "click") {
      const known = inspected.get(command.application_id), parsed = parseId(p.button_id);
      if (!known || p.snapshot_id !== known.snapshot_id || p.expected_url !== known.url || !known.frames[parsed.frameId]) throw error("STALE_SNAPSHOT", "Inspect and validate the current page before clicking");
      const current = await browser.tabs.get(owner.tab_id);
      if (current.url !== known.url) throw error("STALE_PAGE", "Application navigated since the last inspection");
      manager.check(command); inspected.delete(command.application_id);
      return send(owner.tab_id, parsed.frameId, "click", {...p, button_id: parsed.id, snapshot_id: known.frames[parsed.frameId].snapshot_id, expected_url: known.frames[parsed.frameId].url});
    }
    throw error("UNKNOWN_ACTION", "Unsupported browser action");
  }
  function scheduleReconnect() {clearTimeout(reconnectTimer); if (settings.token) reconnectTimer = setTimeout(connect, Math.min(30000, 700 * 2 ** Math.min(attempts++, 6)) + Math.random() * 400);}
  async function connect() {
    clearTimeout(reconnectTimer);
    if (socket) {socket.onclose = null; socket.close();}
    if (!settings.token) {connection = "unconfigured"; return;}
    connection = "connecting";
    const endpoint = new URL("/bridge", settings.backend_url); endpoint.protocol = "ws:";
    const current = new WebSocket(endpoint.href); socket = current;
    current.onopen = () => current.send(JSON.stringify({type: "hello", token: settings.token, client_id: clientId, ownership: manager.owners}));
    current.onmessage = async event => {
      let command;
      try {command = JSON.parse(event.data);} catch {return;}
      if (command.type === "welcome") {connection = "connected"; attempts = 0; return;}
      if (command.type === "ping") {current.send(JSON.stringify({type: "pong"})); return;}
      if (!command.id || connection !== "connected") return;
      const response = {id: command.id, application_id: command.application_id, worker_id: command.worker_id, ownership_token: command.ownership_token};
      try {response.result = await manager.run(command, execute); response.ok = true;}
      catch (e) {response.ok = false; response.error = {code: e.code || "BROWSER_ERROR", message: e.message};}
      if (current.readyState === WebSocket.OPEN) current.send(JSON.stringify(response));
    };
    current.onerror = () => {connection = "disconnected";};
    current.onclose = () => {if (socket === current) {connection = "disconnected"; scheduleReconnect();}};
  }
  browser.runtime.onMessage.addListener((message, sender) => {
    if (sender.tab || sender.id !== browser.runtime.id) return undefined;
    return (async () => {
      if (message.type === "connection_status") return {connection, ownership: Object.fromEntries(Object.entries(manager.owners).map(([key, o]) => [key, {tab_id: o.tab_id, worker_id: o.worker_id, url: o.url}]))};
      if (message.type === "save_settings") {settings = validateSettings(message.settings); await browser.storage.local.set({settings}); await connect(); return {ok: true};}
      if (message.type === "open_dashboard") {await browser.tabs.create({url: browser.runtime.getURL("dashboard.html")}); return {ok: true};}
      return undefined;
    })();
  });
  browser.tabs.onRemoved.addListener(tabId => {for (const [applicationId, owner] of Object.entries(manager.owners)) if (owner.tab_id === tabId) {manager.remove(applicationId).catch(() => {}); inspected.delete(applicationId);}});
  (async () => {
    await manager.initialize(); const data = await browser.storage.local.get(["settings", "client_id"]);
    settings = validateSettings({...defaults, ...data.settings}); clientId = data.client_id || crypto.randomUUID();
    await browser.storage.local.set({client_id: clientId});
    for (const [key, owner] of Object.entries(manager.owners)) {try {await browser.tabs.get(owner.tab_id);} catch {await manager.remove(key);}}
    await connect();
  })().catch(() => {connection = "configuration_error";});
})();
