/*
 * ApplyPilot DOM engine.
 *
 * UMD module: content-script global `ApplyPilotDom`, or `module.exports` in Node.
 *
 *   const engine = ApplyPilotDom.createEngine(document, ApplyPilotAdapters, {debug})
 *   engine.scan({include_text, max_text})        -> per-frame PageSnapshot (PROTOCOL §3), LOCAL ids
 *   await engine.fill([{field_id, value}])       -> {results: [{field_id, ok, actual?, error?, options?}]}
 *   await engine.upload(fieldId, {name, mime, base64}) -> {ok, files}
 *   await engine.click(id)                       -> {ok, href?}
 *   await engine.clickText(text, exact)          -> {ok, element_id}
 *   await engine.clickPoint(x, y)                -> {ok, tag, text}
 *   engine.extractText(max)                      -> {text, url, title}
 *   await engine.waitQuiet(quietMs, timeoutMs)   -> {quiet, waited_ms, ready_state}
 *
 * Errors are thrown as Error objects with a `code` property (PROTOCOL §2 codes).
 * Ids are LOCAL (the per-document counter stored as data-ap-id); the background
 * prefixes them with the frame id.
 *
 * Everything here must only use constructors of the page window
 * (document.defaultView) so it behaves the same in Firefox content scripts and
 * in jsdom under Node.
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module && module.exports) module.exports = api;
  else root.ApplyPilotDom = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---------------------------------------------------------------- utilities
  function clean(s) {
    return String(s == null ? "" : s).replace(/[\s\u00a0\u200b]+/g, " ").trim();
  }
  function norm(s) {
    return clean(s).toLowerCase();
  }
  // Normalisation used for option matching: case/space-insensitive, ignores a
  // trailing required marker and surrounding punctuation.
  function loose(s) {
    return norm(s).replace(/[*✱]+/g, "").replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, " ").replace(/^[\s.,:;!?-]+|[\s.,:;!?-]+$/g, "");
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
  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }
  function fail(code, message, extra) {
    var e = new Error(message);
    e.code = code;
    if (extra) Object.keys(extra).forEach(function (k) { e[k] = extra[k]; });
    return e;
  }
  function stripRequired(label) {
    var s = clean(label);
    for (var i = 0; i < 3; i++) {
      var before = s;
      s = s.replace(/\s*\((required|optional)\)\s*$/i, "")
        .replace(/\s*[*✱]+\s*$/, "")
        .replace(/^\s*[*✱]+\s*/, "")
        .trim();
      if (s === before) break;
    }
    return s;
  }
  function looksRequired(raw) {
    return /[*✱]|\(required\)/i.test(raw || "");
  }

  var CAPTCHA_URL_RE = /recaptcha\/(api2|enterprise)\/(anchor|bframe)|recaptcha\/api\/fallback|hcaptcha\.com\/(captcha|.*#frame=)|newassets\.hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs\.com|funcaptcha\.com/i;
  var CAPTCHA_FRAME_SELF_RE = /\/recaptcha\/(api2|enterprise)\/|hcaptcha\.com|challenges\.cloudflare\.com|arkoselabs\.com|funcaptcha\.com/i;

  var CONTROL_SELECTOR = "input, select, textarea, [role=combobox], [role=listbox], [role=radio], [role=checkbox], [role=switch], [contenteditable=''], [contenteditable=true], button[aria-haspopup=listbox], [role=button][aria-haspopup=listbox]";
  var SKIP_INPUT_TYPES = { hidden: 1, submit: 1, button: 1, reset: 1, image: 1 };

  // ------------------------------------------------------------------ engine
  function createEngine(document, adapterRegistry, options) {
    options = options || {};
    var win = document.defaultView;
    var registry = adapterRegistry || (typeof ApplyPilotAdapters !== "undefined" ? ApplyPilotAdapters : null); // eslint-disable-line no-undef
    var isJsdom = /jsdom/i.test((win && win.navigator && win.navigator.userAgent) || "");
    var debug = !!options.debug;
    var lastFields = new Map(); // local id -> descriptor (from the last scan)

    function log() {
      if (!debug) return;
      try { win.console.debug.apply(win.console, ["[ApplyPilot]"].concat([].slice.call(arguments))); } catch (e) { /* ignore */ }
    }

    // ------------------------------------------------------------ ids
    function nextCounter() {
      var de = document.documentElement;
      var n = parseInt((de && de.getAttribute("data-ap-next")) || "1", 10);
      if (!(n > 0)) n = 1;
      if (de) de.setAttribute("data-ap-next", String(n + 1));
      return n;
    }
    function idOf(el) {
      var id = el.getAttribute("data-ap-id");
      if (!id) {
        id = String(nextCounter());
        el.setAttribute("data-ap-id", id);
      }
      return id;
    }
    function findById(id) {
      var rs = roots();
      var sel = '[data-ap-id="' + String(id).replace(/["\\]/g, "") + '"]';
      for (var i = 0; i < rs.length; i++) {
        var el = rs[i].querySelector(sel);
        if (el) return el;
      }
      return null;
    }

    // ------------------------------------------------------------ DOM traversal
    function shadowOf(el) {
      try {
        if (el.openOrClosedShadowRoot) return el.openOrClosedShadowRoot; // Firefox content scripts
      } catch (e) { /* ignore */ }
      return el.shadowRoot || null;
    }
    function roots() {
      var out = [document];
      for (var i = 0; i < out.length && out.length < 500; i++) {
        var all = out[i].querySelectorAll("*");
        for (var j = 0; j < all.length; j++) {
          var sr = shadowOf(all[j]);
          if (sr) out.push(sr);
        }
      }
      return out;
    }
    function queryAll(selector, rs) {
      rs = rs || roots();
      var out = [];
      for (var i = 0; i < rs.length; i++) {
        var found = rs[i].querySelectorAll(selector);
        for (var j = 0; j < found.length; j++) out.push(found[j]);
      }
      return out;
    }
    function parentOf(node) {
      if (!node) return null;
      if (node.parentElement) return node.parentElement;
      var r = node.getRootNode ? node.getRootNode() : null;
      return r && r.host ? r.host : null;
    }
    function topLevel(el) {
      var n = el;
      while (n) {
        var r = n.getRootNode ? n.getRootNode() : null;
        if (r && r.host) n = r.host; else break;
      }
      return n;
    }
    function rootNodeOf(el) {
      return (el.getRootNode && el.getRootNode()) || document;
    }
    function byIdIn(el, id) {
      if (!id) return null;
      var r = rootNodeOf(el);
      var found = r.getElementById ? r.getElementById(id) : null;
      if (!found && r !== document) found = document.getElementById(id);
      return found;
    }
    function tag(el) {
      return (el && el.tagName ? el.tagName : "").toUpperCase();
    }
    function attr(el, name) {
      return (el && el.getAttribute && el.getAttribute(name)) || "";
    }
    function classOf(el) {
      return attr(el, "class");
    }

    // ------------------------------------------------------------ visibility
    var styleCache = null; // Map during a scan
    function style(el) {
      if (styleCache && styleCache.has(el)) return styleCache.get(el);
      var s = null;
      try { s = win.getComputedStyle(el); } catch (e) { s = null; }
      if (styleCache) styleCache.set(el, s);
      return s;
    }
    var hiddenCache = null;
    function selfHidden(node) {
      if (hiddenCache && hiddenCache.has(node)) return hiddenCache.get(node);
      var hidden = false;
      if (node.hidden && tag(node) !== "DETAILS") hidden = true;
      else if (attr(node, "aria-hidden") === "true") hidden = true;
      else if (node.hasAttribute && node.hasAttribute("inert")) hidden = true;
      else {
        var s = style(node);
        if (s && (s.display === "none" || s.visibility === "hidden" || s.visibility === "collapse")) hidden = true;
        if (!hidden && s && isJsdom && (s.position === "absolute" || s.position === "fixed")) {
          var left = parseFloat(s.left), top = parseFloat(s.top);
          if (left <= -999 || top <= -999) hidden = true;
        }
      }
      if (hiddenCache) hiddenCache.set(node, hidden);
      return hidden;
    }
    function offscreen(el) {
      if (isJsdom || !el.getBoundingClientRect) return false;
      var r = el.getBoundingClientRect();
      var sx = win.scrollX || 0, sy = win.scrollY || 0;
      return r.right + sx < -50 || r.bottom + sy < -50;
    }
    function visible(el, opts) {
      if (!el || el.nodeType !== 1) return false;
      opts = opts || {};
      if (tag(el) === "INPUT" && String(el.type).toLowerCase() === "hidden") return false;
      for (var n = el; n; n = parentOf(n)) {
        if (n.nodeType !== 1) continue;
        if (selfHidden(n)) {
          // keep visually hidden file inputs (but never display:none parents of the dropzone)
          if (opts.file && n === el && !(n.hidden) && attr(n, "aria-hidden") !== "true") continue;
          return false;
        }
        if (tag(n) === "BODY" || tag(n) === "HTML") break;
      }
      if (!isJsdom && !opts.file) {
        try {
          if (el.getClientRects && el.getClientRects().length === 0) return false;
        } catch (e) { /* ignore */ }
        if (offscreen(el)) return false;
      }
      return true;
    }

    // ------------------------------------------------------------ text helpers
    var SKIP_TEXT_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, SELECT: 1, OPTION: 1, TEXTAREA: 1, INPUT: 1, SVG: 1 };
    // Text of an element, excluding form controls, scripts and (optionally) given elements.
    function textOf(el, exclude, opts) {
      if (!el) return "";
      opts = opts || {};
      var parts = [];
      var budget = opts.max || 600;
      (function walk(node) {
        if (budget <= 0) return;
        if (node.nodeType === 3) {
          parts.push(node.nodeValue);
          budget -= node.nodeValue.length;
          return;
        }
        if (node.nodeType !== 1 && node.nodeType !== 11) return;
        if (node.nodeType === 1) {
          if (SKIP_TEXT_TAGS[tag(node)]) return;
          if (exclude && exclude.indexOf(node) >= 0) return;
          if (node.hidden) return;
          if (opts.visibleOnly && node !== el && selfHidden(node)) return;
          if (opts.skipErrors && node !== el && isErrorish(node)) return;
          if (opts.skipControls && node !== el && node.matches && node.matches("[role=listbox], [role=option], [role=combobox], button")) return;
        }
        var kids = node.childNodes;
        for (var i = 0; i < kids.length; i++) walk(kids[i]);
        if (node.nodeType === 1 && /^(DIV|P|LI|TR|H[1-6]|BR|LABEL|SECTION|DT|DD)$/.test(tag(node))) parts.push(" ");
      })(el);
      return clean(parts.join(" "));
    }
    function isErrorish(node) {
      var c = classOf(node);
      return /(^|[\s_-])(error|errors|invalid|field-error)([\s_-]|$)|error-?message|errorMessage|_error_/i.test(c) || attr(node, "data-automation-id") === "errorMessage";
    }
    function elementText(el) {
      var t = textOf(el, null, { max: 300 });
      if (!t) t = attr(el, "value") || "";
      if (!t) t = attr(el, "aria-label") || attr(el, "title") || "";
      if (!t) {
        var img = el.querySelector && el.querySelector("img[alt]");
        if (img) t = img.getAttribute("alt");
      }
      return clean(t).slice(0, 200);
    }

    // ------------------------------------------------------------ adapters
    function activeAdapters() {
      if (!registry) return [];
      try {
        return registry.detect ? registry.detect(win.location, document) : [];
      } catch (e) {
        log("adapter detect failed", e);
        return [];
      }
    }
    function callAdapters(list, hook, args, firstOnly) {
      var out = [];
      for (var i = 0; i < list.length; i++) {
        var fn = list[i][hook];
        if (typeof fn !== "function") continue;
        try {
          var r = fn.apply(list[i], args);
          if (r != null && r !== "" && !(Array.isArray(r) && r.length === 0)) {
            if (firstOnly) return r;
            out.push(r);
          }
        } catch (e) {
          log("adapter hook failed", list[i].name, hook, e);
        }
      }
      return firstOnly ? null : out;
    }

    // ------------------------------------------------------------ labels
    function labelledByText(el) {
      var ids = attr(el, "aria-labelledby").split(/\s+/).filter(Boolean);
      if (!ids.length) return "";
      return clean(ids.map(function (id) {
        var t = byIdIn(el, id);
        return t && t !== el ? textOf(t, null, { max: 400 }) : "";
      }).join(" "));
    }
    function nativeLabels(el) {
      var labels = [];
      try {
        if (el.labels) labels = [].slice.call(el.labels);
      } catch (e) { labels = []; }
      if (!labels.length && el.id) {
        var r = rootNodeOf(el);
        try {
          var esc = el.id.replace(/["\\]/g, "\\$&");
          labels = [].slice.call(r.querySelectorAll('label[for="' + esc + '"]'));
        } catch (e) { /* ignore */ }
      }
      if (!labels.length) {
        var wrap = el.closest ? el.closest("label") : null;
        if (wrap) labels = [wrap];
      }
      return labels;
    }
    function nativeLabelText(el) {
      var labels = nativeLabels(el);
      return clean(labels.map(function (l) { return textOf(l, [el], { max: 400, skipErrors: true }); }).join(" "));
    }
    function countControls(node, members) {
      var found = node.querySelectorAll(CONTROL_SELECTOR);
      var n = 0;
      for (var i = 0; i < found.length; i++) {
        var c = found[i];
        if (members && members.indexOf(c) >= 0) continue;
        if (tag(c) === "INPUT" && SKIP_INPUT_TYPES[String(c.type).toLowerCase()]) continue;
        if (attr(c, "aria-hidden") === "true" || c.hidden) continue;
        if (c.closest && c.closest("[role=listbox]") && tag(c) !== "SELECT" && attr(c, "role") === "option") continue;
        if (members && members.some(function (m) { return m.contains(c) || c.contains(m); })) continue;
        n++;
      }
      return n;
    }
    var LABELISH = "label, legend, [class*=label], [class*=Label], [class*=question], [class*=Question], [class*=title], [class*=Title], [class*=heading], h1, h2, h3, h4, h5, h6, [role=heading], [id$=label], [id$=Label]";
    // Question text for a widget or group without a native label: climb ancestors
    // while they contain no other controls; take label-like text preceding the
    // first member.
    function questionText(members, exclude) {
      var first = members[0];
      var node = commonAncestor(members);
      for (var depth = 0; node && depth < 7; depth++) {
        if (tag(node) === "FORM" || tag(node) === "BODY" || tag(node) === "HTML") break;
        if (depth > 0 && countControls(node, members) > 0) break;
        var cands = [].slice.call(node.querySelectorAll(LABELISH)).filter(function (c) {
          if (members.some(function (m) { return c.contains(m) && c !== m; })) return false;
          if (exclude && exclude.some(function (x) { return x === c || x.contains(c); })) return false;
          if (members.some(function (m) { return m === c || m.contains(c); })) return false;
          if (isErrorish(c)) return false;
          return precedes(c, first);
        });
        for (var i = 0; i < cands.length; i++) {
          var t = textOf(cands[i], (exclude || []).concat(members), { max: 400, skipErrors: true });
          if (t) return t;
        }
        // plain text preceding the first member inside this node
        var pre = textBefore(node, first, (exclude || []).concat(members));
        if (pre) return pre;
        node = parentOf(node);
      }
      return "";
    }
    function precedes(a, b) {
      if (a === b) return false;
      try {
        var pos = a.compareDocumentPosition(b);
        return !!(pos & 4) && !(pos & 16);
      } catch (e) { return false; }
    }
    function textBefore(container, anchor, exclude) {
      var parts = [];
      var stop = false;
      (function walk(node) {
        if (stop) return;
        if (node === anchor) { stop = true; return; }
        if (node.nodeType === 3) { parts.push(node.nodeValue); return; }
        if (node.nodeType !== 1) return;
        if (node !== container) {
          if (SKIP_TEXT_TAGS[tag(node)] || node.hidden) return;
          if (exclude && exclude.indexOf(node) >= 0) {
            if (node.contains(anchor)) stop = true;
            return;
          }
          if (isErrorish(node)) return;
        }
        var kids = node.childNodes;
        for (var i = 0; i < kids.length && !stop; i++) walk(kids[i]);
      })(container);
      return clean(parts.join(" ")).slice(-300);
    }
    function commonAncestor(els) {
      if (els.length === 1) return parentOf(els[0]);
      var a = els[0];
      for (var n = parentOf(a); n; n = parentOf(n)) {
        var all = true;
        for (var i = 1; i < els.length; i++) if (!n.contains(els[i])) { all = false; break; }
        if (all) return n;
      }
      return document.body;
    }

    function labelInfo(el, ads) {
      // returns {raw, label}
      var raw = callAdapters(ads, "labelFor", [el, ctx], true) || "";
      if (!raw) raw = labelledByText(el);
      if (!raw) raw = nativeLabelText(el);
      if (!raw) raw = attr(el, "aria-label");
      if (!raw) {
        // custom element hosts (e.g. <oc-input label="First name">)
        var r = rootNodeOf(el);
        if (r && r.host) raw = attr(r.host, "label") || attr(r.host, "aria-label");
      }
      if (!raw) raw = questionText([el], []);
      if (!raw) raw = attr(el, "placeholder") || attr(el, "title");
      if (!raw) raw = humanize(attr(el, "name") || el.id || "");
      raw = clean(raw).slice(0, 500);
      return { raw: raw, label: stripRequired(raw) };
    }
    function humanize(name) {
      var h = clean(String(name).replace(/[_\-\[\].]+/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2"));
      return h.charAt(0).toUpperCase() + h.slice(1);
    }
    function optionLabel(el) {
      var t = nativeLabelText(el) || labelledByText(el) || attr(el, "aria-label");
      if (!t) {
        // text right after the input inside its parent
        var parts = [];
        for (var n = el.nextSibling; n && parts.join("").length < 200; n = n.nextSibling) {
          if (n.nodeType === 1 && /^(INPUT|SELECT|TEXTAREA|BR)$/.test(tag(n))) break;
          parts.push(n.nodeType === 3 ? n.nodeValue : textOf(n, null, { max: 200 }));
        }
        t = clean(parts.join(" "));
      }
      if (!t) t = el.value || "";
      return stripRequired(t);
    }

    function describedBy(el) {
      var ids = attr(el, "aria-describedby").split(/\s+/).filter(Boolean);
      var desc = [], errs = [];
      ids.forEach(function (id) {
        var t = byIdIn(el, id);
        if (!t || !visible(t)) return;
        var txt = textOf(t, null, { max: 400 });
        if (!txt) return;
        if (isErrorish(t) || attr(t, "role") === "alert") errs.push(txt);
        else desc.push(txt);
      });
      return { description: clean(desc.join(" ")), error: clean(errs.join(" ")) };
    }

    // ------------------------------------------------------------ sections / headings
    var headingList = null;
    function headings() {
      if (headingList) return headingList;
      headingList = [].slice.call(document.querySelectorAll("h1, h2, h3, h4, [role=heading], [class*=section-title], [class*=sectionTitle], [class*=section-header]")).filter(function (h) {
        return visible(h) && textOf(h, null, { max: 150 });
      });
      return headingList;
    }
    function sectionOf(el, label) {
      var fs = el.closest ? el.closest("fieldset") : null;
      if (fs) {
        var lg = fs.querySelector("legend");
        var lt = lg ? stripRequired(textOf(lg, null, { max: 150 })) : "";
        if (lt && loose(lt) !== loose(label)) {
          // a legend of a fieldset that only wraps this one field is its label, not a section
          return lt;
        }
      }
      var pos = topLevel(el);
      var hs = headings();
      var best = null;
      for (var i = 0; i < hs.length; i++) {
        if (precedes(hs[i], pos) || hs[i].contains(pos)) best = hs[i];
      }
      if (!best) return "";
      var t = stripRequired(textOf(best, null, { max: 150 }));
      return loose(t) === loose(label) ? "" : t.slice(0, 150);
    }

    // ------------------------------------------------------------ field discovery
    function inCaptcha(el) {
      return !!(el.closest && el.closest(".g-recaptcha, .h-captcha, .cf-turnstile, #FunCaptcha, [data-hcaptcha-widget-id], .grecaptcha-badge"));
    }
    function isHoneypot(el, labelRaw) {
      var name = norm(attr(el, "name") + " " + el.id + " " + attr(el, "autocomplete"));
      var l = norm(labelRaw);
      if (/leave (this|it)?\s*(field)?\s*(blank|empty)|do not (fill|enter)|if you are (a )?human/.test(l)) return true;
      var tabNeg = attr(el, "tabindex") === "-1";
      if (/honeypot|hp[_-]?field|^hp_|bot[_-]?field|^fax$|url_confirm/.test(name)) return true;
      if (tabNeg && /\b(website|url|fax|homepage|nickname|email_confirm)\b/.test(name) && !/combobox/.test(attr(el, "role"))) {
        var s = style(el);
        var tiny = s && ((parseFloat(s.width) <= 1 && parseFloat(s.height) <= 1) || s.opacity === "0");
        if (tiny || isJsdom === false) return true;
      }
      return false;
    }

    function inputType(el) {
      var t = tag(el);
      if (t === "TEXTAREA") return "textarea";
      if (t === "SELECT") return "select";
      if (t !== "INPUT") return attr(el, "contenteditable") !== "" || el.isContentEditable ? "textarea" : "text";
      var ty = String(el.type || "text").toLowerCase();
      switch (ty) {
        case "email": case "tel": case "number": case "url": case "date": case "month":
        case "password": case "file": case "checkbox": case "radio":
          return ty;
        case "search": case "text": case "datetime-local": case "time": case "week": case "color": case "range":
        default:
          return "text";
      }
    }
    function dateFormatOf(el, labelRaw) {
      var hay = (attr(el, "placeholder") + " " + attr(el, "aria-label") + " " + attr(el, "data-format") + " " + labelRaw).toUpperCase();
      var m = hay.match(/\b(MM\/DD\/YYYY|DD\/MM\/YYYY|YYYY-MM-DD|MM-DD-YYYY|DD\.MM\.YYYY|MM\/YYYY|YYYY-MM|MM\/YY)\b/);
      return m ? m[1] : null;
    }

    function isComboInput(el) {
      if (attr(el, "role") === "combobox") return true;
      if (attr(el, "aria-autocomplete") === "list" || attr(el, "aria-autocomplete") === "both") return true;
      var wrap = el.closest ? el.closest("[role=combobox]") : null;
      if (wrap && wrap !== el) return true;
      if (el.closest && el.closest("[class*='select__control'], [class*='Select-control'], [class*='select-control']")) return true;
      return false;
    }

    // Builds the list of internal field descriptors.
    function collect(ads) {
      styleCache = new Map();
      hiddenCache = new Map();
      headingList = null;
      var rs = roots();
      var claimed = new Set();
      var descs = [];

      function claim(d) {
        (d.members || [d.el]).forEach(function (m) { claimed.add(m); });
        if (d.el) claimed.add(d.el);
        if (d.claim) d.claim.forEach(function (m) { claimed.add(m); });
        descs.push(d);
      }

      // 1. adapter-provided fields
      ads.forEach(function (ad) {
        if (typeof ad.extraFields !== "function") return;
        var extra = [];
        try { extra = ad.extraFields(document, ctx) || []; } catch (e) { log("extraFields failed", ad.name, e); }
        extra.forEach(function (d) {
          if (!d || !d.el || claimed.has(d.el)) return;
          d.adapter = ad;
          claim(d);
        });
      });

      // 2. native and ARIA controls
      var els = queryAll(CONTROL_SELECTOR, rs);
      var radioGroups = new Map();
      var checkGroups = new Map();
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (claimed.has(el)) continue;
        var t = tag(el);
        var role = attr(el, "role");
        if (t === "INPUT" && SKIP_INPUT_TYPES[String(el.type).toLowerCase()]) continue;
        if (inCaptcha(el)) continue;
        if (role === "option" ) continue;
        if (role === "listbox" && t !== "SELECT") continue; // listbox popups are part of comboboxes
        if (t !== "INPUT" && t !== "SELECT" && t !== "TEXTAREA" && el.querySelector && el.querySelector("input:not([type=hidden]), select, textarea")) {
          // wrapper element (e.g. div[role=combobox] containing the input) -> the inner input is the field
          if (role === "combobox") continue;
        }
        if (role === "combobox" && t !== "INPUT" && el.closest("[role=combobox]") !== el) continue;
        var ty = String(el.type || "").toLowerCase();
        var isFile = t === "INPUT" && ty === "file";
        if (!visible(el, { file: isFile })) {
          // custom-styled radios/checkboxes: the input is hidden but its label is the click target
          var choice = t === "INPUT" && (ty === "radio" || ty === "checkbox");
          if (!choice || el.hidden || !nativeLabels(el).some(function (l) { return visible(l); })) continue;
        }
        if (el.disabled && !isFile) continue;

        if (t === "INPUT" && ty === "radio") {
          var key = (el.form ? "f" : "d") + "|" + (el.name || ("__noname_" + idOf(parentOf(el))));
          var rootKey = rootNodeOf(el);
          var gk = key;
          var g = radioGroups.get(rootKey) || new Map();
          radioGroups.set(rootKey, g);
          if (!g.has(gk)) {
            g.set(gk, { el: el, type: "radio", widget: "radio-group", members: [] });
            claim(g.get(gk));
          }
          g.get(gk).members.push(el);
          claimed.add(el);
          continue;
        }
        if (t === "INPUT" && ty === "checkbox") {
          var ck = el.name ? "n|" + el.name : null;
          var gmap = checkGroups.get(rootNodeOf(el)) || new Map();
          checkGroups.set(rootNodeOf(el), gmap);
          if (ck && gmap.has(ck)) {
            gmap.get(ck).members.push(el);
            claimed.add(el);
            continue;
          }
          var cd = { el: el, type: "checkbox", widget: "checkbox-group", members: [el] };
          if (ck) gmap.set(ck, cd);
          claim(cd);
          continue;
        }
        if (role === "radio") {
          var rg = el.closest("[role=radiogroup]") || parentOf(el);
          var rgId = "aria|" + idOf(rg);
          var rgm = radioGroups.get(document) || new Map();
          radioGroups.set(document, rgm);
          if (!rgm.has(rgId)) {
            var ad = { el: el, type: "radio", widget: "aria-radio-group", members: [], group: rg };
            rgm.set(rgId, ad);
            claim(ad);
          }
          rgm.get(rgId).members.push(el);
          claimed.add(el);
          continue;
        }
        if (role === "checkbox" || role === "switch") {
          claim({ el: el, type: "checkbox", widget: "aria-checkbox", members: [el] });
          continue;
        }
        if ((t === "BUTTON" || role === "button") && attr(el, "aria-haspopup") === "listbox") {
          claim({ el: el, type: "combobox", widget: "listbox-button", members: [el] });
          continue;
        }
        if (role === "combobox" && t !== "INPUT") {
          claim({ el: el, type: "combobox", widget: "listbox-button", members: [el] });
          continue;
        }
        if (t === "INPUT" && isComboInput(el) && !/^(email|tel|number|url|password|date|month)$/.test(ty)) {
          claim({ el: el, type: "combobox", widget: "combobox-input", members: [el] });
          continue;
        }
        if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT") {
          claim({ el: el, type: inputType(el), widget: "native", members: [el] });
          continue;
        }
        if (attr(el, "contenteditable") === "" || attr(el, "contenteditable") === "true") {
          if (el.closest("[contenteditable=true]") !== el && parentOf(el) && parentOf(el).closest("[contenteditable=true], [contenteditable='']")) continue;
          claim({ el: el, type: "textarea", widget: "contenteditable", members: [el] });
        }
      }

      // 3. checkbox groups without names: siblings in the same fieldset/question
      var singles = descs.filter(function (d) { return d.widget === "checkbox-group" && d.members.length === 1; });
      var byFs = new Map();
      singles.forEach(function (d) {
        var fs = d.el.closest("fieldset, [role=group]");
        if (!fs) return;
        if (!byFs.has(fs)) byFs.set(fs, []);
        byFs.get(fs).push(d);
      });
      byFs.forEach(function (list, fs) {
        if (list.length < 2) return;
        // only if the fieldset contains nothing but these checkboxes
        if (countControls(fs, list.map(function (d) { return d.el; })) > 0) return;
        var head = list[0];
        list.slice(1).forEach(function (d) {
          head.members.push(d.el);
          descs.splice(descs.indexOf(d), 1);
        });
      });

      // finalize types
      descs.forEach(function (d) {
        if (d.widget === "checkbox-group") {
          d.type = d.members.length > 1 ? "checkbox_group" : "checkbox";
          if (d.type === "checkbox") d.widget = "checkbox";
        }
      });

      // stable document order
      descs.sort(function (a, b) {
        if (a.el === b.el) return 0;
        var pa = topLevel(a.el), pb = topLevel(b.el);
        if (pa === pb) return precedes(a.el, b.el) ? -1 : 1;
        return precedes(pa, pb) ? -1 : 1;
      });
      return descs;
    }

    // ------------------------------------------------------------ field description
    function groupLabelInfo(d, ads) {
      var raw = callAdapters(ads, "labelFor", [d.el, ctx], true) || "";
      if (!raw && d.group) raw = labelledByText(d.group) || attr(d.group, "aria-label");
      if (!raw) {
        var fs = commonAncestor(d.members);
        var f = fs && fs.closest ? fs.closest("fieldset") : null;
        if (f && d.members.every(function (m) { return f.contains(m); }) && countControls(f, d.members) === 0) {
          var lg = f.querySelector("legend");
          if (lg) raw = textOf(lg, null, { max: 400 });
        }
      }
      if (!raw) {
        var rg = d.el.closest ? d.el.closest("[role=radiogroup], [role=group]") : null;
        if (rg && d.members.every(function (m) { return rg.contains(m); })) raw = labelledByText(rg) || attr(rg, "aria-label");
      }
      if (!raw) {
        var memberLabels = [];
        d.members.forEach(function (m) { nativeLabels(m).forEach(function (l) { memberLabels.push(l); }); });
        raw = questionText(d.members, memberLabels);
      }
      raw = clean(raw).slice(0, 500);
      return { raw: raw, label: stripRequired(raw) };
    }

    function comboRoot(el) {
      var ctl = el.closest("[class*='select__control'], [class*='Select-control'], [class*='select-control'], [class*='__control']");
      if (ctl && parentOf(ctl)) return parentOf(ctl);
      var wrap = el.closest("[role=combobox]");
      if (wrap && wrap !== el) return parentOf(wrap) || wrap;
      return parentOf(parentOf(el)) || parentOf(el) || el;
    }
    function comboDisplay(d) {
      var el = d.el;
      if (d.widget === "listbox-button") {
        var t = elementText(el);
        if (/^(select( one)?|select\.\.\.|choose( one)?|-+|please select)$/i.test(t)) return "";
        return t;
      }
      var r = comboRoot(el);
      var sv = r.querySelector("[class*='single-value'], [class*='singleValue'], [class*='SingleValue']");
      if (sv && clean(sv.textContent)) return clean(sv.textContent);
      var mv = r.querySelectorAll("[class*='multi-value__label'], [class*='multiValue'] [class*='label'], [class*='MultiValueLabel']");
      if (mv.length) return [].map.call(mv, function (m) { return clean(m.textContent); }).join(", ");
      return clean(el.value || "");
    }

    function readOptionsNative(d) {
      var el = d.el;
      if (d.type === "select") {
        var out = [];
        [].forEach.call(el.options, function (o, idx) {
          if (o.disabled && idx === 0) return;
          var label = clean(o.label || o.textContent);
          if (idx === 0 && o.value === "" && (/^(|-+.*|select.*|please select.*|choose.*|none selected|\u2014.*)$/i.test(label))) return;
          if (o.hidden) return;
          out.push({ label: label, value: o.value, selected: !!o.selected });
        });
        return out;
      }
      if (d.type === "radio" || d.type === "checkbox_group") {
        return d.members.map(function (m) {
          if (d.widget === "aria-radio-group") return { label: stripRequired(elementText(m)), value: attr(m, "data-value") || stripRequired(elementText(m)), selected: attr(m, "aria-checked") === "true" };
          if (d.widget === "button-group") return { label: stripRequired(elementText(m)), value: attr(m, "value") || stripRequired(elementText(m)), selected: buttonActive(m) };
          return { label: optionLabel(m), value: m.value, selected: !!m.checked };
        });
      }
      return null;
    }
    function buttonActive(b) {
      if (attr(b, "aria-pressed") === "true" || attr(b, "aria-checked") === "true" || attr(b, "aria-selected") === "true") return true;
      return /(^|[\s_-])(active|selected|checked|isSelected|is-selected|_active_)/i.test(classOf(b)) || /_active/.test(classOf(b));
    }

    function readValue(d) {
      var el = d.el;
      switch (d.type) {
        case "password": return null;
        case "file": return null;
        case "checkbox":
          if (d.widget === "aria-checkbox") return attr(el, "aria-checked") === "true";
          return !!el.checked;
        case "checkbox_group":
          return readOptionsNative(d).filter(function (o) { return o.selected; }).map(function (o) { return o.label; });
        case "radio": {
          if (d.widget === "custom" && d.adapter && d.readValue) return d.readValue();
          var sel = (d.options || readOptionsNative(d) || []).filter(function (o) { return o.selected; });
          return sel.length ? sel[0].label : null;
        }
        case "select": {
          if (el.multiple) return [].filter.call(el.options, function (o) { return o.selected; }).map(function (o) { return clean(o.label || o.textContent); });
          var o = el.options[el.selectedIndex];
          if (!o) return null;
          if (o.value === "" && el.selectedIndex === 0) return null;
          return clean(o.label || o.textContent);
        }
        case "combobox":
          if (typeof d.readValue === "function") return d.readValue();
          return comboDisplay(d) || null;
        default:
          if (d.widget === "contenteditable") return clean(el.textContent);
          if (typeof d.readValue === "function") return d.readValue();
          return el.value == null ? null : String(el.value);
      }
    }

    function fieldContainer(d, allMembers) {
      var members = d.members || [d.el];
      var node = members.length > 1 ? commonAncestor(members) : parentOf(d.el);
      var best = node;
      for (var depth = 0; node && depth < 5; depth++) {
        if (/^(FORM|BODY|HTML)$/.test(tag(node))) break;
        var others = 0;
        for (var i = 0; i < allMembers.length; i++) {
          var m = allMembers[i];
          if (members.indexOf(m) >= 0) continue;
          if (node.contains(m)) { others++; break; }
        }
        if (others) break;
        best = node;
        node = parentOf(node);
      }
      return best;
    }

    function containerError(container, d) {
      if (!container) return "";
      var found = container.querySelectorAll("[role=alert], [class*=error], [class*=Error], [class*=invalid], [data-automation-id=errorMessage]");
      var texts = [];
      for (var i = 0; i < found.length; i++) {
        var n = found[i];
        if (d.members.some(function (m) { return n === m || n.contains(m); })) continue;
        if (!isErrorish(n) && attr(n, "role") !== "alert") continue;
        if (!visible(n)) continue;
        var t = textOf(n, null, { max: 300 });
        if (t && texts.indexOf(t) < 0 && !texts.some(function (x) { return x.indexOf(t) >= 0; })) texts.push(t);
      }
      return clean(texts.join(" ")).slice(0, 300);
    }

    function containerDescription(container, d, labelText) {
      if (!container) return "";
      var found = container.querySelectorAll("[class*=description], [class*=Description], [class*=help], [class*=hint], [class*=instructions], [class*=subtitle], small");
      var out = [];
      for (var i = 0; i < found.length; i++) {
        var n = found[i];
        if (d.members.some(function (m) { return n.contains(m); })) continue;
        if (isErrorish(n) || !visible(n)) continue;
        var t = textOf(n, null, { max: 300 });
        if (t && loose(t) !== loose(labelText) && out.indexOf(t) < 0) out.push(t);
      }
      return clean(out.join(" ")).slice(0, 500);
    }

    function describe(d, ads, allMembers) {
      var el = d.el;
      var li;
      if (d.label != null) li = { raw: d.rawLabel || d.label, label: stripRequired(d.label) };
      else if (d.type === "radio" || d.type === "checkbox_group") li = groupLabelInfo(d, ads);
      else if (d.widget === "listbox-button") {
        li = labelInfo(el, ads);
        // aria-label of Workday-like buttons repeats the current value: "State Select One Required"
        li.label = stripRequired(li.label.replace(/\s+(select one|select)\s*(required)?$/i, ""));
        if (/required$/i.test(li.raw)) li.raw += " *";
      } else li = labelInfo(el, ads);

      var container = fieldContainer(d, allMembers);
      var section = d.section != null ? d.section : sectionOf(el, li.label);
      var required = d.required != null ? !!d.required : !!(
        el.required || attr(el, "aria-required") === "true" || looksRequired(li.raw) ||
        d.members.some(function (m) { return m.required || attr(m, "aria-required") === "true"; }) ||
        (d.group && attr(d.group, "aria-required") === "true")
      );
      var hint = d.hint || null;
      if (d.widget === "listbox-button" && !hint) hint = "listbox";
      if (d.widget === "button-group" && !hint) hint = "buttons";
      if (d.widget === "combobox-input" && !hint) hint = "combobox";

      var type = d.type;
      var dateFormat = d.dateFormat || null;
      if (type === "text" && d.widget === "native") {
        dateFormat = dateFormatOf(el, li.raw);
        if (dateFormat) {
          type = /DD/.test(dateFormat) ? "date" : "month";
          hint = hint || "text-date:" + dateFormat;
          d.dateFormat = dateFormat;
        }
      }
      d.type = type;

      var options = d.options || readOptionsNative(d);
      if (options) options = options.map(function (o) { return { label: o.label, value: o.value != null ? String(o.value) : o.label, selected: !!o.selected }; });
      if (type === "combobox" && !options) {
        var visibleOpts = currentOptions(d);
        if (visibleOpts.length) options = visibleOpts.map(function (o) { return { label: o.label, value: o.value, selected: false }; });
      }
      d.options = options || null;

      var db = describedBy(el);
      var error = db.error || containerError(container, d);
      var adapterHint = callAdapters(ads, "fieldHint", [el, d, ctx], true);
      if (adapterHint) hint = adapterHint;

      var description = db.description || containerDescription(container, d, li.raw);
      var name = attr(el, "name") || el.id || "";
      var isFileType = type === "file";
      var field = {
        id: idOf(el),
        key: "f_" + fnv1a([loose(li.label), name.replace(/\d{4,}/g, "#"), type, loose(section)].join("|")),
        frame: 0,
        type: type,
        label: li.label,
        name: name,
        description: description,
        placeholder: attr(el, "placeholder"),
        section: section,
        required: required,
        disabled: !!(el.disabled || el.readOnly || attr(el, "aria-disabled") === "true" || attr(el, "aria-readonly") === "true"),
        value: null,
        options: d.options,
        options_dynamic: type === "combobox" ? !(d.options && d.options.length) || !!d.optionsDynamic : false,
        multiple: !!(el.multiple || attr(el, "aria-multiselectable") === "true" || d.multiple),
        max_length: el.maxLength > 0 ? el.maxLength : (parseInt(attr(el, "maxlength"), 10) > 0 ? parseInt(attr(el, "maxlength"), 10) : null),
        min_length: el.minLength > 0 ? el.minLength : null,
        autocomplete: attr(el, "autocomplete") && attr(el, "autocomplete") !== "off" && attr(el, "autocomplete") !== "on" ? attr(el, "autocomplete") : null,
        accept: isFileType ? (attr(el, "accept") || null) : null,
        files: isFileType ? [].map.call(el.files || [], function (f) { return f.name; }) : undefined,
        error: error || null,
        hint: hint
      };
      if (!isFileType) delete field.files;
      try { field.value = readValue(d); } catch (e) { field.value = null; }
      if (type === "password") field.value = null;
      d.field = field;
      d.labelRaw = li.raw;
      return field;
    }

    // ------------------------------------------------------------ buttons
    var KIND_RULES = [
      ["add", /^(\+\s*)?add( another| more| new)?\b|^\+$/],
      ["verify", /^(verify|verify (my )?(email|code|account|identity)|confirm (code|email)|submit code|validate)$/],
      ["back", /^(back|previous|prev|go back|<\s*back|« back|‹ back)\b/],
      ["submit", /^submit\b|submit (my |your )?application|send (my )?application|^send$|complete (my )?application|finish (application|and submit)|^finish$|^submit$/],
      ["signup", /create (an |your |my )?(candidate )?account|^sign ?up\b|^register\b|^join now\b/],
      ["login", /\b(sign ?in|log ?in)\b/],
      ["next", /^(next|continue|save (and|&) continue|save and next|review|review (my |your )?application|proceed|next step|save & next)\b/],
      ["apply", /^apply\b|apply (now|for this (job|position|role)|manually|to (this )?job|on company site)|i'?m interested|start (my |your )?application|easy apply|^apply$/]
    ];
    function classifyButton(el, text, ads, formFieldCount) {
      var k = callAdapters(ads, "classifyButton", [el, text, ctx], true);
      if (k) return k;
      var t = norm(text).replace(/[→›»>]+$/, "").trim();
      if (!t) return "other";
      for (var i = 0; i < KIND_RULES.length; i++) {
        if (KIND_RULES[i][1].test(t)) {
          var kind = KIND_RULES[i][0];
          if (kind === "apply" && /^apply$/.test(t) && formFieldCount >= 3) return "submit";
          if (kind === "login" && /create|sign ?up|register/.test(t)) return "signup";
          return kind;
        }
      }
      if (/^(next|continue)\s*[>→›»]?$/.test(t)) return "next";
      return "other";
    }
    function collectButtons(ads, fieldEls, fieldCountByForm) {
      var sel = "button, input[type=submit], input[type=button], input[type=image], [role=button], a[href], [role=link], [role=tab]";
      var els = queryAll(sel);
      var out = [];
      var seen = new Set();
      els.forEach(function (el) {
        if (seen.has(el)) return;
        seen.add(el);
        if (fieldEls.has(el)) return;
        if (attr(el, "aria-haspopup") === "listbox") return;
        if (inCaptcha(el)) return;
        if (el.closest("[class*=indicator], [role=listbox], [role=option]")) return;
        if (!visible(el)) return;
        // nested clickables: keep the outermost real button
        var outer = parentOf(el) && parentOf(el).closest ? parentOf(el).closest("button, [role=button], a[href]") : null;
        if (outer && outer !== el && seen.has(outer)) return;
        var text = elementText(el);
        if (!text) return;
        var isAnchor = tag(el) === "A" && attr(el, "role") !== "button";
        var form = el.form || (el.closest && el.closest("form"));
        var fc = form ? (fieldCountByForm.get(form) || 0) : 0;
        var kind = classifyButton(el, text, ads, fc);
        if (isAnchor && kind === "other" && !/(^|\s|-|_)(btn|button)/i.test(classOf(el))) return;
        if (attr(el, "role") === "tab" && kind === "other") return;
        out.push({
          id: idOf(el),
          text: text.slice(0, 120),
          kind: kind,
          disabled: !!(el.disabled || attr(el, "aria-disabled") === "true"),
          frame: 0
        });
      });
      return out;
    }

    // ------------------------------------------------------------ errors
    function collectErrors(descs, ads) {
      var errors = [];
      function exists(text, fid) {
        return errors.some(function (e) {
          return (e.field_id === fid || fid === null || e.field_id === null) && (e.text === text || (e.field_id === fid && e.text.indexOf(text) >= 0));
        });
      }
      function add(text, fid) {
        text = clean(text).slice(0, 300);
        fid = fid || null;
        if (!text || exists(text, fid)) return;
        errors.push({ text: text, field_id: fid });
      }
      descs.forEach(function (d) {
        if (d.field && d.field.error) add(d.field.error, d.field.id);
      });
      var found = queryAll("[role=alert], [aria-live=assertive], [class*=error], [class*=Error], [class*=invalid], [data-automation-id=errorMessage], [data-automation-id=errorBanner]");
      found.forEach(function (n) {
        if (errors.length >= 30) return;
        if (!(isErrorish(n) || attr(n, "role") === "alert" || attr(n, "aria-live") === "assertive")) return;
        if (n.querySelector("input, select, textarea")) return;
        if (n.hasAttribute("data-fixture-banner")) return;
        if (!visible(n)) return;
        var t = textOf(n, null, { max: 300 });
        if (!t) return;
        var fid = null;
        for (var i = 0; i < descs.length; i++) {
          var c = descs[i].container;
          if (c && c.contains(n)) { fid = descs[i].field.id; break; }
        }
        // nested error wrappers: skip when a parent/child with the same text was already taken
        if (errors.some(function (e) { return e.field_id === fid && (e.text.indexOf(t) >= 0 || t.indexOf(e.text) >= 0) && fid !== null; })) return;
        add(t, fid);
      });
      callAdapters(ads, "errors", [document, ctx]).forEach(function (list) {
        (list || []).forEach(function (e) {
          if (typeof e === "string") add(e, null);
          else if (e && e.text) add(e.text, e.el ? idOf(e.el) : (e.field_id || null));
        });
      });
      return errors;
    }

    // ------------------------------------------------------------ captcha & page kind
    function iframeVisibleSize(f) {
      if (!visible(f)) return false;
      // any opacity:0 ancestor hides challenge popups (hCaptcha)
      for (var n = f; n && n.nodeType === 1; n = parentOf(n)) {
        var s = style(n);
        if (s && s.opacity === "0") return false;
      }
      if (!isJsdom && f.getBoundingClientRect) {
        var r = f.getBoundingClientRect();
        return r.width >= 30 && r.height >= 30;
      }
      var s2 = style(f) || {};
      var w = parseFloat(s2.width) || parseFloat(attr(f, "width"));
      var h = parseFloat(s2.height) || parseFloat(attr(f, "height"));
      if (isNaN(w) && isNaN(h)) return true;
      return !(w < 30 || h < 30);
    }
    function detectCaptcha(visText) {
      var res = { present: false, kind: null, interactive: false, detail: null };
      var href = String(win.location && win.location.href || "");
      if (CAPTCHA_FRAME_SELF_RE.test(href)) {
        return { present: true, kind: kindFromUrl(href), interactive: true, detail: "captcha frame" };
      }
      var frames = queryAll("iframe");
      for (var i = 0; i < frames.length; i++) {
        var f = frames[i];
        var src = attr(f, "src") || "";
        var title = attr(f, "title") || "";
        var isCap = CAPTCHA_URL_RE.test(src) || /recaptcha|hcaptcha|captcha|turnstile|arkose|funcaptcha/i.test(title + " " + src);
        if (!isCap) continue;
        res.present = true;
        res.kind = res.kind || kindFromUrl(src + " " + title);
        if (f.closest(".grecaptcha-badge") || /[?&]size=invisible/.test(src)) continue;
        if (f.closest("[data-size=invisible]") && !/frame=challenge|bframe/.test(src)) continue;
        if (iframeVisibleSize(f)) {
          res.interactive = true;
          res.kind = kindFromUrl(src + " " + title);
          res.detail = (title || src).slice(0, 120);
          return res;
        }
      }
      var widgets = queryAll(".g-recaptcha, .h-captcha, .cf-turnstile, #FunCaptcha, [data-sitekey], .grecaptcha-badge, script[src*='recaptcha'], script[src*='hcaptcha'], script[src*='turnstile']");
      if (widgets.length) {
        res.present = true;
        res.kind = res.kind || kindFromUrl(widgets.map(function (w) { return classOf(w) + " " + attr(w, "src"); }).join(" "));
      }
      var turn = queryAll(".cf-turnstile").filter(function (w) { return visible(w) && w.querySelector("iframe"); });
      if (turn.length) { res.interactive = true; res.kind = "turnstile"; res.detail = "cf-turnstile"; return res; }
      // Cloudflare interstitial challenge page
      var title2 = clean(document.title);
      if ((/^just a moment/i.test(title2) || /attention required/i.test(title2)) && /verify(ing)? you are (a )?human|checking (if the site connection is secure|your browser)|needs to review the security/i.test(visText)) {
        return { present: true, kind: "cloudflare", interactive: true, detail: "cloudflare challenge page" };
      }
      if (document.querySelector("#challenge-form, #cf-challenge-running, .cf-browser-verification, #challenge-stage") && /verify(ing)? you are (a )?human/i.test(visText)) {
        return { present: true, kind: "cloudflare", interactive: true, detail: "cloudflare challenge page" };
      }
      return res;
    }
    function kindFromUrl(s) {
      s = String(s).toLowerCase();
      if (/h-?captcha/.test(s)) return "hcaptcha";
      if (/turnstile|cloudflare/.test(s)) return "turnstile";
      if (/arkose|funcaptcha/.test(s)) return "arkose";
      if (/recaptcha/.test(s)) return "recaptcha";
      return "unknown";
    }

    var CONFIRM_RES = [
      /thank(s| you) for (applying|your application|submitting( your application)?)/i,
      /application (has been |was )?(successfully )?(submitted|received|sent)\b/i,
      /(we('ve| have)|we) (successfully )?received your application/i,
      /\bsuccessfully submitted\b/i,
      /\bsubmitted successfully\b/i,
      /your application is (complete|in)\b/i
    ];
    var ALREADY_RE = /you('ve| have) already applied|already (submitted an|applied for this|applied to this)|you applied (to|for) this (job|position|role) on/i;
    var CLOSED_RE = /no longer accepting applications|(position|job|role) has (been filled|closed|expired)|job is no longer available|(job|position|posting) (is )?no longer (available|open|active)|this (job|position|posting) (has expired|is closed)|job (posting )?not found|no longer (open|available) for applications|this requisition is (closed|no longer)/i;
    var NOT_FOUND_RE = /page not found|404\b|the page you (are|were) looking for (doesn'?t|does not|could not|cannot)|this page (doesn'?t|does not) exist/i;
    var ERROR_RE = /something went wrong|an (unexpected )?error (has )?occurred|internal server error|\b500\b|access denied|403 forbidden|service unavailable|too many requests/i;
    var VERIFY_TEXT_RE = /enter (the |your )?(\d[- ]digit |verification |security |one[- ]time )?(code|passcode|pin)|verification code|we (just )?(sent|emailed|texted) (you )?(a |an )?(\d[- ]digit )?(code|verification)|verify your (email|account|identity)|one[- ]time (code|password|passcode)|check your (email|inbox)|verification link|confirm your email address/i;

    function isCodeField(d) {
      var f = d.field;
      if (!f || !/^(text|number|tel)$/.test(f.type)) return false;
      var hay = norm([f.label, f.name, f.placeholder, f.autocomplete, attr(d.el, "inputmode")].join(" "));
      if (f.autocomplete === "one-time-code") return true;
      return /\b(code|otp|passcode|pin|verification|token)\b/.test(hay) && !/zip|postal|country|area|phone|promo|discount|referral/.test(hay);
    }

    function pageKind(ctxInfo, ads) {
      var k = callAdapters(ads, "pageKind", [document, ctxInfo, ctx], true);
      if (k) return k;
      var fields = ctxInfo.fields, buttons = ctxInfo.buttons, text = ctxInfo.text;
      var fillable = ctxInfo.descs.filter(function (d) { return !d.field.disabled && !/search|keyword/i.test(d.field.label + " " + d.field.name); });
      var n = fillable.length;
      if (ctxInfo.captcha.interactive) return "captcha";
      if (ctxInfo.confirmation.detected) return "confirmation";
      if (ALREADY_RE.test(text)) return "already_applied";
      if (n <= 1 && CLOSED_RE.test(text)) return "closed";
      if (n === 0 && NOT_FOUND_RE.test(text) && !buttons.some(function (b) { return b.kind === "apply"; })) return "closed";
      var passwords = fields.filter(function (f) { return f.type === "password"; });
      var codeFields = ctxInfo.descs.filter(isCodeField);
      if (codeFields.length && passwords.length === 0 && n <= 3 && VERIFY_TEXT_RE.test(text)) return "verification";
      if (codeFields.length && passwords.length === 0 && n <= 2 && buttons.some(function (b) { return b.kind === "verify"; })) return "verification";
      if (n === 0 && /check your (email|inbox)|verification link|verify your email|confirm your email/i.test(text)) return "verification";
      if (passwords.length >= 2) return "signup";
      if (passwords.length === 1) {
        var conf = fields.some(function (f) { return f.type === "password" && /confirm|verify|re-?enter|repeat|again/i.test(f.label + " " + f.name); });
        if (conf) return "signup";
        var hasLogin = buttons.some(function (b) { return b.kind === "login"; });
        var hasSignup = buttons.some(function (b) { return b.kind === "signup"; });
        var heading = norm(ctxInfo.heading);
        if (/create|sign ?up|register/.test(heading) || (hasSignup && !hasLogin)) return "signup";
        return "login";
      }
      if (n > 0) {
        if (n <= 1 && buttons.some(function (b) { return b.kind === "apply"; })) return "job_description";
        return "form";
      }
      if (buttons.some(function (b) { return b.kind === "apply"; })) return "job_description";
      if (ERROR_RE.test(text.slice(0, 2000))) return "error";
      return "unknown";
    }

    function detectConfirmation(visText, descs) {
      var required = descs.filter(function (d) { return d.field && d.field.required && !d.field.disabled; });
      var n = descs.length;
      for (var i = 0; i < CONFIRM_RES.length; i++) {
        var m = visText.match(CONFIRM_RES[i]);
        if (!m) continue;
        // prominent: inside a heading / status region
        var prominent = queryAll("h1, h2, h3, [role=status], [role=alert], [class*=confirm], [class*=success], [class*=thank], [data-automation-id*=ongratulation]").some(function (h) {
          return visible(h) && CONFIRM_RES[i].test(textOf(h, null, { max: 400 }));
        });
        if (n <= 2 || (prominent && required.length === 0) || (prominent && n <= 4)) {
          return { detected: true, text: m[0] };
        }
      }
      return { detected: false, text: null };
    }

    // ------------------------------------------------------------ visible text
    function visibleText(max) {
      max = max || 20000;
      var chunks = [];
      var total = 0;
      var visCache = new Map();
      function parentVisible(p) {
        if (visCache.has(p)) return visCache.get(p);
        var v = true;
        for (var n = p; n; n = parentOf(n)) {
          if (n.nodeType !== 1) continue;
          if (visCache.has(n) && n !== p) { v = visCache.get(n); break; }
          if (SKIP_TEXT_TAGS[tag(n)] && tag(n) !== "TEXTAREA" && tag(n) !== "SELECT") { v = false; break; }
          if (selfHidden(n)) { v = false; break; }
        }
        visCache.set(p, v);
        return v;
      }
      var rs = roots();
      var count = 0;
      for (var r = 0; r < rs.length && total < max; r++) {
        var start = rs[r].body || rs[r];
        if (!start) continue;
        var walker = document.createTreeWalker(start, 4 /* SHOW_TEXT */);
        while (walker.nextNode() && total < max && count < 40000) {
          count++;
          var node = walker.currentNode;
          var v = node.nodeValue;
          if (!v || !v.trim()) continue;
          var p = node.parentNode;
          if (!p || p.nodeType !== 1) continue;
          if (!parentVisible(p)) continue;
          var t = clean(v);
          chunks.push(t);
          total += t.length + 1;
        }
      }
      return clean(chunks.join(" ")).slice(0, max);
    }

    function pageHeading() {
      var hs = ["h1", "h2", "[role=heading]"];
      for (var i = 0; i < hs.length; i++) {
        var list = queryAll(hs[i]);
        for (var j = 0; j < list.length; j++) {
          if (!visible(list[j])) continue;
          var t = textOf(list[j], null, { max: 200 });
          if (t) return t.slice(0, 200);
        }
      }
      return clean(document.title).slice(0, 200);
    }

    function detectStep(ads, visText) {
      var s = callAdapters(ads, "step", [document, ctx], true);
      if (s) return s;
      var pb = queryAll("[role=progressbar][aria-valuenow][aria-valuemax]").filter(function (p) { return visible(p); })[0];
      if (pb) {
        var cur = parseInt(attr(pb, "aria-valuenow"), 10), tot = parseInt(attr(pb, "aria-valuemax"), 10);
        if (cur > 0 && tot > 0 && tot <= 20) return { current: cur, total: tot, label: attr(pb, "aria-valuetext") || null };
      }
      var cs = queryAll("[aria-current=step]").filter(function (p) { return visible(p); })[0];
      if (cs) {
        var list = cs.closest("ol, ul");
        if (list) {
          var items = [].filter.call(list.children, function (c) { return tag(c) === "LI"; });
          var idx = items.indexOf(cs.closest("li") || cs);
          if (idx >= 0) return { current: idx + 1, total: items.length, label: textOf(cs, null, { max: 80 }) || null };
        }
      }
      var m = visText.match(/\bstep (\d{1,2}) of (\d{1,2})\b/i);
      if (m) return { current: parseInt(m[1], 10), total: parseInt(m[2], 10), label: null };
      return null;
    }

    // ------------------------------------------------------------ scan
    function isCaptchaFrame() {
      return CAPTCHA_FRAME_SELF_RE.test(String(win.location && win.location.href || ""));
    }

    function scan(opts) {
      opts = opts || {};
      var started = Date.now();
      var url = String(win.location && win.location.href || "");
      if (isCaptchaFrame()) {
        return {
          url: url, title: clean(document.title), frames: 1, ats: "generic", page_kind: "captcha", heading: "", step: null,
          fields: [], buttons: [], errors: [], captcha: { present: true, kind: kindFromUrl(url), interactive: true, detail: "captcha frame" },
          confirmation: { detected: false, text: null }, signals: { has_password: false, has_submit: false, text_hash: fnv1a("") },
          fingerprint: fnv1a(url), field_count: 0
        };
      }
      var ads = activeAdapters();
      var descs;
      try {
        descs = collect(ads);
        var allMembers = [];
        descs.forEach(function (d) { (d.members || [d.el]).forEach(function (m) { allMembers.push(m); }); });
        var fields = [];
        descs = descs.filter(function (d) {
          try {
            var f = describe(d, ads, allMembers);
            if (isHoneypot(d.el, d.labelRaw)) return false;
            d.container = fieldContainer(d, allMembers);
            fields.push(f);
            return true;
          } catch (e) {
            log("describe failed", e);
            return false;
          }
        });
        lastFields = new Map();
        descs.forEach(function (d) { lastFields.set(d.field.id, d); });

        var fieldEls = new Set();
        descs.forEach(function (d) {
          (d.members || []).forEach(function (m) { fieldEls.add(m); });
          fieldEls.add(d.el);
          (d.claim || []).forEach(function (m) { fieldEls.add(m); });
        });
        var countByForm = new Map();
        descs.forEach(function (d) {
          var fm = d.el.form || (d.el.closest && d.el.closest("form"));
          if (fm) countByForm.set(fm, (countByForm.get(fm) || 0) + 1);
        });
        var buttons = collectButtons(ads, fieldEls, countByForm);
        var errors = collectErrors(descs, ads);
        var text = visibleText(Math.max(20000, opts.max_text || 0));
        var captcha = detectCaptcha(text);
        var confirmation = detectConfirmation(text, descs);
        var heading = pageHeading();
        var info = { fields: fields, buttons: buttons, text: text, descs: descs, captcha: captcha, confirmation: confirmation, heading: heading };
        var kind = pageKind(info, ads);
        if (kind === "confirmation" && !confirmation.detected) confirmation = { detected: true, text: heading };
        var ats = "generic";
        for (var i = 0; i < ads.length; i++) if (ads[i].name !== "generic") { ats = ads[i].name; break; }
        var snapshot = {
          url: url,
          title: clean(document.title),
          frames: 1,
          ats: ats,
          page_kind: kind,
          heading: heading,
          step: detectStep(ads, text),
          fields: fields,
          buttons: buttons,
          errors: errors,
          captcha: captcha,
          confirmation: confirmation,
          signals: {
            has_password: fields.some(function (f) { return f.type === "password"; }),
            has_submit: buttons.some(function (b) { return b.kind === "submit"; }),
            text_hash: fnv1a(text)
          },
          fingerprint: fnv1a([pathOf(url), heading, fields.map(function (f) { return f.key; }).join(",")].join("|")),
          field_count: fields.length
        };
        var extra = callAdapters(ads, "atsData", [document, ctx], true);
        if (extra) snapshot.ats_data = extra;
        if (opts.include_text) snapshot.text = text.slice(0, opts.max_text || 4000);
        log("scan", fields.length, "fields", buttons.length, "buttons", Date.now() - started, "ms");
        return snapshot;
      } finally {
        styleCache = null;
        hiddenCache = null;
        headingList = null;
        // ensure ids allocated in this scan stay stable
      }
    }
    function pathOf(url) {
      try { return new win.URL(url).pathname; } catch (e) { return url; }
    }

    // ------------------------------------------------------------ events & primitives
    function fire(el, type, init, Ctor) {
      init = Object.assign({ bubbles: true, cancelable: true, composed: true }, init || {});
      var C = Ctor && win[Ctor] ? win[Ctor] : win.Event;
      var ev;
      try { ev = new C(type, init); } catch (e) { ev = new win.Event(type, init); }
      return el.dispatchEvent(ev);
    }
    function focusEl(el) {
      try { el.focus({ preventScroll: true }); } catch (e) { try { el.focus(); } catch (e2) { /* ignore */ } }
      var active = rootNodeOf(el).activeElement;
      if (active !== el) {
        fire(el, "focus", { bubbles: false }, "FocusEvent");
        fire(el, "focusin", {}, "FocusEvent");
      }
    }
    function blurEl(el) {
      var active = rootNodeOf(el).activeElement;
      try { el.blur(); } catch (e) { /* ignore */ }
      if (active !== el) {
        fire(el, "blur", { bubbles: false }, "FocusEvent");
        fire(el, "focusout", {}, "FocusEvent");
      }
    }
    function scrollTo(el) {
      try { if (el.scrollIntoView) el.scrollIntoView({ block: "center", inline: "nearest" }); } catch (e) { /* ignore */ }
    }
    function mouseSequence(el, opts) {
      opts = opts || {};
      var rect = null;
      try { rect = el.getBoundingClientRect(); } catch (e) { rect = null; }
      var x = rect ? rect.left + rect.width / 2 : 0, y = rect ? rect.top + rect.height / 2 : 0;
      var base = { clientX: x, clientY: y, button: 0, view: win };
      fire(el, "pointerover", Object.assign({ pointerType: "mouse" }, base), "PointerEvent");
      fire(el, "mouseover", base, "MouseEvent");
      fire(el, "pointerdown", Object.assign({ pointerType: "mouse", buttons: 1 }, base), "PointerEvent");
      fire(el, "mousedown", Object.assign({ buttons: 1 }, base), "MouseEvent");
      if (opts.focus !== false && typeof el.focus === "function") {
        try { el.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
      }
      fire(el, "pointerup", Object.assign({ pointerType: "mouse" }, base), "PointerEvent");
      fire(el, "mouseup", base, "MouseEvent");
      if (opts.noClick) return { prevented: false };
      var captured = null;
      var cap = function (e) { if (!captured) captured = e; };
      win.addEventListener("click", cap, true);
      try {
        if (typeof el.click === "function") el.click();
        else fire(el, "click", base, "MouseEvent");
      } finally {
        win.removeEventListener("click", cap, true);
      }
      return { prevented: captured ? !!captured.defaultPrevented : false };
    }
    function setNativeValue(el, value) {
      var proto = tag(el) === "TEXTAREA" ? win.HTMLTextAreaElement.prototype : tag(el) === "SELECT" ? win.HTMLSelectElement.prototype : win.HTMLInputElement.prototype;
      var desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (desc && desc.set) desc.set.call(el, value);
      else el.value = value;
    }
    function inputEvents(el, value) {
      fire(el, "beforeinput", { inputType: "insertText", data: value }, "InputEvent");
      fire(el, "input", { inputType: "insertText", data: value, cancelable: false }, "InputEvent");
    }
    function typeText(el, value, opts) {
      opts = opts || {};
      focusEl(el);
      if (attr(el, "contenteditable") != null && tag(el) !== "INPUT" && tag(el) !== "TEXTAREA") {
        el.textContent = value;
        inputEvents(el, value);
      } else {
        setNativeValue(el, value);
        inputEvents(el, value);
      }
      if (opts.keyup) fire(el, "keyup", { key: value.slice(-1) || "a" }, "KeyboardEvent");
      if (!opts.keepOpen) {
        fire(el, "change", {});
        blurEl(el);
      }
    }
    function key(el, keyName) {
      var codes = { Enter: 13, Escape: 27, ArrowDown: 40, ArrowUp: 38, Tab: 9, Backspace: 8 };
      var init = { key: keyName, code: keyName, keyCode: codes[keyName] || 0, which: codes[keyName] || 0 };
      fire(el, "keydown", init, "KeyboardEvent");
      if (keyName === "Enter") fire(el, "keypress", init, "KeyboardEvent");
      fire(el, "keyup", init, "KeyboardEvent");
    }
    async function waitFor(fn, timeoutMs, intervalMs) {
      var end = Date.now() + (timeoutMs || 3000);
      var v;
      do {
        try { v = fn(); } catch (e) { v = null; }
        if (v) return v;
        await sleep(intervalMs || 80);
      } while (Date.now() < end);
      try { v = fn(); } catch (e) { v = null; }
      return v || null;
    }

    // ------------------------------------------------------------ option matching
    function boolWord(v) {
      if (v === true) return "Yes";
      if (v === false) return "No";
      return v;
    }
    // options: [{label, value, el?}] ; returns {option} or {error: "none"|"ambiguous"}
    function matchOption(options, value) {
      value = boolWord(value);
      if (value == null) return { error: "none" };
      var v = String(value);
      var lv = loose(v);
      var i;
      for (i = 0; i < options.length; i++) if (options[i].label === v) return { option: options[i] };
      for (i = 0; i < options.length; i++) if (options[i].value != null && String(options[i].value) === v && v !== "") return { option: options[i] };
      var eq = options.filter(function (o) { return loose(o.label) === lv || (o.value != null && loose(o.value) === lv && lv !== ""); });
      if (eq.length === 1) return { option: eq[0] };
      if (eq.length > 1) return { option: eq[0] }; // identical labels: first
      if (!lv) return { error: "none" };
      var pre = options.filter(function (o) { return loose(o.label).indexOf(lv) === 0; });
      if (pre.length === 1) return { option: pre[0] };
      if (pre.length > 1) {
        // prefer whole-word prefix ("Yes" vs "Yes, I have...") only when unique
        var word = pre.filter(function (o) { return new RegExp("^" + escapeRe(lv) + "\\b").test(loose(o.label)); });
        if (word.length === 1) return { option: word[0] };
        return { error: "ambiguous", candidates: pre };
      }
      var con = options.filter(function (o) { return loose(o.label).indexOf(lv) >= 0; });
      if (con.length === 1) return { option: con[0] };
      if (con.length > 1) return { error: "ambiguous", candidates: con };
      return { error: "none" };
    }
    function escapeRe(s) {
      return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }
    var PLACEHOLDER_OPTION_RE = /^(no options?|no results?( found)?|no matches( found)?|loading\.*|searching\.*|type to search|start typing.*|please enter \d+ or more characters|begin typing.*)$/i;

    function currentOptions(d) {
      var el = d.el;
      var lists = [];
      var ids = (attr(el, "aria-controls") + " " + attr(el, "aria-owns")).split(/\s+/).filter(Boolean);
      var wrap = el.closest ? el.closest("[role=combobox]") : null;
      if (wrap && wrap !== el) ids = ids.concat((attr(wrap, "aria-controls") + " " + attr(wrap, "aria-owns")).split(/\s+/).filter(Boolean));
      ids.forEach(function (id) { var l = byIdIn(el, id); if (l) lists.push(l); });
      var opts = [];
      function take(nodes) {
        [].forEach.call(nodes, function (o) {
          if (opts.some(function (x) { return x.el === o; })) return;
          if (!visible(o)) return;
          var label = clean(attr(o, "data-automation-label") || elementText(o));
          if (!label || PLACEHOLDER_OPTION_RE.test(label)) return;
          if (attr(o, "aria-disabled") === "true") return;
          opts.push({ label: label, value: attr(o, "data-value") || attr(o, "value") || label, el: o });
        });
      }
      lists.forEach(function (l) { take(l.querySelectorAll("[role=option]")); if (attr(l, "role") === "option") take([l]); });
      if (!opts.length) {
        var r = d.widget === "listbox-button" ? null : comboRoot(el);
        if (r) take(r.querySelectorAll("[role=option]"));
      }
      if (!opts.length) {
        var boxes = queryAll("[role=listbox]").filter(function (b) { return visible(b) && tag(b) !== "SELECT" && b !== el; });
        boxes.forEach(function (b) { take(b.querySelectorAll("[role=option]")); });
      }
      if (!opts.length && d.widget !== "listbox-button") {
        var r2 = comboRoot(el);
        if (r2) take([].filter.call(r2.querySelectorAll("[class*=option], [class*=Option], li"), function (o) { return !o.querySelector("input") && !o.querySelector("[class*=option]"); }));
      }
      if (!opts.length) {
        take(queryAll("[role=option]").filter(function (o) { return !o.closest("select"); }));
      }
      return opts;
    }

    // ------------------------------------------------------------ fill
    function resolveDesc(id) {
      var d = lastFields.get(String(id));
      if (d && d.el && d.el.isConnected) return d;
      // re-scan silently and retry
      scan({});
      d = lastFields.get(String(id));
      if (d && d.el && d.el.isConnected) return d;
      return null;
    }
    function isoParts(v) {
      var m = String(v).trim().match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?/);
      if (!m) return null;
      return { y: m[1], m: ("0" + m[2]).slice(-2), d: m[3] ? ("0" + m[3]).slice(-2) : null };
    }
    function formatDate(v, fmt) {
      var p = isoParts(v);
      if (!p) return String(v);
      switch (fmt) {
        case "MM/DD/YYYY": return p.m + "/" + (p.d || "01") + "/" + p.y;
        case "DD/MM/YYYY": return (p.d || "01") + "/" + p.m + "/" + p.y;
        case "MM-DD-YYYY": return p.m + "-" + (p.d || "01") + "-" + p.y;
        case "DD.MM.YYYY": return (p.d || "01") + "." + p.m + "." + p.y;
        case "MM/YYYY": return p.m + "/" + p.y;
        case "MM/YY": return p.m + "/" + p.y.slice(2);
        case "YYYY-MM": return p.y + "-" + p.m;
        default: return p.y + "-" + p.m + (p.d ? "-" + p.d : "");
      }
    }
    function digits(s) { return String(s || "").replace(/\D+/g, ""); }

    function optionsOut(list) {
      return (list || []).slice(0, 200).map(function (o) { return { label: o.label, value: o.value != null ? String(o.value) : o.label, selected: !!o.selected }; });
    }

    async function fillText(d, value) {
      var el = d.el;
      var v = value == null ? "" : String(value);
      var warning = null;
      var max = el.maxLength > 0 ? el.maxLength : null;
      if (d.type === "date" || d.type === "month") {
        if (tag(el) === "INPUT" && (el.type === "date" || el.type === "month")) {
          var p = isoParts(v);
          if (p) v = el.type === "month" ? p.y + "-" + p.m : p.y + "-" + p.m + "-" + (p.d || "01");
        } else if (d.dateFormat) {
          v = formatDate(v, d.dateFormat);
        }
      }
      if (max && v.length > max) { v = v.slice(0, max); warning = "truncated to maxlength " + max; }
      if (d.type === "number" && v !== "" && isNaN(Number(v))) {
        var num = String(v).match(/-?\d+(\.\d+)?/);
        if (num) v = num[0];
      }
      typeText(el, v);
      await sleep(0);
      var actual = d.widget === "contenteditable" ? clean(el.textContent) : String(el.value);
      var ok = actual === v || (d.widget === "contenteditable" && clean(actual) === clean(v));
      if (!ok && d.type === "tel" && digits(actual) && digits(actual) === digits(v)) ok = true;
      if (!ok && d.type === "tel" && digits(actual).length >= 7 && digits(v).slice(-digits(actual).length) === digits(actual)) ok = true;
      if (!ok && d.type === "number" && Number(actual) === Number(v)) ok = true;
      if (!ok && clean(actual) === clean(v)) ok = true;
      var res = { ok: ok, actual: d.type === "password" ? undefined : actual };
      if (warning) res.warning = warning;
      if (!ok) res.error = { code: "VERIFY_FAILED", message: "value did not stick (page reformatted or rejected it)" };
      return res;
    }

    async function fillSelect(d, value) {
      var el = d.el;
      var opts = [].map.call(el.options, function (o, i) { return { label: clean(o.label || o.textContent), value: o.value, el: o, index: i, selected: o.selected }; }).filter(function (o) { return !o.el.disabled; });
      if (el.multiple && Array.isArray(value)) {
        var picks = [];
        for (var i = 0; i < value.length; i++) {
          var m = matchOption(opts, value[i]);
          if (!m.option) return { ok: false, error: { code: "NO_OPTION_MATCH", message: "no option matches " + JSON.stringify(value[i]) }, options: optionsOut(opts) };
          picks.push(m.option);
        }
        focusEl(el);
        opts.forEach(function (o) { o.el.selected = picks.indexOf(o) >= 0; });
        fire(el, "input", {});
        fire(el, "change", {});
        blurEl(el);
        var got = [].filter.call(el.options, function (o) { return o.selected; }).map(function (o) { return clean(o.label || o.textContent); });
        return { ok: got.length === picks.length, actual: got };
      }
      var match = matchOption(opts.filter(function (o, i) { return !(i === 0 && o.value === "" && /^(|-+.*|select.*|please select.*|choose.*)$/i.test(o.label)); }), Array.isArray(value) ? value[0] : value);
      if (!match.option) {
        return { ok: false, error: { code: "NO_OPTION_MATCH", message: (match.error === "ambiguous" ? "ambiguous option for " : "no option matches ") + JSON.stringify(value) }, options: optionsOut(d.options || opts) };
      }
      focusEl(el);
      setNativeValue(el, match.option.value);
      el.selectedIndex = match.option.index;
      fire(el, "input", {});
      fire(el, "change", {});
      blurEl(el);
      await sleep(0);
      var cur = el.options[el.selectedIndex];
      var actual = cur ? clean(cur.label || cur.textContent) : null;
      var ok = el.selectedIndex === match.option.index;
      return ok ? { ok: true, actual: actual } : { ok: false, actual: actual, error: { code: "VERIFY_FAILED", message: "select value did not stick" } };
    }

    async function clickChoice(member, isChecked) {
      scrollTo(member);
      mouseSequence(member);
      await sleep(0);
      if (isChecked()) return true;
      var labels = nativeLabels(member);
      if (labels.length && labels[0] !== member.closest("label")) {
        labels[0].click();
        await sleep(0);
        if (isChecked()) return true;
      }
      return isChecked();
    }

    async function fillRadio(d, value) {
      var opts = (readOptionsNative(d) || []).map(function (o, i) { return Object.assign({ el: d.members[i] }, o); });
      if (d.widget === "custom" && d.options) opts = d.options.map(function (o, i) { return Object.assign({ el: (d.optionEls || d.members)[i] }, o); });
      var m = matchOption(opts, Array.isArray(value) ? value[0] : value);
      if (!m.option) return { ok: false, error: { code: "NO_OPTION_MATCH", message: (m.error === "ambiguous" ? "ambiguous option for " : "no option matches ") + JSON.stringify(value) }, options: optionsOut(opts) };
      var target = m.option.el;
      if (d.widget === "radio-group") {
        if (!target.checked) {
          var ok = await clickChoice(target, function () { return target.checked; });
          if (!ok) {
            target.checked = true;
            fire(target, "input", {});
            fire(target, "change", {});
          }
        }
        return target.checked ? { ok: true, actual: m.option.label } : { ok: false, actual: readValue(d), error: { code: "VERIFY_FAILED", message: "radio not checked" } };
      }
      if (d.widget === "aria-radio-group") {
        if (attr(target, "aria-checked") !== "true") await clickChoice(target, function () { return attr(target, "aria-checked") === "true"; });
        var okA = attr(target, "aria-checked") === "true";
        return okA ? { ok: true, actual: m.option.label } : { ok: false, error: { code: "VERIFY_FAILED", message: "option not selected" } };
      }
      // button group (yes/no toggles)
      var beforeState = stateOf(target);
      if (!buttonActive(target)) {
        scrollTo(target);
        mouseSequence(target);
        await sleep(30);
      }
      if (buttonActive(target) || stateOf(target) !== beforeState) return { ok: true, actual: m.option.label };
      var t2 = findById(idOf(target));
      if (t2 && buttonActive(t2)) return { ok: true, actual: m.option.label };
      return { ok: false, actual: null, error: { code: "VERIFY_FAILED", message: "toggle button did not become active" } };
    }
    function stateOf(el) {
      return classOf(el) + "|" + attr(el, "aria-pressed") + "|" + attr(el, "aria-checked") + "|" + attr(el, "aria-selected");
    }
    function truthy(v) {
      if (typeof v === "boolean") return v;
      var s = norm(v);
      return s === "true" || s === "yes" || s === "1" || s === "on" || s === "checked" || s === "y";
    }
    async function fillCheckbox(d, value) {
      var el = d.el;
      var want = truthy(value);
      var isAria = d.widget === "aria-checkbox";
      var checked = function () { return isAria ? attr(el, "aria-checked") === "true" : !!el.checked; };
      if (checked() !== want) {
        await clickChoice(el, function () { return checked() === want; });
        if (checked() !== want && !isAria) {
          el.checked = want;
          fire(el, "input", {});
          fire(el, "change", {});
        }
      }
      return checked() === want ? { ok: true, actual: want } : { ok: false, actual: checked(), error: { code: "VERIFY_FAILED", message: "checkbox state did not change" } };
    }
    async function fillCheckboxGroup(d, value) {
      var wanted = Array.isArray(value) ? value : (value == null || value === "" ? [] : [value]);
      var opts = readOptionsNative(d).map(function (o, i) { return Object.assign({ el: d.members[i] }, o); });
      var targets = [];
      for (var i = 0; i < wanted.length; i++) {
        var m = matchOption(opts, wanted[i]);
        if (!m.option) return { ok: false, error: { code: "NO_OPTION_MATCH", message: "no option matches " + JSON.stringify(wanted[i]) }, options: optionsOut(opts) };
        targets.push(m.option.el);
      }
      for (var j = 0; j < d.members.length; j++) {
        var cb = d.members[j];
        var want = targets.indexOf(cb) >= 0;
        if (!!cb.checked !== want) {
          await clickChoice(cb, function () { return !!cb.checked === want; });
          if (!!cb.checked !== want) { cb.checked = want; fire(cb, "change", {}); }
        }
      }
      var actual = readValue(d);
      var ok = d.members.every(function (cb) { return !!cb.checked === (targets.indexOf(cb) >= 0); });
      return ok ? { ok: true, actual: actual } : { ok: false, actual: actual, error: { code: "VERIFY_FAILED", message: "checkbox group state mismatch" } };
    }

    function closeCombo(d) {
      try { key(d.el, "Escape"); } catch (e) { /* ignore */ }
    }
    function displayMatches(display, label) {
      var a = loose(display), b = loose(label);
      if (!a) return false;
      return a === b || a.indexOf(b) >= 0 || b.indexOf(a) >= 0;
    }
    async function openCombo(d) {
      var el = d.el;
      scrollTo(el);
      if (d.widget === "listbox-button") {
        mouseSequence(el);
        return;
      }
      var ctl = el.closest("[class*='__control'], [class*='-control'], [class*='Select-control']");
      focusEl(el);
      if (ctl) mouseSequence(ctl, { focus: false, noClick: false });
      else mouseSequence(el, { focus: true });
    }
    async function fillCombobox(d, value, timeoutMs) {
      var el = d.el;
      var v = boolWord(Array.isArray(value) ? value[0] : value);
      v = v == null ? "" : String(v);
      timeoutMs = timeoutMs || 3500;
      // "Already set" must look at the widget's real selection (readValue), never at typed search text.
      var current = typeof d.readValue === "function" ? d.readValue() : comboDisplay(d);
      if (current && displayMatches(current, v) && loose(current) === loose(v)) return { ok: true, actual: current };
      await openCombo(d);
      var queries = [v];
      if (v.indexOf(",") > 0) queries.push(v.split(",")[0].trim());
      if (d.widget !== "listbox-button" && v.split(" ").length > 3) queries.push(v.split(" ").slice(0, 2).join(" "));
      var lastOpts = [];
      for (var qi = 0; qi < queries.length; qi++) {
        var q = queries[qi];
        if (d.widget !== "listbox-button" && tag(el) === "INPUT" && !el.readOnly) {
          focusEl(el);
          setNativeValue(el, "");
          inputEvents(el, "");
          setNativeValue(el, q);
          inputEvents(el, q);
          fire(el, "keyup", { key: q.slice(-1) }, "KeyboardEvent");
        }
        // wait for options to appear and settle
        var stableCount = -1, stableSince = 0;
        var opts = await waitFor(function () {
          var o = currentOptions(d);
          if (!o.length) return null;
          if (o.length !== stableCount) { stableCount = o.length; stableSince = Date.now(); return null; }
          return Date.now() - stableSince >= 120 ? o : null;
        }, qi === 0 ? timeoutMs : 1500, 60);
        if (!opts) opts = currentOptions(d);
        lastOpts = opts && opts.length ? opts : lastOpts;
        if (!opts || !opts.length) continue;
        var m = matchOption(opts, v);
        if (!m.option && q !== v) m = matchOption(opts, q);
        if (!m.option) {
          if (m.error === "ambiguous" && qi === queries.length - 1) break;
          continue;
        }
        var target = m.option.el;
        scrollTo(target);
        mouseSequence(target, { focus: false });
        await sleep(60);
        var shown = await waitFor(function () {
          var disp = typeof d.readValue === "function" ? d.readValue() : comboDisplay(d);
          return displayMatches(disp, m.option.label) ? disp || true : null;
        }, 1200, 60);
        var display = typeof d.readValue === "function" ? d.readValue() : comboDisplay(d);
        if (!shown && d.widget !== "listbox-button" && tag(el) === "INPUT") {
          // keyboard fallback: highlight + Enter
          focusEl(el);
          key(el, "ArrowDown");
          key(el, "Enter");
          await sleep(80);
          display = typeof d.readValue === "function" ? d.readValue() : comboDisplay(d);
          shown = displayMatches(display, m.option.label);
        }
        if (currentOptions(d).length && d.widget !== "listbox-button") closeCombo(d);
        if (shown) return { ok: true, actual: display || m.option.label };
        return { ok: false, actual: display || null, error: { code: "VERIFY_FAILED", message: "combobox shows '" + (display || "") + "' after choosing '" + m.option.label + "'" } };
      }
      // nothing matched: clear typed text and close
      if (d.widget !== "listbox-button" && tag(el) === "INPUT") {
        setNativeValue(el, "");
        inputEvents(el, "");
        if (!lastOpts.length) {
          // A search with no hits shows nothing; the unfiltered list lets the backend choose.
          var all = await waitFor(function () { var o = currentOptions(d); return o.length ? o : null; }, 1500, 60);
          if (all && all.length) lastOpts = all;
        }
      }
      closeCombo(d);
      if (d.widget === "listbox-button" && currentOptions(d).length) mouseSequence(el);
      return {
        ok: false,
        error: { code: "NO_OPTION_MATCH", message: lastOpts.length ? "no unique option matches " + JSON.stringify(v) : "no options appeared for " + JSON.stringify(v) },
        options: optionsOut(lastOpts)
      };
    }

    async function fillOne(item) {
      var id = item && item.field_id != null ? String(item.field_id) : "";
      if (!id) return { field_id: id, ok: false, error: { code: "BAD_REQUEST", message: "field_id missing" } };
      if (isCaptchaFrame()) return { field_id: id, ok: false, error: { code: "UNSUPPORTED", message: "refusing to interact with a captcha frame" } };
      var d = resolveDesc(id);
      if (!d) return { field_id: id, ok: false, error: { code: "ELEMENT_NOT_FOUND", message: "no field with id " + id + " (re-scan)" } };
      var f = d.field || {};
      if (f.disabled) return { field_id: id, ok: false, error: { code: "DISABLED", message: "field is disabled or read-only" } };
      var value = item.value;
      var res;
      try {
        if (d.adapter && typeof d.adapter.fillCustom === "function") {
          var custom = d.adapter.fillCustom(d, d.el, value, ctx);
          if (custom) res = await custom;
        }
        if (!res) {
          switch (d.type) {
            case "file":
              res = { ok: false, error: { code: "BAD_REQUEST", message: "use the upload action for file fields" } };
              break;
            case "select": res = await fillSelect(d, value); break;
            case "radio": res = await fillRadio(d, value); break;
            case "checkbox": res = await fillCheckbox(d, value); break;
            case "checkbox_group": res = await fillCheckboxGroup(d, value); break;
            case "combobox": res = await fillCombobox(d, value); break;
            default: res = await fillText(d, value);
          }
        }
      } catch (e) {
        res = { ok: false, error: { code: e.code || "INTERNAL", message: String(e && e.message || e) } };
      }
      res = Object.assign({ field_id: id }, res);
      if (d.type === "password") delete res.actual;
      if (res.ok) delete res.error;
      return res;
    }

    async function fill(items) {
      if (!Array.isArray(items)) throw fail("BAD_REQUEST", "items must be an array");
      if (!lastFields.size) scan({});
      var results = [];
      for (var i = 0; i < items.length; i++) {
        results.push(await fillOne(items[i]));
      }
      return { results: results };
    }

    // ------------------------------------------------------------ upload
    function b64ToBytes(b64) {
      var bin;
      if (typeof win.atob === "function") bin = win.atob(String(b64).replace(/\s+/g, ""));
      else if (typeof atob === "function") bin = atob(String(b64)); // eslint-disable-line no-undef
      else throw fail("UNSUPPORTED", "no base64 decoder");
      var bytes = new win.Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return bytes;
    }
    function makeFileList(file) {
      if (typeof win.DataTransfer === "function") {
        try {
          var dt = new win.DataTransfer();
          dt.items.add(file);
          return { dt: dt, files: dt.files };
        } catch (e) { /* fall through */ }
      }
      var list = { 0: file, length: 1, item: function (i) { return i === 0 ? file : null; } };
      if (typeof Symbol !== "undefined") list[Symbol.iterator] = function* () { yield file; };
      return { dt: { files: list, items: [], types: ["Files"], getData: function () { return ""; } }, files: list };
    }
    function assignFiles(input, fl) {
      try {
        input.files = fl.files;
      } catch (e) { /* jsdom: read-only */ }
      if (!input.files || !input.files.length || input.files[0] !== fl.files[0]) {
        try {
          Object.defineProperty(input, "files", { value: fl.files, configurable: true });
        } catch (e2) { /* ignore */ }
      }
    }
    async function upload(fieldId, file) {
      if (isCaptchaFrame()) throw fail("UNSUPPORTED", "refusing to interact with a captcha frame");
      if (!file || !file.name || file.base64 == null) throw fail("BAD_REQUEST", "file {name, mime, base64} required");
      var el = findById(fieldId);
      if (!el) throw fail("ELEMENT_NOT_FOUND", "no element with id " + fieldId);
      var input = el;
      if (!(tag(el) === "INPUT" && String(el.type).toLowerCase() === "file")) {
        input = el.querySelector && el.querySelector("input[type=file]");
        if (!input) {
          var c = el.closest && el.closest("[class*=upload], [class*=drop], [class*=file], [data-automation-id*=file], form");
          input = c ? c.querySelector("input[type=file]") : null;
        }
      }
      var bytes = b64ToBytes(file.base64);
      var f = new win.File([bytes], file.name, { type: file.mime || "application/octet-stream", lastModified: Date.now() });
      var fl = makeFileList(f);
      var dropTarget = null;
      if (input && !input.disabled) {
        assignFiles(input, fl);
        fire(input, "input", { cancelable: false });
        fire(input, "change", { cancelable: false });
        await sleep(50);
      }
      var ok = !!(input && input.files && input.files.length && input.files[0].name === file.name);
      if (!ok) {
        dropTarget = (input && input.closest && input.closest("[class*=drop], [class*=Drop], [class*=upload], [data-automation-id*=drop]")) ||
          (el !== input ? el : null);
        if (dropTarget) {
          ["dragenter", "dragover", "drop"].forEach(function (t) {
            var ev;
            try {
              ev = new win.DragEvent(t, { bubbles: true, cancelable: true, dataTransfer: fl.dt });
            } catch (e) {
              ev = new win.Event(t, { bubbles: true, cancelable: true });
            }
            if (!ev.dataTransfer) {
              try { Object.defineProperty(ev, "dataTransfer", { value: fl.dt }); } catch (e2) { /* ignore */ }
            }
            dropTarget.dispatchEvent(ev);
          });
          await sleep(100);
          ok = !!(input && input.files && input.files.length && input.files[0].name === file.name) || textOf(dropTarget, null, { max: 2000 }).indexOf(file.name) >= 0 || visibleText(50000).indexOf(file.name) >= 0;
        }
      }
      var names = input && input.files ? [].map.call(input.files, function (x) { return x.name; }) : [];
      if (!ok) throw fail("VERIFY_FAILED", "file was not attached", { details: { files: names } });
      return { ok: true, files: names.length ? names : [file.name] };
    }

    // ------------------------------------------------------------ click
    function hrefFor(el, prevented) {
      var a = el.closest ? el.closest("a[href]") : null;
      if (!a || prevented) return null;
      var raw = attr(a, "href");
      if (!raw || raw.charAt(0) === "#" || /^javascript:/i.test(raw)) return null;
      return a.href || raw;
    }
    async function click(id) {
      if (isCaptchaFrame()) throw fail("UNSUPPORTED", "refusing to interact with a captcha frame");
      var el = findById(id);
      if (!el) throw fail("ELEMENT_NOT_FOUND", "no element with id " + id);
      if (inCaptcha(el)) throw fail("UNSUPPORTED", "refusing to click inside a captcha widget");
      if (el.disabled || attr(el, "aria-disabled") === "true") throw fail("DISABLED", "element is disabled");
      var a = el.closest ? el.closest("a[href]") : null;
      if (a && attr(a, "target") === "_blank" && /^https?:/i.test(a.href || "")) a.setAttribute("target", "_self");
      scrollTo(el);
      var r = mouseSequence(el);
      var out = { ok: true };
      var href = hrefFor(el, r.prevented);
      if (href) out.href = href;
      await sleep(0);
      return out;
    }
    var CLICKABLE = "button, a[href], input[type=submit], input[type=button], [role=button], [role=link], [role=tab], [role=menuitem], [role=option], label, summary, [onclick], [data-automation-id]";
    async function clickText(text, exact) {
      if (isCaptchaFrame()) throw fail("UNSUPPORTED", "refusing to interact with a captcha frame");
      var want = loose(text);
      if (!want) throw fail("BAD_REQUEST", "text required");
      styleCache = new Map(); hiddenCache = new Map();
      try {
        var cands = queryAll(CLICKABLE).filter(function (el) { return !inCaptcha(el) && visible(el); });
        var scored = [];
        cands.forEach(function (el) {
          var t = loose(elementText(el));
          if (!t) return;
          var score = -1;
          if (t === want) score = 3;
          else if (!exact && t.indexOf(want) === 0) score = 2;
          else if (!exact && t.indexOf(want) >= 0) score = 1;
          if (score < 0) return;
          // prefer real buttons/links over generic containers
          var real = /^(BUTTON|A|INPUT)$/.test(tag(el)) || /button|link|tab|option|menuitem/.test(attr(el, "role")) ? 0.5 : 0;
          scored.push({ el: el, score: score + real - Math.min(t.length, 200) / 1000 });
        });
        if (!scored.length) throw fail("ELEMENT_NOT_FOUND", "no visible clickable element matches " + JSON.stringify(text));
        scored.sort(function (a, b) { return b.score - a.score; });
        var best = scored[0].el;
        // deepest descendants matching identical text are often inner spans: use outermost clickable
        var id = idOf(best);
        var res = await click(id);
        res.element_id = id;
        return res;
      } finally {
        styleCache = null; hiddenCache = null;
      }
    }
    async function clickPoint(x, y) {
      if (isCaptchaFrame()) throw fail("UNSUPPORTED", "refusing to interact with a captcha frame");
      if (typeof document.elementFromPoint !== "function") throw fail("UNSUPPORTED", "elementFromPoint is unavailable");
      var el = document.elementFromPoint(Number(x), Number(y));
      if (!el) throw fail("ELEMENT_NOT_FOUND", "nothing at point " + x + "," + y);
      while (el && shadowOf(el) && shadowOf(el).elementFromPoint) {
        var inner = shadowOf(el).elementFromPoint(Number(x), Number(y));
        if (!inner || inner === el) break;
        el = inner;
      }
      if (tag(el) === "IFRAME" && (CAPTCHA_URL_RE.test(attr(el, "src")) || /captcha/i.test(attr(el, "title")))) throw fail("UNSUPPORTED", "refusing to click a captcha frame");
      if (inCaptcha(el)) throw fail("UNSUPPORTED", "refusing to click inside a captcha widget");
      scrollTo(el);
      var r = mouseSequence(el);
      var out = { ok: true, tag: tag(el).toLowerCase(), text: elementText(el).slice(0, 120) };
      var href = hrefFor(el, r.prevented);
      if (href) out.href = href;
      return out;
    }

    function extractText(max) {
      max = Math.max(100, Math.min(Number(max) || 4000, 200000));
      styleCache = new Map(); hiddenCache = new Map();
      try {
        return { text: visibleText(max), url: String(win.location.href), title: clean(document.title) };
      } finally {
        styleCache = null; hiddenCache = null;
      }
    }

    function waitQuiet(quietMs, timeoutMs) {
      quietMs = Math.max(50, Number(quietMs) || 600);
      timeoutMs = Math.max(quietMs, Math.min(Number(timeoutMs) || 10000, 120000));
      var start = Date.now();
      return new Promise(function (resolve) {
        var last = Date.now();
        var done = false;
        var obs = null;
        try {
          obs = new win.MutationObserver(function (records) {
            for (var i = 0; i < records.length; i++) {
              var r = records[i];
              if (r.type === "attributes" && /^data-ap-/.test(r.attributeName || "")) continue;
              last = Date.now();
              return;
            }
          });
          obs.observe(document.documentElement || document, { subtree: true, childList: true, attributes: true, characterData: true });
        } catch (e) { obs = null; }
        function finish(quiet) {
          if (done) return;
          done = true;
          clearInterval(timer);
          if (obs) obs.disconnect();
          resolve({ quiet: quiet, waited_ms: Date.now() - start, ready_state: document.readyState });
        }
        var timer = setInterval(function () {
          var now = Date.now();
          if (document.readyState === "complete" && now - last >= quietMs) finish(true);
          else if (now - start >= timeoutMs) finish(false);
        }, Math.min(100, quietMs / 2));
      });
    }

    // ------------------------------------------------------------ adapter ctx
    var ctx = {
      document: document,
      window: win,
      isJsdom: isJsdom,
      util: {
        clean: clean, norm: norm, loose: loose, fnv1a: fnv1a, sleep: sleep, fail: fail,
        stripRequired: stripRequired, looksRequired: looksRequired,
        attr: attr, tag: tag, textOf: textOf, elementText: elementText, visible: visible,
        queryAll: queryAll, roots: roots, parentOf: parentOf, byIdIn: byIdIn,
        labelInfo: function (el) { return labelInfo(el, []); }, questionText: questionText,
        nativeLabelText: nativeLabelText, idOf: idOf, findById: findById,
        fire: fire, focus: focusEl, blur: blurEl, mouseSequence: mouseSequence, key: key, scrollTo: scrollTo,
        setNativeValue: setNativeValue, inputEvents: inputEvents, typeText: typeText,
        waitFor: waitFor, matchOption: matchOption, currentOptions: currentOptions, optionsOut: optionsOut,
        fillCombobox: fillCombobox, formatDate: formatDate, isoParts: isoParts, buttonActive: buttonActive
      }
    };

    return {
      scan: scan,
      fill: fill,
      upload: upload,
      click: click,
      clickText: clickText,
      clickPoint: clickPoint,
      extractText: extractText,
      waitQuiet: waitQuiet,
      _ctx: ctx
    };
  }

  return { createEngine: createEngine, fnv1a: fnv1a, version: "1.0.0" };
});
