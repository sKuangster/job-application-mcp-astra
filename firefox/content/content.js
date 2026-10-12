/*
 * ApplyPilot content script: wires the DOM engine into the page.
 *
 * Message format (background -> content, via browser.tabs.sendMessage with frameId):
 *   {__applypilot: 1, action, payload}
 * Reply (Promise): {ok: true, result} | {ok: false, error: {code, message, details?}}
 *
 * Actions: ping, scan, fill, upload, click, click_text, click_point, highlight, extract_text, wait_quiet.
 * Ids in payloads/results are LOCAL to this frame (the background adds "<frameId>:").
 */
(function () {
  "use strict";
  if (window.__applypilotContentLoaded) return;
  window.__applypilotContentLoaded = true;

  var api = typeof browser !== "undefined" ? browser : null; // eslint-disable-line no-undef
  if (!api || !api.runtime || !api.runtime.onMessage) return;

  var debug = false;
  var engine = ApplyPilotDom.createEngine(document, ApplyPilotAdapters, { debug: debug }); // eslint-disable-line no-undef

  function handle(action, p) {
    switch (action) {
      case "ping":
        return Promise.resolve({ pong: true, url: String(location.href), ready_state: document.readyState });
      case "scan":
        return Promise.resolve(engine.scan({ include_text: !!p.include_text, max_text: p.max_text || 4000 }));
      case "fill":
        return engine.fill(p.items || []);
      case "upload":
        return engine.upload(String(p.field_id), p.file);
      case "click":
        return engine.click(String(p.element_id));
      case "click_text":
        return engine.clickText(p.text, !!p.exact);
      case "click_point":
        return engine.clickPoint(p.x, p.y);
      case "highlight":
        return Promise.resolve(engine.highlight(String(p.element_id)));
      case "extract_text":
        return Promise.resolve(engine.extractText(p.max_chars || 4000));
      case "wait_quiet":
        return engine.waitQuiet(p.quiet_ms || 600, p.timeout_ms || 10000);
      default: {
        var e = new Error("unknown content action " + action);
        e.code = "UNSUPPORTED";
        return Promise.reject(e);
      }
    }
  }

  api.runtime.onMessage.addListener(function (msg) {
    if (!msg || msg.__applypilot !== 1) return undefined;
    var p;
    try {
      p = handle(msg.action, msg.payload || {});
    } catch (e) {
      p = Promise.reject(e);
    }
    return p.then(function (result) {
      return { ok: true, result: result };
    }, function (err) {
      var error = { code: (err && err.code) || "INTERNAL", message: String((err && err.message) || err) };
      if (err && err.details) error.details = err.details;
      return { ok: false, error: error };
    });
  });
})();
