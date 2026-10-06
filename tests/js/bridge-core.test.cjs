"use strict";
// BridgeCore tests with a scripted fake driver (no DOM).
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { BridgeCore, mergeSnapshots, STORAGE_KEY } = require(path.resolve(__dirname, "..", "..", "firefox", "background", "bridge-core.js"));

const sleep = ms => new Promise(r => setTimeout(r, ms));

function memStorage(initial = {}) {
  const m = new Map(Object.entries(initial));
  return { m, get: async k => m.get(k), set: async (k, v) => { m.set(k, JSON.parse(JSON.stringify(v))); } };
}

// frames: tabId -> {frameId: handler(msg) -> reply}
function makeDriver() {
  const d = {
    tabs: new Map(), nextTab: 1, nextWin: 10, calls: [], removed: [], frames: new Map(), delays: {},
    log: [],
    onTabRemoved(cb) { this.removed.push(cb); },
    async openTab(url, opts) {
      const id = this.nextTab++;
      const win = this.nextWin++;
      this.tabs.set(id, { tab_id: id, window_id: win, url, title: "T" + id, status: "complete" });
      this.calls.push(["openTab", url, opts.strategy]);
      return { tab_id: id, window_id: win };
    },
    async navigate(tabId, url) { this.calls.push(["navigate", tabId, url]); this.tabs.get(tabId).url = url; },
    async waitForLoad(tabId) { if (!this.tabs.has(tabId)) throw Object.assign(new Error("x"), { code: "TAB_CLOSED" }); return { complete: true }; },
    async getTab(tabId) { const t = this.tabs.get(tabId); return t ? Object.assign({}, t) : null; },
    async focus(tabId) { this.calls.push(["focus", tabId]); },
    async close(tabId) { this.calls.push(["close", tabId]); this.tabs.delete(tabId); this.removed.forEach(cb => cb(tabId)); },
    async screenshot(tabId) { this.calls.push(["screenshot", tabId]); return { data_url: "data:image/png;base64,AA==" }; },
    async getFrames(tabId) {
      const f = this.frames.get(tabId) || { 0: defaultHandler };
      return Object.keys(f).map(k => ({ frameId: Number(k), url: "https://x/" + k }));
    },
    async sendToFrame(tabId, frameId, msg) {
      this.log.push({ tabId, frameId, action: msg.action, payload: msg.payload });
      const f = (this.frames.get(tabId) || { 0: defaultHandler })[frameId];
      if (!f) throw new Error("no frame");
      const delay = this.delays[tabId] || 0;
      if (delay) await sleep(delay);
      return f(msg, tabId);
    },
    userClose(tabId) { this.tabs.delete(tabId); this.removed.forEach(cb => cb(tabId)); }
  };
  return d;
}
function defaultHandler(msg) {
  switch (msg.action) {
    case "scan": return { ok: true, result: snap({}) };
    case "click": return { ok: true, result: { ok: true } };
    case "wait_quiet": return { ok: true, result: { quiet: true } };
    case "fill": return { ok: true, result: { results: msg.payload.items.map(i => ({ field_id: i.field_id, ok: true, actual: i.value })) } };
    default: return { ok: true, result: {} };
  }
}
function snap(o) {
  return Object.assign({
    url: "https://x/", title: "t", frames: 1, ats: "generic", page_kind: "unknown", heading: "", step: null,
    fields: [], buttons: [], errors: [], captcha: { present: false, kind: null, interactive: false, detail: null },
    confirmation: { detected: false, text: null }, signals: { has_password: false, has_submit: false, text_hash: "00" }, fingerprint: "x"
  }, o);
}
let seq = 0;
function cmd(app_id, action, payload = {}, timeout_ms) {
  return { type: "command", id: "c" + (++seq), app_id, action, payload, timeout_ms };
}

test("ping/pong, hello, unknown action, missing app_id", async () => {
  const core = new BridgeCore({ driver: makeDriver(), storage: memStorage(), version: "9.9" });
  assert.deepEqual(await core.handleMessage({ type: "ping", ts: 5 }), { type: "pong", ts: 5 });
  const hello = core.helloMessage();
  assert.equal(hello.type, "hello"); assert.equal(hello.version, "9.9"); assert.deepEqual(hello.owned, []);
  let r = await core.handleMessage(cmd("a", "explode"));
  assert.equal(r.ok, false); assert.equal(r.error.code, "UNSUPPORTED");
  r = await core.handleMessage({ type: "command", id: "x1", action: "scan", payload: {} });
  assert.equal(r.error.code, "BAD_REQUEST");
  r = await core.handleMessage(cmd("a", "open", { url: "javascript:alert(1)" }));
  assert.equal(r.error.code, "BAD_REQUEST");
  assert.equal(await core.handleMessage({ type: "nonsense" }), null);
});

