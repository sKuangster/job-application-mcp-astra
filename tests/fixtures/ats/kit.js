/*
 * Shared helper for the local ApplyPilot ATS fixtures.
 * LOCAL TEST FIXTURE — NOT A REAL APPLICATION.
 *
 * window.FixtureKit:
 *   submit(fixtureName, data, options?) -> Promise<{status, body}>
 *       POSTs {fixture, data, url, ts} as JSON to "/__fixture/submit" with
 *       XMLHttpRequest, records it in window.__fixtureSubmissions, then renders
 *       the confirmation:
 *         options.confirm: function()            -> called to render the page
 *         options.confirm: {target, html}         -> target.innerHTML = html
 *         default                                 -> replaces [data-fixture-root]
 *                                                    (or <main>) with a generic
 *                                                    "Thank you for applying." page
 *   collect(root) -> plain object of name -> value for all named controls under
 *       root (also descends into open shadow roots). Checkboxes sharing a name
 *       become arrays of checked values; a lone checkbox becomes a boolean; file
 *       inputs become arrays of file names; passwords are omitted.
 *   requireFields(root, opts?) -> array of missing controls ([required] or
 *       [aria-required=true]); marks them aria-invalid and shows an inline error
 *       ("This field is required.") unless opts.mark === false.
 *   fieldError(el, message) / clearErrors(root) -> inline error helpers.
 *   banner() -> ensures the visible LOCAL TEST FIXTURE banner exists.
 */
