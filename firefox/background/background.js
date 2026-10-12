/*
 * ApplyPilot background page (Firefox, MV2, persistent).
 *
 * - FirefoxDriver: implements the BridgeCore driver interface with
 *   browser.windows / tabs / webNavigation / tabs.executeScript.
 * - WebSocket client to the local backend: ws://<backend>/bridge?token=<token>,
 *   sends `hello` on every (re)connect, routes commands to BridgeCore,
 *   reconnects with exponential backoff + jitter (cap 15 s).
 * - runtime.onMessage {type: "bridge_status"} -> {connected, url, owned, last_error}
 *   runtime.onMessage {type: "bridge_reconnect"} -> forces a reconnect.
 *
 * Settings (browser.storage.local): backendUrl (default http://127.0.0.1:8765), token, debug.
 */
/* global browser, ApplyPilotBridgeCore, ApplyPilotAttention */
(function () {
  "use strict";

  var DEFAULT_BACKEND = "http://127.0.0.1:8765";
  var CONTENT_FILES = ["/content/adapters.js", "/content/dom-engine.js", "/content/content.js"];
  var CAPTCHA_URL_RE = /recaptcha\/(api2|enterprise)\/|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs\.com|funcaptcha\.com/i;
  var VERSION = (browser.runtime.getManifest && browser.runtime.getManifest().version) || "1.0.0";

  var debug = false;
  function log() {
    if (!debug) return;
    try { console.debug.apply(console, ["[ApplyPilot bg]"].concat([].slice.call(arguments))); } catch (e) { /* ignore */ }
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function err(code, message, details) { var e = new Error(message); e.code = code; if (details) e.details = details; return e; }

  // ------------------------------------------------------------------ driver
  function FirefoxDriver() {
    this.sharedWindowId = null; // dedicated window for strategy "tab"
    this.pendingNav = new Map(); // tabId -> {since, sawLoading}
    this.removedCbs = [];
    this.navCbs = [];
    var self = this;
    browser.tabs.onRemoved.addListener(function (tabId) {
      self.pendingNav.delete(tabId);
      self.removedCbs.forEach(function (cb) { try { cb(tabId); } catch (e) { /* ignore */ } });
    });
    browser.tabs.onUpdated.addListener(function (tabId, info) {
      var p = self.pendingNav.get(tabId);
      if (p && info.status === "loading") p.sawLoading = true;
    });
    browser.webNavigation.onCommitted.addListener(function (d) {
      if (d.frameId !== 0) return;
      var p = self.pendingNav.get(d.tabId);
      if (p) p.sawLoading = true;
      self.navCbs.forEach(function (cb) { try { cb(d.tabId, d.url); } catch (e) { /* ignore */ } });
    });
    browser.windows.onRemoved.addListener(function (windowId) {
      if (windowId === self.sharedWindowId) self.sharedWindowId = null;
    });
  }

  FirefoxDriver.prototype.onTabRemoved = function (cb) { this.removedCbs.push(cb); };
  FirefoxDriver.prototype.onTabNavigated = function (cb) { this.navCbs.push(cb); };

  FirefoxDriver.prototype.openTab = async function (url, opts) {
    opts = opts || {};
    var active = opts.active !== false;
    this._markNav(null);
    if (opts.strategy === "tab") {
      var win = null;
      if (this.sharedWindowId != null) {
        try { win = await browser.windows.get(this.sharedWindowId); } catch (e) { win = null; }
      }
      if (!win) {
        win = await browser.windows.create({ url: url, focused: active, type: "normal" });
        this.sharedWindowId = win.id;
        var first = win.tabs && win.tabs[0];
        if (!first) first = (await browser.tabs.query({ windowId: win.id }))[0];
        this._markNav(first.id);
        return { tab_id: first.id, window_id: win.id };
      }
      var tab = await browser.tabs.create({ windowId: win.id, url: url, active: true });
      if (active) { try { await browser.windows.update(win.id, { focused: true }); } catch (e) { /* ignore */ } }
      this._markNav(tab.id);
      return { tab_id: tab.id, window_id: win.id };
    }
    var w = await browser.windows.create({ url: url, focused: active, type: "normal" });
    var t = w.tabs && w.tabs[0];
    if (!t) t = (await browser.tabs.query({ windowId: w.id }))[0];
    this._markNav(t.id);
    return { tab_id: t.id, window_id: w.id };
  };

  FirefoxDriver.prototype._markNav = function (tabId) {
    if (tabId == null) return;
    this.pendingNav.set(tabId, { since: Date.now(), sawLoading: false });
  };

  FirefoxDriver.prototype.navigate = async function (tabId, url) {
    this._markNav(tabId);
    await browser.tabs.update(tabId, { url: url });
  };

  FirefoxDriver.prototype.waitForLoad = async function (tabId, timeoutMs) {
    var end = Date.now() + Math.max(500, timeoutMs || 20000);
    var completeSince = 0;
    while (Date.now() < end) {
      var tab;
      try { tab = await browser.tabs.get(tabId); } catch (e) { throw err("TAB_CLOSED", "tab was closed"); }
      var p = this.pendingNav.get(tabId);
      // after a navigation request, ignore the previous document's "complete" until
      // a load started (or 3 s passed: same-document navigations never load)
      var navOk = !p || p.sawLoading || Date.now() - p.since > 3000;
      if (tab.status === "complete" && navOk && tab.url && tab.url !== "about:blank") {
        if (!completeSince) completeSince = Date.now();
        if (Date.now() - completeSince >= 250) {
          this.pendingNav.delete(tabId);
          return { complete: true };
        }
      } else {
        completeSince = 0;
      }
      await sleep(150);
    }
    return { complete: false };
  };

  FirefoxDriver.prototype.getTab = async function (tabId) {
    try {
      var t = await browser.tabs.get(tabId);
      return { tab_id: t.id, window_id: t.windowId, url: t.url || "", title: t.title || "", status: t.status || null };
    } catch (e) {
      return null;
    }
  };

  FirefoxDriver.prototype.focus = async function (tabId, windowId) {
    await browser.tabs.update(tabId, { active: true });
    if (windowId != null) await browser.windows.update(windowId, { focused: true });
    var tab = await browser.tabs.get(tabId);
    var win = windowId != null ? await browser.windows.get(windowId) : null;
    return { confirmed: !!(tab.active && (!win || win.focused)), tab_active: !!tab.active,
      window_focused: win ? !!win.focused : null };
  };

  FirefoxDriver.prototype.close = async function (tabId) {
    await browser.tabs.remove(tabId);
  };

  FirefoxDriver.prototype.screenshot = async function (tabId, windowId, opts) {
    var o = { format: opts && opts.format === "png" ? "png" : "jpeg" };
    if (o.format === "jpeg") o.quality = Math.max(10, Math.min(100, (opts && opts.quality) || 70));
    var dataUrl;
    if (typeof browser.tabs.captureTab === "function") {
      dataUrl = await browser.tabs.captureTab(tabId, o);
    } else {
      await browser.tabs.update(tabId, { active: true });
      await sleep(150);
      dataUrl = await browser.tabs.captureVisibleTab(windowId, o);
    }
    return { data_url: dataUrl };
  };

  FirefoxDriver.prototype.getFrames = async function (tabId) {
    var frames = await browser.webNavigation.getAllFrames({ tabId: tabId });
    return (frames || []).filter(function (f) { return !f.errorOccurred; }).map(function (f) {
      return { frameId: f.frameId, url: f.url, parentFrameId: f.parentFrameId };
    });
  };

  FirefoxDriver.prototype._inject = async function (tabId, frameId) {
    var frames = await this.getFrames(tabId);
    var frame = frames.filter(function (f) { return f.frameId === frameId; })[0];
    var frameUrl = frame && frame.url || "";
    if (!/^https?:/i.test(frameUrl)) {
      throw err("CONTENT_SCRIPT_UNAVAILABLE", "Firefox does not allow ApplyPilot to script " + (frameUrl || "this page"));
    }
    var origin;
    try { origin = new URL(frameUrl).origin + "/*"; } catch (e) { origin = null; }
    if (origin && browser.permissions && browser.permissions.contains) {
      var allowed = await browser.permissions.contains({ origins: [origin] });
      if (!allowed) {
        throw err("HOST_PERMISSION_REQUIRED", "ApplyPilot needs Firefox site access for " + origin +
          ". Grant site access from Firefox's Extensions panel, or reinstall the current extension build and " +
          "accept its requested access, then reload this tab.",
          { origin: origin, tab_id: tabId, frame_id: frameId });
      }
    }
    for (var i = 0; i < CONTENT_FILES.length; i++) {
      await browser.tabs.executeScript(tabId, { file: CONTENT_FILES[i], frameId: frameId, runAt: "document_idle", matchAboutBlank: true });
    }
  };

  FirefoxDriver.prototype.sendToFrame = async function (tabId, frameId, message) {
    var self = this;
    async function send() {
      return browser.tabs.sendMessage(tabId, message, { frameId: frameId });
    }
    var reply;
    try {
      reply = await send();
    } catch (e) {
      reply = undefined;
      log("sendMessage failed", tabId, frameId, e && e.message);
    }
    if (reply !== undefined && reply !== null) return reply;
    // no listener: content scripts missing (tab predates install, or frame not matched)
    var frames = [];
    try { frames = await self.getFrames(tabId); } catch (e) { frames = []; }
    var fr = frames.filter(function (f) { return f.frameId === frameId; })[0];
    if (!fr) throw err("CONTENT_SCRIPT_UNAVAILABLE", "frame " + frameId + " not found");
    if (CAPTCHA_URL_RE.test(fr.url || "")) throw err("UNSUPPORTED", "captcha frames are never touched");
    if (!/^(https?:|about:blank|about:srcdoc)/.test(fr.url || "")) throw err("CONTENT_SCRIPT_UNAVAILABLE", "cannot script " + (fr.url || "this frame"));
    try {
      await self._inject(tabId, frameId);
    } catch (e) {
      if (e && e.code) throw e;
      throw err("CONTENT_SCRIPT_UNAVAILABLE", "injection failed: " + (e && e.message || e));
    }
    try {
      reply = await send();
    } catch (e) {
      throw err("CONTENT_SCRIPT_UNAVAILABLE", "content script did not answer: " + (e && e.message || e));
    }
    if (reply === undefined || reply === null) throw err("CONTENT_SCRIPT_UNAVAILABLE", "content script did not answer");
    return reply;
  };

  // ------------------------------------------------------------------ storage
  var storage = {
    get: async function (key) {
      var r = await browser.storage.local.get(key);
      return r ? r[key] : undefined;
    },
    set: async function (key, value) {
      var o = {};
      o[key] = value;
      await browser.storage.local.set(o);
    }
  };

  var driver = new FirefoxDriver();
  var core = new ApplyPilotBridgeCore.BridgeCore({
    driver: driver,
    storage: storage,
    version: VERSION,
    browserName: "firefox",
    log: function () { log.apply(null, arguments); }
  });

  // ------------------------------------------------------------------ websocket client
  var ws = null;
  var connected = false;
  var lastError = null;
  var attempt = 0;
  var reconnectTimer = null;
  var currentUrl = null; // without token
  var generation = 0;

  function backoffMs(n) {
    var base = Math.min(15000, 500 * Math.pow(2, Math.min(n, 6)));
    var jitter = base * (0.7 + Math.random() * 0.6);
    return Math.min(15000, Math.round(jitter));
  }

  async function settings() {
    var s = await browser.storage.local.get(["backendUrl", "token", "debug"]);
    debug = !!s.debug;
    return { backendUrl: (s.backendUrl || DEFAULT_BACKEND).trim().replace(/\/+$/, ""), token: (s.token || "").trim() };
  }

  function wsUrlFor(backendUrl) {
    var u = backendUrl;
    if (/^https:/i.test(u)) u = "wss:" + u.slice(6);
    else if (/^http:/i.test(u)) u = "ws:" + u.slice(5);
    else if (!/^wss?:/i.test(u)) u = "ws://" + u;
    return u + "/bridge";
  }

  function send(obj) {
    if (!ws || ws.readyState !== 1) return false;
    try { ws.send(JSON.stringify(obj)); return true; } catch (e) { return false; }
  }

  core.onEvent(function (evt) { send(evt); });

  function scheduleReconnect() {
    if (reconnectTimer) return;
    var wait = backoffMs(attempt++);
    log("reconnect in", wait);
    reconnectTimer = setTimeout(function () { reconnectTimer = null; connect(); }, wait);
  }

  function teardown() {
    generation++;
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws) {
      var old = ws;
      ws = null;
      try { old.close(1000, "reconnect"); } catch (e) { /* ignore */ }
    }
    connected = false;
  }

  var tokenRejected = false;

  async function tryPair(backendUrl) {
    try {
      var ctrl = new AbortController();
      var t = setTimeout(function () { ctrl.abort(); }, 4000);
      var r = await fetch(backendUrl + "/api/pair", { method: "POST", signal: ctrl.signal });
      clearTimeout(t);
      if (!r.ok) return null;
      var body = await r.json();
      return body && typeof body.token === "string" && body.token ? body.token : null;
    } catch (e) {
      return null;
    }
  }

  async function connect() {
    // Idempotent: never open a second socket while one is open or connecting (a second socket makes the
    // backend drop the first, aborting in-flight commands). teardown() clears `ws` when a reconnect is wanted.
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
    var gen = ++generation;
    var s;
    try { s = await settings(); } catch (e) { s = { backendUrl: DEFAULT_BACKEND, token: "" }; }
    if (gen !== generation) return;
    currentUrl = wsUrlFor(s.backendUrl);
    if (!s.token || tokenRejected) {
      // One-click pairing: works only while the backend's pairing window is open
      // (`applypilot run --pair` / `applypilot pair`) and only for extension origins.
      var paired = await tryPair(s.backendUrl);
      if (gen !== generation) return;
      if (paired) {
        tokenRejected = false;
        await browser.storage.local.set({ token: paired }); // storage.onChanged reconnects
        return;
      }
      if (!s.token) {
        lastError = "no token configured (run `applypilot pair`, or paste the token in ApplyPilot options)";
        connected = false;
        scheduleReconnect();
        return;
      }
    }
    var sock;
    try {
      sock = new WebSocket(currentUrl + "?token=" + encodeURIComponent(s.token));
    } catch (e) {
      lastError = "invalid backend url: " + (e && e.message || e);
      scheduleReconnect();
      return;
    }
    ws = sock;
    sock.onopen = async function () {
      if (ws !== sock) return;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      try { await core.init(); } catch (e) { /* ignore */ }
      connected = true;
      attempt = 0;
      lastError = null;
      send(core.helloMessage());
      log("connected", currentUrl);
    };
    sock.onmessage = function (ev) {
      if (ws !== sock) return;
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      core.handleMessage(msg).then(function (reply) {
        if (reply) send(reply);
      }, function (e) {
        if (msg && msg.type === "command") send({ type: "result", id: msg.id, ok: false, error: { code: "INTERNAL", message: String(e && e.message || e) } });
      });
    };
    sock.onerror = function () {
      if (ws !== sock) return;
      lastError = "connection error (is the backend running at " + currentUrl.replace(/\/bridge$/, "") + "?)";
    };
    sock.onclose = function (ev) {
      if (ws !== sock) return;
      ws = null;
      connected = false;
      if (ev && ev.code === 4401) tokenRejected = true;
      if (ev && ev.code === 4409) {
        lastError = "another Firefox profile is already connected to this ApplyPilot backend";
        attempt = Math.max(attempt, 6); // back off (~15 s) instead of fighting over the bridge
      }
      if (ev && ev.code && ev.code !== 1000 && ev.code !== 1006) lastError = "closed by backend (" + ev.code + (ev.reason ? ": " + ev.reason : "") + ")";
      else if (!lastError) lastError = "disconnected";
      scheduleReconnect();
    };
  }

  browser.storage.onChanged.addListener(function (changes, area) {
    if (area !== "local") return;
    if (changes.backendUrl || changes.token) {
      attempt = 0;
      teardown();
      connect();
    }
    if (changes.debug) debug = !!changes.debug.newValue;
  });

  browser.runtime.onMessage.addListener(function (msg, sender) {
    if (!msg || typeof msg !== "object") return undefined;
    // only extension pages (popup / options / dashboard) may query the bridge
    if (sender && sender.tab && sender.url && !/^moz-extension:/.test(sender.url)) return undefined;
    if (msg.type === "bridge_status") {
      return Promise.resolve({ connected: connected, url: currentUrl, owned: core.ownedList(), last_error: lastError, version: VERSION });
    }
    if (msg.type === "bridge_reconnect") {
      attempt = 0;
      teardown();
      connect();
      return Promise.resolve({ ok: true });
    }
    if (msg.type === "bridge_user_action" && msg.action) {
      send({ type: "event", event: "user_action", app_id: msg.app_id || undefined, data: { action: String(msg.action) } });
      return Promise.resolve({ ok: true, sent: connected });
    }
    return undefined;
  });

  // ---- attention badge + notifications -------------------------------------------------
  // Polls the backend status so a CAPTCHA / missing fact is noticed even with the popup closed.
  var notified = {};
  async function pollAttention() {
    var s;
    try { s = await settings(); } catch (e) { return; }
    if (!s.token) return;
    try {
      var r = await fetch(s.backendUrl + "/api/status", { headers: { "X-ApplyPilot-Token": s.token } });
      if (!r.ok) return;
      var st = await r.json();
      var items = (st.needs_user || []).filter(function (n) { return n.kind !== "review"; });
      var ready = (st.needs_user || []).length - items.length;
      browser.browserAction.setBadgeText({ text: items.length ? String(items.length) : (ready ? "✓" : "") });
      browser.browserAction.setBadgeBackgroundColor({ color: items.length ? "#c62828" : "#2e7d32" });
      var title = "ApplyPilot — " + (st.control || "") + (items.length ? " — " + items.length + " need attention" : "");
      browser.browserAction.setTitle({ title: title });
      items.forEach(function (n) {
        var key = ApplyPilotAttention.key(n);
        if (notified[key]) return;
        notified[key] = true;
        if (browser.notifications) {
          browser.notifications.create(
            "ap-" + n.application_id,
            ApplyPilotAttention.notification(n, browser.runtime.getURL("icons/icon-96.png"))
          ).catch(function () {});
        }
      });
    } catch (e) { /* backend offline: the popup explains */ }
  }
  if (browser.notifications && browser.notifications.onClicked) {
    browser.notifications.onClicked.addListener(function (id) {
      if (id.indexOf("ap-") === 0) {
        var appId = id.slice(3);
        var owned = core.owned.get(appId);
        if (owned) {
          Promise.resolve(driver.focus(owned.tab_id, owned.window_id)).catch(function () {
            return browser.tabs.create({ url: browser.runtime.getURL("ui/dashboard.html#/application/" + appId) });
          });
        } else {
          browser.tabs.create({ url: browser.runtime.getURL("ui/dashboard.html#/application/" + appId) });
        }
      }
    });
  }
  setInterval(pollAttention, 10000);

  core.init().then(connect, connect);
})();