test("ownership isolation: app B can never touch app A's tab", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  const a = await core.handleMessage(cmd("A", "open", { url: "https://a.example/job" }));
  assert.equal(a.ok, true, JSON.stringify(a));
  assert.equal(a.result.url, "https://a.example/job");
  // B has nothing yet
  for (const action of ["scan", "click", "fill", "tab_info", "screenshot", "close", "navigate", "focus", "extract_text"]) {
    const payload = action === "click" ? { element_id: "0:1" } : action === "fill" ? { items: [] } : action === "navigate" ? { url: "https://b/" } : {};
    const r = await core.handleMessage(cmd("B", action, payload));
    assert.equal(r.ok, false, action);
    assert.equal(r.error.code, "NO_OWNED_TAB", action);
  }
  const b = await core.handleMessage(cmd("B", "open", { url: "https://b.example/" }));
  assert.notEqual(b.result.tab_id, a.result.tab_id);
  drv.log.length = 0;
  await core.handleMessage(cmd("B", "click", { element_id: "0:7" }));
  assert.ok(drv.log.every(l => l.tabId === b.result.tab_id));
  // re-open for A navigates A's existing tab instead of creating a new one
  const a2 = await core.handleMessage(cmd("A", "open", { url: "https://a.example/apply" }));
  assert.equal(a2.result.tab_id, a.result.tab_id);
  assert.deepEqual(drv.calls.filter(c => c[0] === "navigate").pop(), ["navigate", a.result.tab_id, "https://a.example/apply"]);
  const owned = await core.handleMessage({ type: "command", id: "lo", action: "list_owned", payload: {} });
  assert.deepEqual(owned.result.owned.map(o => o.app_id).sort(), ["A", "B"]);
});

test("per-app sequential execution while different apps run concurrently", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  await core.handleMessage(cmd("A", "open", { url: "https://a/" }));
  await core.handleMessage(cmd("B", "open", { url: "https://b/" }));
  const events = [];
  let active = { A: 0, B: 0 };
  let maxActive = { A: 0, B: 0 };
  let bothActive = false;
  const handler = name => async msg => {
    active[name]++;
    maxActive[name] = Math.max(maxActive[name], active[name]);
    if (active.A && active.B) bothActive = true;
    events.push(name + ":start:" + msg.payload.element_id);
    await sleep(60);
    events.push(name + ":end:" + msg.payload.element_id);
    active[name]--;
    return { ok: true, result: { ok: true } };
  };
  drv.frames.set(1, { 0: handler("A") });
  drv.frames.set(2, { 0: handler("B") });
  const t0 = Date.now();
  const rs = await Promise.all([
    core.handleMessage(cmd("A", "click", { element_id: "0:1" })),
    core.handleMessage(cmd("A", "click", { element_id: "0:2" })),
    core.handleMessage(cmd("B", "click", { element_id: "0:3" })),
    core.handleMessage(cmd("A", "click", { element_id: "0:4" }))
  ]);
  const elapsed = Date.now() - t0;
  assert.ok(rs.every(r => r.ok));
  assert.equal(maxActive.A, 1);
  assert.equal(bothActive, true);
  const aEvents = events.filter(e => e.startsWith("A"));
  assert.deepEqual(aEvents, ["A:start:1", "A:end:1", "A:start:2", "A:end:2", "A:start:4", "A:end:4"]);
  assert.ok(elapsed >= 170 && elapsed < 600, "elapsed " + elapsed);
});