(function () {
  "use strict";
  var BANNER_TEXT = "LOCAL TEST FIXTURE — NOT A REAL APPLICATION";

  function banner() {
    if (!document.body) return;
    if (document.querySelector("[data-fixture-banner]")) return;
    var div = document.createElement("div");
    div.setAttribute("data-fixture-banner", "");
    div.setAttribute("role", "note");
    div.style.cssText = "background:#fff3cd;color:#664d03;border:2px solid #ffcd39;padding:6px 10px;font:bold 14px sans-serif;text-align:center";
    div.textContent = BANNER_TEXT;
    document.body.insertBefore(div, document.body.firstChild);
  }

  function roots(root) {
    var out = [root];
    for (var i = 0; i < out.length; i++) {
      var all = out[i].querySelectorAll("*");
      for (var j = 0; j < all.length; j++) if (all[j].shadowRoot) out.push(all[j].shadowRoot);
    }
    return out;
  }

  function controls(root) {
    var list = [];
    roots(root || document).forEach(function (r) {
      var found = r.querySelectorAll("input, select, textarea");
      for (var i = 0; i < found.length; i++) list.push(found[i]);
    });
    return list;
  }

  function collect(root) {
    var data = {};
    var els = controls(root || document);
    var checkboxCounts = {};
    els.forEach(function (el) {
      if (el.type === "checkbox" && el.name) checkboxCounts[el.name] = (checkboxCounts[el.name] || 0) + 1;
    });
    els.forEach(function (el) {
      var name = el.name || el.id;
      if (!name || el.disabled) return;
      var type = (el.type || "").toLowerCase();
      if (type === "password" || type === "submit" || type === "button") return;
      if (type === "file") {
        data[name] = Array.prototype.map.call(el.files || [], function (f) { return f.name; });
      } else if (type === "checkbox") {
        if ((checkboxCounts[name] || 0) > 1) {
          if (!Array.isArray(data[name])) data[name] = [];
          if (el.checked) data[name].push(el.value);
        } else {
          data[name] = !!el.checked;
        }
      } else if (type === "radio") {
        if (el.checked) data[name] = el.value;
        else if (!(name in data)) data[name] = null;
      } else if (el.tagName === "SELECT" && el.multiple) {
        data[name] = Array.prototype.filter.call(el.options, function (o) { return o.selected; }).map(function (o) { return o.value; });
      } else {
        data[name] = el.value;
      }
    });
    return data;
  }

  function errorNodeFor(el) {
    var id = (el.id || el.name || "f") + "-fixture-error";
    var host = el.getRootNode && el.getRootNode() !== document ? el.getRootNode() : document;
    return { id: id, node: host.getElementById ? host.getElementById(id) : null, host: host };
  }

  function fieldError(el, message) {
    var info = errorNodeFor(el);
    var node = info.node;
    if (!node) {
      node = document.createElement("div");
      node.id = info.id;
      node.className = "field-error error-message";
      node.style.color = "#b00020";
      var anchor = el;
      if (el.type === "radio" || el.type === "checkbox") anchor = el.closest("fieldset, [data-question], .question, .field") || el;
      if (anchor.parentNode) anchor.parentNode.insertBefore(node, anchor.nextSibling);
    }
    node.textContent = message;
    node.hidden = false;
    el.setAttribute("aria-invalid", "true");
    var described = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
    if (described.indexOf(info.id) < 0) described.push(info.id);
    el.setAttribute("aria-describedby", described.join(" "));
  }

  function clearErrors(root) {
    roots(root || document).forEach(function (r) {
      Array.prototype.forEach.call(r.querySelectorAll(".field-error"), function (n) { n.remove(); });
      Array.prototype.forEach.call(r.querySelectorAll("[aria-invalid=true]"), function (n) { n.removeAttribute("aria-invalid"); });
    });
  }

  function isEmpty(el, scope) {
    var type = (el.type || "").toLowerCase();
    if (type === "checkbox") {
      if (el.name) {
        var group = controls(scope).filter(function (x) { return x.type === "checkbox" && x.name === el.name; });
        if (group.length > 1) return !group.some(function (x) { return x.checked; });
      }
      return !el.checked;
    }
    if (type === "radio") {
      return !controls(scope).some(function (x) { return x.type === "radio" && x.name === el.name && x.checked; });
    }
    if (type === "file") return !(el.files && el.files.length);
    return !String(el.value || "").trim();
  }

  function visibleControl(el) {
    for (var n = el; n && n.nodeType === 1; n = n.parentElement || (n.getRootNode && n.getRootNode().host)) {
      if (n.hidden) return false;
      if (n.style && n.style.display === "none") return false;
    }
    return true;
  }

  function requireFields(root, opts) {
    opts = opts || {};
    var scope = root || document;
    var missing = [];
    var seen = {};
    controls(scope).forEach(function (el) {
      if (el.disabled) return;
      if (!(el.required || el.getAttribute("aria-required") === "true")) return;
      if (el.type !== "file" && !visibleControl(el)) return;
      var groupKey = (el.type === "radio" || el.type === "checkbox") && el.name ? el.type + ":" + el.name : null;
      if (groupKey) { if (seen[groupKey]) return; seen[groupKey] = true; }
      if (isEmpty(el, scope)) {
        missing.push(el);
        if (opts.mark !== false) fieldError(el, opts.message || "This field is required.");
      }
    });
    return missing;
  }

  function defaultConfirm() {
    var root = document.querySelector("[data-fixture-root]") || document.querySelector("main") || document.body;
    root.innerHTML = '<div class="confirmation"><h1>Thank you for applying.</h1><p>Your application has been received.</p></div>';
  }

  function submit(fixtureName, data, options) {
    options = options || {};
    var record = { fixture: fixtureName, data: data || {}, url: String(location.href), ts: Date.now() };
    window.__fixtureSubmissions = window.__fixtureSubmissions || [];
    window.__fixtureSubmissions.push(record);
    return new Promise(function (resolve) {
      var done = function (status, body) {
        try {
          if (typeof options.confirm === "function") options.confirm();
          else if (options.confirm && options.confirm.target) options.confirm.target.innerHTML = options.confirm.html;
          else if (options.confirm !== false) defaultConfirm();
        } catch (e) { /* rendering problems must not hide the submission */ }
        resolve({ status: status, body: body });
      };
      var xhr;
      try {
        xhr = new XMLHttpRequest();
        xhr.open("POST", "/__fixture/submit", true);
        xhr.setRequestHeader("Content-Type", "application/json");
      } catch (e) { done(0, null); return; }
      xhr.onload = function () { done(xhr.status, xhr.responseText); };
      xhr.onerror = function () { done(0, null); };
      xhr.ontimeout = function () { done(0, null); };
      xhr.timeout = 5000;
      try { xhr.send(JSON.stringify(record)); } catch (e) { done(0, null); }
    });
  }

  window.FixtureKit = Object.assign(window.FixtureKit || {}, {
    submit: submit,
    collect: collect,
    requireFields: requireFields,
    fieldError: fieldError,
    clearErrors: clearErrors,
    banner: banner,
    BANNER_TEXT: BANNER_TEXT
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", banner);
  else banner();
})();
