#!/usr/bin/env node
"use strict";
/*
 * fake_browser.cjs — a "fake Firefox" for backend integration tests.
 *
 * Runs the REAL firefox/background/bridge-core.js and the REAL content scripts
 * (adapters.js, dom-engine.js, content.js) on top of jsdom, connected to the
 * backend's bridge WebSocket exactly like the extension.
 *
 *   node tests/js/fake_browser.cjs --url ws://127.0.0.1:PORT/bridge --token TOKEN [--log]
 *
 * Prints "FAKE_BROWSER_READY" on stdout once the first connection is open and
 * `hello` was sent. Reconnects when the server restarts. Exits cleanly on SIGTERM/SIGINT.
 *
 * Emulation notes:
 *   - every "tab" is a JSDOM.fromURL document (runScripts "dangerously", resources "usable",
 *     pretendToBeVisual) sharing one cookie jar; localStorage is per document.
 *   - frame 0 is the top document; same-origin iframes get frame ids 1, 2, ... (stable per
 *     iframe element) and receive the content scripts once loaded.
 *   - anchor navigation: when a click/click_text/click_point result carries an `href`
 *     (anchor click that was not prevented) the tab (or iframe) loads that URL.
 *     Form-submission navigation and `location` assignments are NOT emulated.
 *   - screenshots return a 1x1 PNG placeholder.
 */
const fs = require("node:fs");
const path = require("node:path");

const FIREFOX = path.resolve(__dirname, "..", "..", "firefox");
const { JSDOM, VirtualConsole, CookieJar } = require("jsdom");
const { BridgeCore } = require(path.join(FIREFOX, "background", "bridge-core.js"));

const CONTENT_SOURCES = ["adapters.js", "dom-engine.js", "content.js"].map(f => ({
  name: f, code: fs.readFileSync(path.join(FIREFOX, "content", f), "utf8")
}));
const PLACEHOLDER_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