test("frame id prefixing, routing of fill/click/upload, scan merge priority", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  const o = await core.handleMessage(cmd("A", "open", { url: "https://outer/" }));
  const tab = o.result.tab_id;
  const frameHandler = (frameId, s) => msg => {
    if (msg.action === "scan") return { ok: true, result: s };
    if (msg.action === "fill") return { ok: true, result: { results: msg.payload.items.map(i => ({ field_id: i.field_id, ok: true, actual: "f" + frameId })) } };
    if (msg.action === "click") return { ok: true, result: { ok: true, frame: frameId, got: msg.payload.element_id } };
    if (msg.action === "upload") return { ok: true, result: { ok: true, files: [msg.payload.file.name] } };
    if (msg.action === "click_text") return msg.payload.text === "Go" && frameId === 3 ? { ok: true, result: { ok: true, element_id: "9" } } : { ok: false, error: { code: "ELEMENT_NOT_FOUND", message: "nope" } };
    if (msg.action === "extract_text") return { ok: true, result: { text: "text" + frameId, url: "u", title: "t" } };
    return { ok: true, result: {} };
  };
  drv.frames.set(tab, {
    0: frameHandler(0, snap({ page_kind: "job_description", heading: "Careers", fields: [], buttons: [{ id: "1", text: "Apply", kind: "apply", disabled: false, frame: 0 }] })),
    3: frameHandler(3, snap({ ats: "icims", page_kind: "form", heading: "Profile", step: { current: 1, total: 2, label: null },
      fields: [{ id: "4", key: "f_1", type: "text", label: "Name", frame: 0 }, { id: "5", key: "f_2", type: "file", label: "Resume", frame: 0 }],
      errors: [{ text: "Required", field_id: "4" }], buttons: [{ id: "6", text: "Submit", kind: "submit", disabled: false, frame: 0 }] })),
    5: frameHandler(5, snap({ page_kind: "unknown" }))
  });
  const s = await core.handleMessage(cmd("A", "scan", { include_text: false }));
  assert.equal(s.ok, true, JSON.stringify(s));
  const r = s.result;
  assert.equal(r.page_kind, "form");
  assert.equal(r.ats, "icims");
  assert.equal(r.frames, 3);
  assert.deepEqual(r.fields.map(f => [f.id, f.frame]), [["3:4", 3], ["3:5", 3]]);
  assert.deepEqual(r.buttons.map(b => b.id), ["0:1", "3:6"]);
  assert.deepEqual(r.errors, [{ text: "Required", field_id: "3:4" }]);
  assert.deepEqual(r.step, { current: 1, total: 2, label: null });
  assert.equal(r.signals.has_submit, true);
  assert.equal(r.heading, "Profile");

  drv.log.length = 0;
  const f = await core.handleMessage(cmd("A", "fill", { items: [{ field_id: "3:4", value: "x" }, { field_id: "0:9", value: "y" }, { field_id: "bogus", value: 1 }] }));
  assert.deepEqual(f.result.results.map(x => [x.field_id, x.ok, x.actual || x.error.code]), [["3:4", true, "f3"], ["0:9", true, "f0"], ["bogus", false, "BAD_REQUEST"]]);
  assert.deepEqual(drv.log.filter(l => l.action === "fill").map(l => [l.frameId, l.payload.items.map(i => i.field_id)]), [[3, ["4"]], [0, ["9"]]]);
  const c = await core.handleMessage(cmd("A", "click", { element_id: "3:6" }));
  assert.deepEqual(c.result, { ok: true, frame: 3, got: "6" });
  const u = await core.handleMessage(cmd("A", "upload", { field_id: "3:5", file: { name: "cv.pdf", mime: "application/pdf", base64: "AA==" } }));
  assert.deepEqual(u.result, { ok: true, files: ["cv.pdf"] });
  const ct = await core.handleMessage(cmd("A", "click_text", { text: "Go" }));
  assert.equal(ct.result.element_id, "3:9");
  const ct2 = await core.handleMessage(cmd("A", "click_text", { text: "Nothing" }));
  assert.equal(ct2.error.code, "ELEMENT_NOT_FOUND");
  const et = await core.handleMessage(cmd("A", "extract_text", { max_chars: 100 }));
  assert.equal(et.result.text, "text0\n\ntext3\n\ntext5");
  const bad = await core.handleMessage(cmd("A", "click", { element_id: "12" }));
  assert.equal(bad.error.code, "BAD_REQUEST");
});

test("mergeSnapshots priority order", () => {
  const order = [["form", "captcha"], ["confirmation", "form"], ["login", "form"], ["signup", "login"], ["job_description", "form"], ["closed", "verification"], ["unknown", "error"]];
  const expected = ["captcha", "confirmation", "login", "signup", "form", "closed", "error"];
  order.forEach((pair, i) => {
    const m = mergeSnapshots(pair.map((k, j) => ({ frameId: j, s: snap({ page_kind: k }) })), {}, null);
    assert.equal(m.page_kind, expected[i], pair.join(","));
  });
  const cap = mergeSnapshots([{ frameId: 0, s: snap({ page_kind: "form" }) }, { frameId: 2, s: snap({ page_kind: "captcha", captcha: { present: true, kind: "recaptcha", interactive: true, detail: "x" } }) }], {}, null);
  assert.equal(cap.captcha.interactive, true);
});

test("content errors propagate; missing content script -> CONTENT_SCRIPT_UNAVAILABLE", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  const o = await core.handleMessage(cmd("A", "open", { url: "https://x/" }));
  drv.frames.set(o.result.tab_id, { 0: () => ({ ok: false, error: { code: "DISABLED", message: "disabled" } }) });
  let r = await core.handleMessage(cmd("A", "click", { element_id: "0:1" }));
  assert.deepEqual(r.error, { code: "DISABLED", message: "disabled" });
  drv.frames.set(o.result.tab_id, { 0: () => undefined });
  r = await core.handleMessage(cmd("A", "scan"));
  assert.equal(r.error.code, "CONTENT_SCRIPT_UNAVAILABLE");
});

