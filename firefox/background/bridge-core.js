/*
 * ApplyPilot bridge core — transport- and browser-agnostic command handling.
 *
 * UMD module: background global `ApplyPilotBridgeCore`, or `module.exports` in Node.
 *
 *   const core = new BridgeCore({driver, storage, log?, version?, browserName?, defaultTimeoutMs?})
 *   await core.init()                     // restore ownership from storage, drop vanished tabs
 *   core.helloMessage()                   // {type: "hello", version, browser, owned}
 *   await core.handleMessage(msgObj)      // -> reply object ({type:"result"|"pong"}) or null
 *   core.onEvent(cb)                      // cb({type:"event", event, app_id, data}) for tab_closed / tab_navigated
 *   core.ownedList()                      // [{app_id, tab_id, window_id, url}]
 *   core.handleTabRemoved(tabId)          // also wired automatically via driver.onTabRemoved
 *   core.handleTabNavigated(tabId, url)   // also wired via driver.onTabNavigated (optional)
 *
 * Driver interface (all methods async unless noted):
 *   openTab(url, {strategy: "window"|"tab", active: bool}) -> {tab_id, window_id}
 *   navigate(tabId, url)                  -> void (starts navigation)
 *   waitForLoad(tabId, timeoutMs)         -> {complete: bool}  (top document finished loading)
 *   getTab(tabId)                         -> {tab_id, window_id, url, title, status} | null if gone
 *   focus(tabId, windowId)                -> void
 *   close(tabId, windowId)                -> void
 *   screenshot(tabId, windowId, {format, quality}) -> {data_url, width?, height?}
 *   getFrames(tabId)                      -> [{frameId, url, parentFrameId?}]
 *   sendToFrame(tabId, frameId, message)  -> content reply ({ok, result} | {ok:false, error}); may throw
 *   onTabRemoved(cb(tabId))               -> sync registration
 *   onTabNavigated?(cb(tabId, url))       -> optional sync registration (top-frame commits)
 *
 * Storage interface: async get(key) -> value | undefined, async set(key, value).
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.ApplyPilotBridgeCore = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var STORAGE_KEY = "applypilot_owned";
  var VERSION = "1.0.0";
  var CAPTCHA_FRAME_RE = /recaptcha\/(api2|enterprise)\/|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs\.com|funcaptcha\.com/i;

  var DEFAULT_TIMEOUTS = {
    open: 45000, navigate: 45000, wait_ready: 30000, screenshot: 15000, fill: 60000, upload: 30000,
    scan: 20000, extract_text: 15000, click: 15000, click_text: 15000, click_point: 15000
  };
  var KIND_PRIORITY = ["captcha", "confirmation", "already_applied", "closed", "verification", "signup", "login", "form", "job_description", "error", "unknown"];
  var TAB_ACTIONS = { open: 1, navigate: 1, wait_ready: 1, tab_info: 1, focus: 1, close: 1, screenshot: 1, list_owned: 1 };
  var CONTENT_ACTIONS = { scan: 1, fill: 1, upload: 1, click: 1, click_text: 1, click_point: 1, highlight: 1, extract_text: 1 };

  function BridgeError(code, message, details) {
    var e = new Error(message);
    e.code = code;
    if (details !== undefined) e.details = details;
    return e;
  }
  function fnv1a(str) {
    var h = 0x811c9dc5;
    str = String(str);
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    return ("0000000" + h.toString(16)).slice(-8);
  }
  function delay(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }
  function withTimeout(promise, ms, what) {
    var timer;
    return Promise.race([
      promise,
      new Promise(function (_, reject) {
        timer = setTimeout(function () { reject(BridgeError("TIMEOUT", what + " timed out after " + ms + " ms")); }, ms);
      })
    ]).then(function (v) { clearTimeout(timer); return v; }, function (e) { clearTimeout(timer); throw e; });
  }
  function parseElementId(id) {
    var m = /^(\d+):(.+)$/.exec(String(id == null ? "" : id));
    if (!m) throw BridgeError("BAD_REQUEST", "element id must look like '<frameId>:<n>', got " + JSON.stringify(id));
    return { frameId: parseInt(m[1], 10), local: m[2] };
  }
  function pathOf(url) {
    var m = /^[a-z]+:\/\/[^/]+(\/[^?#]*)?/i.exec(String(url || ""));
    return m ? (m[1] || "/") : String(url || "");
  }

  function BridgeCore(opts) {
    opts = opts || {};
    if (!opts.driver) throw new Error("driver required");
    this.driver = opts.driver;
    this.storage = opts.storage || memoryStorage();
    this.log = opts.log || function () {};
    this.version = opts.version || VERSION;
    this.browserName = opts.browserName || "firefox";
    this.defaultTimeoutMs = opts.defaultTimeoutMs || 30000;
    this.frameTimeoutMs = opts.frameTimeoutMs || 8000;
    this.owned = new Map(); // app_id -> {app_id, tab_id, window_id, url, strategy}
    this.locks = new Map(); // app_id -> tail promise
    this.lastNav = new Map(); // app_id -> last url reported in tab_navigated
    this.listeners = [];
    this._initPromise = null;
    var self = this;
    if (typeof this.driver.onTabRemoved === "function") {
      this.driver.onTabRemoved(function (tabId) { self.handleTabRemoved(tabId); });
    }
    if (typeof this.driver.onTabNavigated === "function") {
      this.driver.onTabNavigated(function (tabId, url) { self.handleTabNavigated(tabId, url); });
    }
  }

  function memoryStorage() {
    var m = new Map();
    return {
      get: function (k) { return Promise.resolve(m.get(k)); },
      set: function (k, v) { m.set(k, v); return Promise.resolve(); }
    };
  }

  BridgeCore.prototype.init = function () {
    var self = this;
    if (this._initPromise) return this._initPromise;
    this._initPromise = (async function () {
      var saved;
      try { saved = await self.storage.get(STORAGE_KEY); } catch (e) { saved = null; }
      if (Array.isArray(saved)) {
        for (var i = 0; i < saved.length; i++) {
          var o = saved[i];
          if (!o || !o.app_id || o.tab_id == null) continue;
          var tab = null;
          try { tab = await self.driver.getTab(o.tab_id); } catch (e) { tab = null; }
          if (!tab) continue;
          if (self._appForTab(o.tab_id)) continue;
          self.owned.set(String(o.app_id), {
            app_id: String(o.app_id), tab_id: o.tab_id, window_id: tab.window_id != null ? tab.window_id : o.window_id,
            url: tab.url || o.url || "", strategy: o.strategy || "window"
          });
        }
      }
      await self._persist();
    })();
    return this._initPromise;
  };

  BridgeCore.prototype._persist = async function () {
    var list = [];
    this.owned.forEach(function (o) { list.push(Object.assign({}, o)); });
    try { await this.storage.set(STORAGE_KEY, list); } catch (e) { this.log("persist failed", e && e.message); }
  };

  BridgeCore.prototype.ownedList = function () {
    var out = [];
    this.owned.forEach(function (o) { out.push({ app_id: o.app_id, tab_id: o.tab_id, window_id: o.window_id, url: o.url }); });
    return out;
  };

  BridgeCore.prototype.helloMessage = function () {
    return { type: "hello", version: this.version, browser: this.browserName, owned: this.ownedList() };
  };

  BridgeCore.prototype.onEvent = function (cb) {
    this.listeners.push(cb);
  };
  BridgeCore.prototype._emit = function (event, appId, data) {
    var msg = { type: "event", event: event };
    if (appId != null) msg.app_id = appId;
    if (data !== undefined) msg.data = data;
    this.listeners.forEach(function (cb) {
      try { cb(msg); } catch (e) { /* ignore listener errors */ }
    });
  };

  BridgeCore.prototype._appForTab = function (tabId) {
    var found = null;
    this.owned.forEach(function (o) { if (o.tab_id === tabId) found = o.app_id; });
    return found;
  };

  BridgeCore.prototype.handleTabRemoved = function (tabId) {
    var appId = this._appForTab(tabId);
    if (!appId) return;
    this.owned.delete(appId);
    this.lastNav.delete(appId);
    this._persist();
    this._emit("tab_closed", appId, { tab_id: tabId });
  };

  BridgeCore.prototype.handleTabNavigated = function (tabId, url) {
    var appId = this._appForTab(tabId);
    if (!appId) return;
    var o = this.owned.get(appId);
    if (this.lastNav.get(appId) === url) return;
    this.lastNav.set(appId, url);
    if (o.url !== url) {
      o.url = url;
      this._persist();
    }
    this._emit("tab_navigated", appId, { url: url, tab_id: tabId, window_id: o.window_id });
  };

  // Per-app mutex: run fn after all previously queued work of the same app.
  BridgeCore.prototype._withLock = function (appId, fn, holdMs) {
    var self = this;
    var prev = this.locks.get(appId) || Promise.resolve();
    var release;
    var gate = new Promise(function (r) { release = r; });
    var tail = prev.then(function () { return gate; });
    this.locks.set(appId, tail);
    var run = prev.then(function () {
      // bounded: even if fn never settles the lock is released after holdMs
      var guard = setTimeout(release, holdMs);
      var p;
      try { p = Promise.resolve(fn()); } catch (e) { p = Promise.reject(e); }
      p.then(function () { clearTimeout(guard); release(); }, function () { clearTimeout(guard); release(); });
      return p;
    });
    tail.then(function () {
      if (self.locks.get(appId) === tail) self.locks.delete(appId);
    });
    return run;
  };

  BridgeCore.prototype.handleMessage = async function (msg) {
    if (!msg || typeof msg !== "object") return null;
    if (msg.type === "ping") return { type: "pong", ts: msg.ts != null ? msg.ts : Date.now() };
    if (msg.type !== "command") return null;
    await this.init();
    var id = msg.id != null ? String(msg.id) : null;
    var self = this;
    var action = msg.action;
    var payload = msg.payload && typeof msg.payload === "object" ? msg.payload : {};
    var timeoutMs = Number(msg.timeout_ms) > 0 ? Number(msg.timeout_ms) : (DEFAULT_TIMEOUTS[action] || this.defaultTimeoutMs);
    try {
      if (!id) throw BridgeError("BAD_REQUEST", "command id required");
      if (!action || !(TAB_ACTIONS[action] || CONTENT_ACTIONS[action])) throw BridgeError("UNSUPPORTED", "unknown action " + JSON.stringify(action));
      if (action === "list_owned") return ok(id, { owned: this.ownedList() });
      if (msg.app_id == null || msg.app_id === "") throw BridgeError("BAD_REQUEST", "app_id required");
      var appId = String(msg.app_id);
      var deadline = Date.now() + timeoutMs;
      var result = await this._withLock(appId, function () {
        return withTimeout(self._dispatch(appId, action, payload, timeoutMs, deadline), timeoutMs, action);
      }, timeoutMs + 5000);
      return ok(id, result);
    } catch (e) {
      var code = e && e.code ? e.code : "INTERNAL";
      if (code === "INTERNAL") this.log("command failed", action, e && e.stack || e);
      var err = { code: code, message: String(e && e.message || e) };
      if (e && e.details !== undefined) err.details = e.details;
      return { type: "result", id: id, ok: false, error: err };
    }
  };
  function ok(id, result) {
    return { type: "result", id: id, ok: true, result: result };
  }

  BridgeCore.prototype._ownedTab = async function (appId) {
    var o = this.owned.get(appId);
    if (!o) throw BridgeError("NO_OWNED_TAB", "application " + appId + " has no owned tab");
    var tab = null;
    try { tab = await this.driver.getTab(o.tab_id); } catch (e) { tab = null; }
    if (!tab) {
      this.owned.delete(appId);
      await this._persist();
      this._emit("tab_closed", appId, { tab_id: o.tab_id });
      throw BridgeError("TAB_CLOSED", "the owned tab of " + appId + " was closed");
    }
    if (tab.window_id != null) o.window_id = tab.window_id;
    if (tab.url) o.url = tab.url;
    return { own: o, tab: tab };
  };

  BridgeCore.prototype._dispatch = async function (appId, action, p, timeoutMs, deadline) {
    var remaining = function () { return Math.max(500, deadline - Date.now()); };
    switch (action) {
      case "open": return this._open(appId, p, remaining);
      case "navigate": {
        if (!p.url || !/^(https?|file|about):/i.test(String(p.url))) throw BridgeError("BAD_REQUEST", "valid url required");
        var t = await this._ownedTab(appId);
        await this.driver.navigate(t.own.tab_id, String(p.url));
        await this.driver.waitForLoad(t.own.tab_id, remaining() - 300);
        var nt = await this._ownedTab(appId);
        await this._persist();
        return { url: nt.tab.url, title: nt.tab.title || "" };
      }
      case "wait_ready": {
        var t2 = await this._ownedTab(appId);
        var budget = Math.min(Number(p.timeout_ms) > 0 ? Number(p.timeout_ms) : 20000, remaining() - 300);
        var started = Date.now();
        var load = await this.driver.waitForLoad(t2.own.tab_id, budget);
        var ready = !!(load && load.complete);
        if (ready) {
          var left = Math.max(200, budget - (Date.now() - started));
          var quiet = await this._sendFrame(t2.own.tab_id, 0, "wait_quiet", { quiet_ms: Number(p.quiet_ms) > 0 ? Number(p.quiet_ms) : 600, timeout_ms: left }, left + 1000).catch(function () { return null; });
          ready = !!(quiet && quiet.quiet);
        }
        var t3 = await this._ownedTab(appId);
        return { url: t3.tab.url, title: t3.tab.title || "", ready: ready };
      }
      case "tab_info": {
        var ti = await this._ownedTab(appId);
        return { tab_id: ti.own.tab_id, window_id: ti.own.window_id, url: ti.tab.url, title: ti.tab.title || "", status: ti.tab.status || null };
      }
      case "focus": {
        var tf = await this._ownedTab(appId);
        var focused = await this.driver.focus(tf.own.tab_id, tf.own.window_id);
        var verified = await this._ownedTab(appId);
        return Object.assign({ ok: true, tab_id: verified.own.tab_id, window_id: verified.own.window_id,
          url: verified.tab.url, title: verified.tab.title || "" }, focused || {});
      }
      case "close": {
        var tc = await this._ownedTab(appId);
        this.owned.delete(appId); // release first so the removal is not reported as tab_closed
        await this._persist();
        try { await this.driver.close(tc.own.tab_id, tc.own.window_id); } catch (e) { /* already gone */ }
        return { ok: true };
      }
      case "screenshot": {
        var ts = await this._ownedTab(appId);
        var shot = await this.driver.screenshot(ts.own.tab_id, ts.own.window_id, { format: p.format === "png" ? "png" : "jpeg", quality: Number(p.quality) > 0 ? Number(p.quality) : 70 });
        return shot;
      }
      case "scan": return this._scan(appId, p, remaining);
      case "fill": return this._fill(appId, p, remaining);
      case "upload": {
        if (!p.file || !p.file.name || typeof p.file.base64 !== "string") throw BridgeError("BAD_REQUEST", "file {name, mime, base64} required");
        var eid = parseElementId(p.field_id);
        var tu = await this._ownedTab(appId);
        return this._sendFrame(tu.own.tab_id, eid.frameId, "upload", { field_id: eid.local, file: p.file }, remaining());
      }
      case "click": {
        var cid = parseElementId(p.element_id);
        var tk = await this._ownedTab(appId);
        return this._sendFrame(tk.own.tab_id, cid.frameId, "click", { element_id: cid.local }, remaining());
      }
      case "click_text": return this._clickText(appId, p, remaining);
      case "click_point": {
        if (!isFinite(Number(p.x)) || !isFinite(Number(p.y))) throw BridgeError("BAD_REQUEST", "x and y required");
        var tp = await this._ownedTab(appId);
        return this._sendFrame(tp.own.tab_id, 0, "click_point", { x: Number(p.x), y: Number(p.y) }, remaining());
      }
      case "highlight": {
        var hid = parseElementId(p.element_id);
        var th = await this._ownedTab(appId);
        return this._sendFrame(th.own.tab_id, hid.frameId, "highlight", { element_id: hid.local }, remaining());
      }
      case "extract_text": return this._extractText(appId, p, remaining);
      default:
        throw BridgeError("UNSUPPORTED", "unknown action " + action);
    }
  };

  BridgeCore.prototype._open = async function (appId, p, remaining) {
    var url = String(p.url || "");
    if (!/^(https?|file|about):/i.test(url)) throw BridgeError("BAD_REQUEST", "valid url required");
    var strategy = p.strategy === "tab" ? "tab" : "window";
    var existing = this.owned.get(appId);
    var tabId = null;
    if (existing) {
      var tab = null;
      try { tab = await this.driver.getTab(existing.tab_id); } catch (e) { tab = null; }
      if (tab) {
        tabId = existing.tab_id;
        await this.driver.navigate(tabId, url);
        if (p.active !== false) await this.driver.focus(tabId, existing.window_id);
      } else {
        this.owned.delete(appId);
      }
    }
    if (tabId == null) {
      var created;
      try {
        created = await this.driver.openTab(url, { strategy: strategy, active: p.active !== false });
      } catch (e) {
        throw BridgeError(e && e.code || "NAVIGATION_FAILED", "could not open tab: " + (e && e.message || e));
      }
      if (!created || created.tab_id == null) throw BridgeError("INTERNAL", "driver returned no tab");
      var other = this._appForTab(created.tab_id);
      if (other && other !== appId) throw BridgeError("INTERNAL", "driver returned a tab owned by another application");
      tabId = created.tab_id;
      this.owned.set(appId, { app_id: appId, tab_id: tabId, window_id: created.window_id, url: url, strategy: strategy });
      await this._persist();
    }
    var load = await this.driver.waitForLoad(tabId, remaining() - 300);
    var t = await this._ownedTab(appId);
    t.own.url = t.tab.url || url;
    await this._persist();
    return { tab_id: tabId, window_id: t.own.window_id, url: t.tab.url, title: t.tab.title || "", loaded: !!(load && load.complete) };
  };

  // Sends a content action to one frame; returns result or throws a BridgeError.
  BridgeCore.prototype._sendFrame = async function (tabId, frameId, action, payload, timeoutMs) {
    var reply;
    try {
      reply = await withTimeout(Promise.resolve(this.driver.sendToFrame(tabId, frameId, { __applypilot: 1, action: action, payload: payload })), Math.max(300, timeoutMs), action + " in frame " + frameId);
    } catch (e) {
      if (e && e.code === "TIMEOUT") throw e;
      if (e && e.code && e.code !== "INTERNAL") throw e;
      throw BridgeError("CONTENT_SCRIPT_UNAVAILABLE", "frame " + frameId + " did not answer: " + (e && e.message || e));
    }
    if (!reply || typeof reply !== "object") throw BridgeError("CONTENT_SCRIPT_UNAVAILABLE", "no content script answered in frame " + frameId);
    if (reply.ok) return reply.result;
    var err = reply.error || {};
    throw BridgeError(err.code || "INTERNAL", err.message || "content error", err.details);
  };

  BridgeCore.prototype._frames = async function (tabId) {
    var frames = [];
    try { frames = (await this.driver.getFrames(tabId)) || []; } catch (e) { frames = []; }
    frames = frames.filter(function (f) { return f && f.frameId != null && !CAPTCHA_FRAME_RE.test(f.url || ""); });
    if (!frames.some(function (f) { return f.frameId === 0; })) frames.unshift({ frameId: 0, url: "" });
    frames.sort(function (a, b) { return a.frameId - b.frameId; });
    return frames;
  };

  // Sends to every frame in parallel; returns [{frameId, result} | {frameId, error}] (top first).
  BridgeCore.prototype._broadcast = async function (tabId, action, payload, remaining) {
    var self = this;
    var frames = await this._frames(tabId);
    var perFrame = Math.min(this.frameTimeoutMs, remaining() - 200);
    return Promise.all(frames.map(function (f) {
      var t = f.frameId === 0 ? remaining() - 200 : perFrame;
      return self._sendFrame(tabId, f.frameId, action, payload, t).then(
        function (r) { return { frameId: f.frameId, url: f.url, result: r }; },
        function (e) { return { frameId: f.frameId, url: f.url, error: e }; }
      );
    }));
  };

  BridgeCore.prototype._scan = async function (appId, p, remaining) {
    var t = await this._ownedTab(appId);
    var payload = { include_text: !!p.include_text, max_text: Number(p.max_text) > 0 ? Number(p.max_text) : 4000 };
    var answers = await this._broadcast(t.own.tab_id, "scan", payload, remaining);
    var top = answers.filter(function (a) { return a.frameId === 0; })[0];
    if (!top || top.error) {
      var e = top && top.error;
      if (e && e.code && e.code !== "INTERNAL") throw e;
      throw BridgeError("CONTENT_SCRIPT_UNAVAILABLE", "top frame did not answer scan: " + (e && e.message || "no answer"));
    }
    var snaps = answers.filter(function (a) { return a.result; }).map(function (a) { return { frameId: a.frameId, s: a.result }; });
    var merged = mergeSnapshots(snaps, payload, t.tab);
    return merged;
  };

  function prefix(frameId, id) {
    return id == null ? null : frameId + ":" + id;
  }

  function mergeSnapshots(snaps, payload, tab) {
    var top = snaps[0].s;
    var fields = [], buttons = [], errors = [], texts = [], hashes = [];
    var kinds = [];
    var ats = "generic", step = null, captcha = null, confirmation = { detected: false, text: null };
    var mostFields = null;
    var atsData = null;
    snaps.forEach(function (x) {
      var f = x.frameId, s = x.s;
      (s.fields || []).forEach(function (fl) {
        var c = Object.assign({}, fl);
        c.id = prefix(f, fl.id);
        c.frame = f;
        fields.push(c);
      });
      (s.buttons || []).forEach(function (b) {
        var c = Object.assign({}, b);
        c.id = prefix(f, b.id);
        c.frame = f;
        buttons.push(c);
      });
      (s.errors || []).forEach(function (e) {
        errors.push({ text: e.text, field_id: e.field_id != null ? prefix(f, e.field_id) : null });
      });
      if (s.page_kind) kinds.push({ kind: s.page_kind, frameId: f, n: (s.fields || []).length });
      if (ats === "generic" && s.ats && s.ats !== "generic") ats = s.ats;
      if (s.captcha) {
        if (!captcha || (!captcha.interactive && s.captcha.interactive) || (!captcha.present && s.captcha.present)) captcha = s.captcha;
      }
      if (s.confirmation && s.confirmation.detected && !confirmation.detected) confirmation = s.confirmation;
      if (!mostFields || (s.fields || []).length > (mostFields.fields || []).length) mostFields = s;
      if (s.text) texts.push(s.text);
      if (s.ats_data && !atsData) atsData = s.ats_data;
      hashes.push(s.signals && s.signals.text_hash || "");
    });
    var kind = "unknown";
    for (var i = 0; i < KIND_PRIORITY.length; i++) {
      if (kinds.some(function (k) { return k.kind === KIND_PRIORITY[i]; })) { kind = KIND_PRIORITY[i]; break; }
    }
    step = (mostFields && mostFields.step) || null;
    if (!step) snaps.forEach(function (x) { if (!step && x.s.step) step = x.s.step; });
    var heading = top.heading || "";
    if ((!heading || (top.fields || []).length === 0) && mostFields && (mostFields.fields || []).length && mostFields.heading) heading = mostFields.heading;
    var url = (tab && tab.url) || top.url;
    var out = {
      url: url,
      title: (tab && tab.title) || top.title || "",
      frames: snaps.length,
      ats: ats,
      page_kind: kind,
      heading: heading,
      step: step,
      fields: fields,
      buttons: buttons,
      errors: errors,
      captcha: captcha || { present: false, kind: null, interactive: false, detail: null },
      confirmation: confirmation,
      signals: {
        has_password: fields.some(function (f) { return f.type === "password"; }),
        has_submit: buttons.some(function (b) { return b.kind === "submit"; }),
        text_hash: hashes.length === 1 ? hashes[0] : fnv1a(hashes.join("|"))
      },
      fingerprint: fnv1a([pathOf(url), heading, fields.map(function (f) { return f.key; }).join(",")].join("|"))
    };
    if (atsData) out.ats_data = atsData;
    if (payload.include_text) out.text = texts.join("\n\n").slice(0, payload.max_text);
    return out;
  }

  BridgeCore.prototype._fill = async function (appId, p, remaining) {
    if (!Array.isArray(p.items)) throw BridgeError("BAD_REQUEST", "items array required");
    var t = await this._ownedTab(appId);
    var results = new Array(p.items.length);
    var groups = new Map(); // frameId -> [{idx, item}]
    var order = [];
    p.items.forEach(function (item, idx) {
      var fid = item && item.field_id;
      var parsed;
      try { parsed = parseElementId(fid); } catch (e) {
        results[idx] = { field_id: fid == null ? null : String(fid), ok: false, error: { code: "BAD_REQUEST", message: e.message } };
        return;
      }
      if (!groups.has(parsed.frameId)) { groups.set(parsed.frameId, []); order.push(parsed.frameId); }
      groups.get(parsed.frameId).push({ idx: idx, local: parsed.local, value: item.value });
    });
    for (var g = 0; g < order.length; g++) {
      var frameId = order[g];
      var list = groups.get(frameId);
      var reply = null, err = null;
      try {
        reply = await this._sendFrame(t.own.tab_id, frameId, "fill", { items: list.map(function (x) { return { field_id: x.local, value: x.value }; }) }, remaining());
      } catch (e) { err = e; }
      list.forEach(function (x, k) {
        var r = reply && reply.results && reply.results[k];
        if (r) {
          var c = Object.assign({}, r);
          c.field_id = prefix(frameId, x.local);
          results[x.idx] = c;
        } else {
          results[x.idx] = { field_id: prefix(frameId, x.local), ok: false, error: { code: err && err.code || "CONTENT_SCRIPT_UNAVAILABLE", message: err ? err.message : "no result" } };
        }
      });
      if (err && err.code === "TIMEOUT") {
        // mark the remaining groups as timed out too
        for (var r = g + 1; r < order.length; r++) {
          groups.get(order[r]).forEach(function (x) {
            results[x.idx] = { field_id: prefix(order[r], x.local), ok: false, error: { code: "TIMEOUT", message: "fill timed out" } };
          });
        }
        break;
      }
    }
    return { results: results };
  };

  BridgeCore.prototype._clickText = async function (appId, p, remaining) {
    if (!p.text) throw BridgeError("BAD_REQUEST", "text required");
    var t = await this._ownedTab(appId);
    var frames = await this._frames(t.own.tab_id);
    var passes = p.exact ? [true] : [true, false];
    var lastErr = null;
    for (var i = 0; i < passes.length; i++) {
      for (var j = 0; j < frames.length; j++) {
        var f = frames[j];
        try {
          var r = await this._sendFrame(t.own.tab_id, f.frameId, "click_text", { text: String(p.text), exact: passes[i] }, Math.min(remaining(), f.frameId === 0 ? remaining() : this.frameTimeoutMs));
          var out = Object.assign({}, r);
          out.element_id = prefix(f.frameId, r.element_id);
          return out;
        } catch (e) {
          if (e.code === "ELEMENT_NOT_FOUND" || e.code === "CONTENT_SCRIPT_UNAVAILABLE" || e.code === "UNSUPPORTED") { lastErr = lastErr && lastErr.code === "ELEMENT_NOT_FOUND" ? lastErr : e; continue; }
          throw e;
        }
      }
    }
    throw BridgeError("ELEMENT_NOT_FOUND", "no visible clickable element matches " + JSON.stringify(p.text) + (lastErr && lastErr.code !== "ELEMENT_NOT_FOUND" ? " (" + lastErr.message + ")" : ""));
  };

  BridgeCore.prototype._extractText = async function (appId, p, remaining) {
    var t = await this._ownedTab(appId);
    var max = Number(p.max_chars) > 0 ? Number(p.max_chars) : 4000;
    var answers = await this._broadcast(t.own.tab_id, "extract_text", { max_chars: max }, remaining);
    var top = answers.filter(function (a) { return a.frameId === 0; })[0];
    if (!top || top.error) throw BridgeError("CONTENT_SCRIPT_UNAVAILABLE", "top frame did not answer extract_text");
    var parts = answers.filter(function (a) { return a.result && a.result.text; }).map(function (a) { return a.result.text; });
    return { text: parts.join("\n\n").slice(0, max), url: t.tab.url || top.result.url, title: t.tab.title || top.result.title || "" };
  };

  return {
    BridgeCore: BridgeCore,
    BridgeError: BridgeError,
    mergeSnapshots: mergeSnapshots,
    parseElementId: parseElementId,
    KIND_PRIORITY: KIND_PRIORITY,
    STORAGE_KEY: STORAGE_KEY,
    VERSION: VERSION,
    fnv1a: fnv1a,
    delay: delay
  };
});