function parseArgs(argv) {
  const out = { url: null, token: null, log: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--url") out.url = argv[++i];
    else if (a === "--token") out.token = argv[++i];
    else if (a === "--log") out.log = true;
    else if (a.startsWith("--url=")) out.url = a.slice(6);
    else if (a.startsWith("--token=")) out.token = a.slice(8);
  }
  return out;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
function sameDocument(a, b) {
  return String(a).split("#")[0] === String(b).split("#")[0];
}

class FakeDriver {
  constructor({ log = () => {} } = {}) {
    this.log = log;
    this.tabs = new Map();
    this.nextTabId = 1;
    this.nextWindowId = 100;
    this.sharedWindowId = null;
    this.cookieJar = new CookieJar();
    this.removedCbs = [];
    this.navCbs = [];
  }
  onTabRemoved(cb) { this.removedCbs.push(cb); }
  onTabNavigated(cb) { this.navCbs.push(cb); }

  async openTab(url, opts = {}) {
    let windowId;
    if (opts.strategy === "tab") {
      if (this.sharedWindowId == null) this.sharedWindowId = this.nextWindowId++;
      windowId = this.sharedWindowId;
    } else {
      windowId = this.nextWindowId++;
    }
    const tab = { id: this.nextTabId++, windowId, dom: null, url, status: "loading", loadPromise: null, frameIds: new WeakMap(), nextFrameId: 1, gen: 0 };
    this.tabs.set(tab.id, tab);
    this._load(tab, url);
    return { tab_id: tab.id, window_id: windowId };
  }

  _load(tab, url) {
    const gen = ++tab.gen;
    tab.status = "loading";
    tab.url = url;
    const old = tab.dom;
    tab.loadPromise = (async () => {
      const vc = new VirtualConsole();
      vc.on("jsdomError", e => this.log("page error:", String(e && e.message || e).slice(0, 300)));
      let dom;
      try {
        dom = await JSDOM.fromURL(url, {
          runScripts: "dangerously", resources: "usable", pretendToBeVisual: true,
          virtualConsole: vc, cookieJar: this.cookieJar
        });
      } catch (e) {
        this.log("load failed", url, e && e.message);
        dom = new JSDOM(`<!doctype html><title>Problem loading page</title><h1>Unable to connect</h1><p>${String(e && e.message || e).replace(/[<&]/g, "")}</p>`, { url: /^https?:/.test(url) ? url : "about:blank", pretendToBeVisual: true });
      }
      if (gen !== tab.gen || !this.tabs.has(tab.id)) { try { dom.window.close(); } catch (e) { /* ignore */ } return; }
      tab.dom = dom;
      if (old) { try { old.window.close(); } catch (e) { /* ignore */ } }
      const win = dom.window;
      if (win.document.readyState !== "complete") {
        await Promise.race([
          new Promise(r => win.addEventListener("load", r, { once: true })),
          sleep(20000)
        ]);
      }
      if (gen !== tab.gen) return;
      this._inject(win);
      tab.url = win.location.href;
      tab.status = "complete";
      this.navCbs.forEach(cb => { try { cb(tab.id, tab.url); } catch (e) { /* ignore */ } });
    })();
    return tab.loadPromise;
  }

  _inject(win) {
    if (!win || win.__apFakeInjected) return;
    let doc;
    try { doc = win.document; } catch (e) { return; }
    if (!doc || doc.readyState === "loading") return;
    const listeners = [];
    win.__apFakeInjected = true;
    win.__apListeners = listeners;
    win.browser = { runtime: { onMessage: { addListener: fn => listeners.push(fn), removeListener: () => {} } } };
    for (const src of CONTENT_SOURCES) {
      try {
        win.eval(src.code + "\n//# sourceURL=applypilot-" + src.name);
      } catch (e) {
        this.log("inject failed", src.name, e && e.message);
      }
    }
  }

  _frameList(tab) {
    const out = [];
    if (!tab.dom) return out;
    const top = tab.dom.window;
    out.push({ frameId: 0, url: tab.url, parentFrameId: -1, win: top, el: null });
    const walk = (win, parentId, depth) => {
      if (depth > 5) return;
      let iframes = [];
      try { iframes = [...win.document.querySelectorAll("iframe, frame")]; } catch (e) { return; }
      for (const el of iframes) {
        let cw = null, href = "";
        try { cw = el.contentWindow; href = cw && cw.location && cw.location.href; } catch (e) { cw = null; }
        if (!cw) continue;
        let id = tab.frameIds.get(el);
        if (id == null) { id = tab.nextFrameId++; tab.frameIds.set(el, id); }
        out.push({ frameId: id, url: href || el.getAttribute("src") || "about:blank", parentFrameId: parentId, win: cw, el });
        walk(cw, id, depth + 1);
      }
    };
    walk(top, 0, 0);
    return out;
  }

  async navigate(tabId, url) {
    const tab = this._tab(tabId);
    this._load(tab, url);
  }

  async waitForLoad(tabId, timeoutMs) {
    const tab = this._tab(tabId);
    const end = Date.now() + Math.max(200, timeoutMs || 20000);
    while (Date.now() < end) {
      const p = tab.loadPromise;
      await Promise.race([p, sleep(Math.max(50, end - Date.now()))]);
      if (tab.status === "complete" && tab.loadPromise === p) {
        // give same-origin iframes a moment to finish loading
        const frames = this._frameList(tab);
        const pending = frames.filter(f => { try { return f.win.document.readyState !== "complete"; } catch (e) { return false; } });
        if (!pending.length || Date.now() > end - 100) return { complete: true };
        await sleep(50);
        continue;
      }
      if (!this.tabs.has(tabId)) throw Object.assign(new Error("tab closed"), { code: "TAB_CLOSED" });
    }
    return { complete: tab.status === "complete" };
  }

  async getTab(tabId) {
    const tab = this.tabs.get(tabId);
    if (!tab) return null;
    let title = "";
    try { title = tab.dom ? tab.dom.window.document.title : ""; } catch (e) { title = ""; }
    let url = tab.url;
    try { if (tab.dom && tab.status === "complete") url = tab.dom.window.location.href; } catch (e) { /* ignore */ }
    return { tab_id: tab.id, window_id: tab.windowId, url, title, status: tab.status };
  }

  async focus() {}

  async close(tabId) {
    const tab = this.tabs.get(tabId);
    if (!tab) return;
    this.tabs.delete(tabId);
    tab.gen++;
    try { if (tab.dom) tab.dom.window.close(); } catch (e) { /* ignore */ }
    this.removedCbs.forEach(cb => { try { cb(tabId); } catch (e) { /* ignore */ } });
  }

  async screenshot() {
    return { data_url: PLACEHOLDER_PNG, width: 1, height: 1 };
  }

  async getFrames(tabId) {
    const tab = this._tab(tabId);
    return this._frameList(tab).map(f => ({ frameId: f.frameId, url: f.url, parentFrameId: f.parentFrameId }));
  }

  async sendToFrame(tabId, frameId, message) {
    const tab = this._tab(tabId);
    if (tab.status !== "complete") await this.waitForLoad(tabId, 15000);
    const frame = this._frameList(tab).find(f => f.frameId === frameId);
    if (!frame) throw Object.assign(new Error("frame " + frameId + " not found"), { code: "CONTENT_SCRIPT_UNAVAILABLE" });
    this._inject(frame.win);
    const listeners = frame.win.__apListeners || [];
    if (!listeners.length) throw Object.assign(new Error("content script not ready"), { code: "CONTENT_SCRIPT_UNAVAILABLE" });
    const msg = JSON.parse(JSON.stringify(message));
    let reply;
    for (const fn of listeners) {
      const r = fn(msg, { id: "applypilot@local" });
      if (r !== undefined) { reply = await r; break; }
    }
    reply = reply === undefined ? undefined : JSON.parse(JSON.stringify(reply));
    // anchor navigation emulation
    if (reply && reply.ok && reply.result && reply.result.href && /^(click|click_text|click_point)$/.test(message.action)) {
      const href = reply.result.href;
      if (/^https?:/i.test(href)) {
        if (frameId === 0) {
          if (!sameDocument(href, tab.url)) {
            this.log("navigating to", href);
            this._load(tab, href);
          }
        } else if (frame.el) {
          frame.el.setAttribute("src", href);
        }
      }
    }
    return reply;
  }

  _tab(tabId) {
    const tab = this.tabs.get(tabId);
    if (!tab) throw Object.assign(new Error("tab " + tabId + " does not exist"), { code: "TAB_CLOSED" });
    return tab;
  }

  closeAll() {
    for (const id of [...this.tabs.keys()]) {
      const tab = this.tabs.get(id);
      this.tabs.delete(id);
      tab.gen++;
      try { if (tab.dom) tab.dom.window.close(); } catch (e) { /* ignore */ }
    }
  }
}

function startFakeBrowser({ url, token, log = () => {}, onReady = () => {}, WebSocketImpl } = {}) {
  if (!url) throw new Error("--url required");
  const WS = WebSocketImpl || require("ws");
  const driver = new FakeDriver({ log });
  const core = new BridgeCore({ driver, browserName: "fake-firefox", log });
  let ws = null, stopped = false, attempt = 0, readyFired = false, timer = null;
  const wsUrl = token ? url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(token) : url;

  function send(obj) {
    if (ws && ws.readyState === 1) {
      try { ws.send(JSON.stringify(obj)); } catch (e) { /* ignore */ }
    }
  }
  core.onEvent(send);

  function connect() {
    if (stopped) return;
    let sock;
    try { sock = new WS(wsUrl); } catch (e) { log("connect error", e.message); return retry(); }
    ws = sock;
    sock.on("open", async () => {
      attempt = 0;
      await core.init();
      send(core.helloMessage());
      log("connected", url);
      if (!readyFired) { readyFired = true; onReady(); }
    });
    sock.on("message", data => {
      let msg;
      try { msg = JSON.parse(String(data)); } catch (e) { return; }
      if (msg && msg.type === "command" && msg.action === "__test_eval") {
        // TEST-ONLY (fake browser): simulate a human acting in the page, e.g. solving a CAPTCHA.
        // The real extension has no such action.
        const owned = core.ownedList().find(o => o.app_id === msg.app_id);
        const tab = owned && driver.tabs.get(owned.tab_id);
        let reply;
        try {
          const value = tab && tab.dom ? tab.dom.window.eval(String((msg.payload || {}).code || "")) : undefined;
          reply = { type: "result", id: msg.id, ok: !!tab, result: { value: value === undefined ? null : value },
                    error: tab ? undefined : { code: "NO_OWNED_TAB", message: "no tab" } };
        } catch (e) {
          reply = { type: "result", id: msg.id, ok: false, error: { code: "INTERNAL", message: String(e && e.message) } };
        }
        send(reply);
        return;
      }
      core.handleMessage(msg).then(reply => { if (reply && ws === sock) send(reply); }, e => log("handle error", e && e.message));
    });
    sock.on("error", e => log("ws error", e && e.message));
    sock.on("close", () => {
      if (ws === sock) ws = null;
      log("disconnected");
      retry();
    });
  }
  function retry() {
    if (stopped || timer) return;
    const wait = Math.min(3000, 200 * Math.pow(2, attempt++)) * (0.8 + Math.random() * 0.4);
    timer = setTimeout(() => { timer = null; connect(); }, wait);
  }
  connect();
  return {
    core, driver,
    stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
      if (ws) { try { ws.close(1000, "shutdown"); } catch (e) { /* ignore */ } }
      driver.closeAll();
    }
  };
}

module.exports = { FakeDriver, startFakeBrowser };

if (require.main === module) {
  const args = parseArgs(process.argv.slice(2));
  if (!args.url) {
    process.stderr.write("usage: node tests/js/fake_browser.cjs --url ws://127.0.0.1:PORT/bridge --token TOKEN [--log]\n");
    process.exit(2);
  }
  const log = args.log ? (...a) => process.stderr.write("[fake_browser] " + a.map(String).join(" ") + "\n") : () => {};
  const fb = startFakeBrowser({
    url: args.url, token: args.token, log,
    onReady: () => process.stdout.write("FAKE_BROWSER_READY\n")
  });
  const shutdown = () => { fb.stop(); setTimeout(() => process.exit(0), 50); };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}