test("tab_closed releases ownership and emits an event; close command does not emit", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  const events = [];
  core.onEvent(e => events.push(e));
  const a = await core.handleMessage(cmd("A", "open", { url: "https://a/" }));
  await core.handleMessage(cmd("B", "open", { url: "https://b/" }));
  drv.userClose(a.result.tab_id);
  assert.deepEqual(events, [{ type: "event", event: "tab_closed", app_id: "A", data: { tab_id: a.result.tab_id } }]);
  const r = await core.handleMessage(cmd("A", "scan"));
  assert.equal(r.error.code, "NO_OWNED_TAB");
  const c = await core.handleMessage(cmd("B", "close"));
  assert.equal(c.ok, true);
  assert.equal(events.length, 1);
  assert.deepEqual(core.ownedList(), []);
  // a tab that vanished without an event -> TAB_CLOSED and release
  const d = await core.handleMessage(cmd("D", "open", { url: "https://d/" }));
  drv.tabs.delete(d.result.tab_id);
  const r2 = await core.handleMessage(cmd("D", "tab_info"));
  assert.equal(r2.error.code, "TAB_CLOSED");
  assert.equal((await core.handleMessage(cmd("D", "tab_info"))).error.code, "NO_OWNED_TAB");
});

test("ownership persists via storage and is restored (dropping vanished tabs)", async () => {
  const storage = memStorage();
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage });
  const a = await core.handleMessage(cmd("A", "open", { url: "https://a/" }));
  const b = await core.handleMessage(cmd("B", "open", { url: "https://b/", strategy: "tab" }));
  assert.deepEqual(storage.m.get(STORAGE_KEY).map(o => o.app_id).sort(), ["A", "B"]);
  // "restart": new core, same storage and browser; tab B vanished meanwhile
  drv.tabs.delete(b.result.tab_id);
  const core2 = new BridgeCore({ driver: drv, storage });
  await core2.init();
  assert.deepEqual(core2.helloMessage().owned, [{ app_id: "A", tab_id: a.result.tab_id, window_id: a.result.window_id, url: "https://a/" }]);
  assert.deepEqual(storage.m.get(STORAGE_KEY).map(o => o.app_id), ["A"]);
  const r = await core2.handleMessage(cmd("A", "tab_info"));
  assert.equal(r.result.tab_id, a.result.tab_id);
});

test("timeouts: slow content -> TIMEOUT, lock released for the next command", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  const o = await core.handleMessage(cmd("A", "open", { url: "https://x/" }));
  let n = 0;
  drv.frames.set(o.result.tab_id, { 0: async () => { n++; if (n === 1) await sleep(10000); return { ok: true, result: { ok: true } }; } });
  const t0 = Date.now();
  const r = await core.handleMessage(cmd("A", "click", { element_id: "0:1" }, 300));
  assert.equal(r.error.code, "TIMEOUT");
  assert.ok(Date.now() - t0 < 1500);
  const t1 = Date.now();
  const r2 = await core.handleMessage(cmd("A", "click", { element_id: "0:1" }, 8000));
  assert.equal(r2.ok, true);
  assert.ok(Date.now() - t1 < 7000, "lock held at most timeout + grace");
});

test("screenshot, focus, wait_ready, tab_info, navigate", async () => {
  const drv = makeDriver();
  const core = new BridgeCore({ driver: drv, storage: memStorage() });
  const events = [];
  core.onEvent(e => events.push(e));
  await core.handleMessage(cmd("A", "open", { url: "https://x/", active: false }));
  assert.equal((await core.handleMessage(cmd("A", "screenshot", { format: "png" }))).result.data_url, "data:image/png;base64,AA==");
  assert.deepEqual((await core.handleMessage(cmd("A", "focus"))).result, { ok: true });
  const w = await core.handleMessage(cmd("A", "wait_ready", { timeout_ms: 2000, quiet_ms: 100 }));
  assert.deepEqual(w.result, { url: "https://x/", title: "T1", ready: true });
  const n = await core.handleMessage(cmd("A", "navigate", { url: "https://x/2" }));
  assert.equal(n.result.url, "https://x/2");
  const ti = await core.handleMessage(cmd("A", "tab_info"));
  assert.equal(ti.result.url, "https://x/2");
  core.handleTabNavigated(1, "https://x/3");
  assert.deepEqual(events.pop(), { type: "event", event: "tab_navigated", app_id: "A", data: { url: "https://x/3" } });
});
