/*
 * Shared DOM helpers for popup / options / dashboard. Global `APUI`.
 * All DOM is built with createElement/textContent — never innerHTML with
 * backend data (answers and questions originate from arbitrary websites).
 */
(function (root) {
  'use strict';
  var doc = root.document;

  var STATES = ['DISCOVERED', 'QUEUED', 'CHECKING_ELIGIBILITY', 'INELIGIBLE', 'READY', 'OPENING',
    'APPLYING', 'ACCOUNT_REQUIRED', 'VERIFYING_EMAIL', 'FILLING', 'ANSWERING', 'WAITING_FOR_CAPTCHA', 'NEEDS_USER',
    'VALIDATING', 'READY_TO_SUBMIT', 'SUBMITTING', 'SUBMITTED', 'SUBMISSION_UNCONFIRMED',
    'FAILED', 'SKIPPED'];

  var METHODS = ['deterministic', 'fact', 'preference', 'eeo', 'template', 'reused', 'generated',
    'user', 'default', 'research'];

  var ATS_LIST = ['greenhouse', 'lever', 'ashby', 'workday', 'icims', 'smartrecruiters', 'simplify', 'generic'];

  function stateGroup(state) {
    switch (String(state || '').toUpperCase()) {
      case 'SUBMITTED': return 'success';
      case 'WAITING_FOR_CAPTCHA': case 'NEEDS_USER': case 'SUBMISSION_UNCONFIRMED': case 'READY_TO_SUBMIT': return 'warn';
      case 'FAILED': return 'danger';
      case 'SKIPPED': case 'INELIGIBLE': return 'muted';
      case '': return 'muted';
      default: return 'active';
    }
  }

  /**
   * h('div', {class: 'x', text: 'hi', on: {click: fn}, attrs: {...}, dataset: {...}}, child, ...)
   * Children: Node | string | number | array | null.
   */
  function h(tag, props) {
    var el = doc.createElement(tag);
    props = props || {};
    Object.keys(props).forEach(function (k) {
      var v = props[k];
      if (v === undefined || v === null || v === false) return;
      if (k === 'class' || k === 'className') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k === 'on') Object.keys(v).forEach(function (ev) { el.addEventListener(ev, v[ev]); });
      else if (k === 'dataset') Object.keys(v).forEach(function (d) { el.dataset[d] = String(v[d]); });
      else if (k === 'attrs') Object.keys(v).forEach(function (a) { if (v[a] !== null && v[a] !== undefined && v[a] !== false) el.setAttribute(a, String(v[a])); });
      else if (k === 'style') el.style.cssText = v;
      else if (k in el && typeof v !== 'string') el[k] = v;
      else el.setAttribute(k, String(v));
    });
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  function append(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    if (typeof child === 'string' || typeof child === 'number') { el.appendChild(doc.createTextNode(String(child))); return; }
    el.appendChild(child);
  }

  function clear(el) { while (el && el.firstChild) el.removeChild(el.firstChild); return el; }

  function replace(el) {
    clear(el);
    for (var i = 1; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }

  /** Re-render `el` only when `data` (JSON-compared) changed. Returns true if rendered. */
  function renderIfChanged(el, data, renderFn) {
    var key;
    try { key = JSON.stringify(data); } catch (e) { key = String(Math.random()); }
    if (el.__apKey === key) return false;
    el.__apKey = key;
    renderFn(el, data);
    return true;
  }

  function badge(state, extraClass) {
    var s = state ? String(state) : '—';
    return h('span', { class: 'badge badge-' + stateGroup(state) + (extraClass ? ' ' + extraClass : ''), text: s.replace(/_/g, ' ') });
  }

  function methodBadge(method) {
    var m = String(method || 'unknown').toLowerCase();
    return h('span', { class: 'mbadge m-' + m.replace(/[^a-z_]/g, ''), text: m });
  }

  /* ------------------------------------------------------------------ time */

  function toMs(ts) {
    if (ts === null || ts === undefined || ts === '') return null;
    if (typeof ts === 'number') return ts < 1e12 ? ts * 1000 : ts;
    var n = Number(ts);
    if (!isNaN(n) && String(ts).trim() !== '') return n < 1e12 ? n * 1000 : n;
    var p = Date.parse(ts);
    return isNaN(p) ? null : p;
  }

  function pad(n) { return n < 10 ? '0' + n : String(n); }

  function fmtTime(ts) {
    var ms = toMs(ts);
    if (ms === null) return '—';
    var d = new Date(ms);
    var now = new Date();
    var time = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    if (d.toDateString() === now.toDateString()) return time;
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + time;
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
    seconds = Math.max(0, seconds);
    if (seconds < 60) return (seconds < 10 ? Math.round(seconds * 10) / 10 : Math.round(seconds)) + 's';
    var m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
    if (m < 60) return m + 'm ' + pad(s) + 's';
    var hh = Math.floor(m / 60);
    return hh + 'h ' + pad(m % 60) + 'm';
  }

  function elapsedSince(ts) {
    var ms = toMs(ts);
    if (ms === null) return '—';
    return fmtDuration((Date.now() - ms) / 1000);
  }

  function fmtAgo(ts) {
    var ms = toMs(ts);
    if (ms === null) return '—';
    return fmtDuration((Date.now() - ms) / 1000) + ' ago';
  }

  /* --------------------------------------------------------------- display */

  function toText(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    if (Array.isArray(v) && v.every(function (x) { return typeof x !== 'object' || x === null; })) return v.join(', ');
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }

  function pretty(v) {
    try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
  }

  function humanize(key) {
    return String(key).replace(/[_.]+/g, ' ').replace(/\s+/g, ' ').trim()
      .replace(/^./, function (c) { return c.toUpperCase(); });
  }

  function copyText(text, btn) {
    var done = function () {
      if (!btn) return;
      var old = btn.textContent;
      btn.textContent = 'Copied';
      setTimeout(function () { btn.textContent = old; }, 1200);
    };
    try {
      if (root.navigator && root.navigator.clipboard && root.navigator.clipboard.writeText) {
        return root.navigator.clipboard.writeText(String(text)).then(done, fallback);
      }
    } catch (e) { /* fallthrough */ }
    fallback();
    function fallback() {
      var ta = h('textarea', { attrs: { readonly: '' } });
      ta.value = String(text);
      ta.style.position = 'fixed'; ta.style.opacity = '0';
      doc.body.appendChild(ta);
      ta.select();
      try { doc.execCommand('copy'); } catch (e) { /* ignore */ }
      doc.body.removeChild(ta);
      done();
    }
    return Promise.resolve();
  }

  function copyButton(text, label) {
    var b = h('button', { type: 'button', class: 'btn btn-xs', text: label || 'Copy', attrs: { title: 'Copy to clipboard' } });
    b.addEventListener('click', function (ev) { ev.stopPropagation(); copyText(text, b); });
    return b;
  }

  function safeHref(url) {
    try {
      var u = new URL(String(url));
      return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
    } catch (e) { return null; }
  }

  /** URL rendered as text + copy button; clickable only for http(s). */
  function linkRow(label, url) {
    if (!url) return h('div', { class: 'kv' }, h('span', { class: 'k', text: label }), h('span', { class: 'v muted', text: '—' }));
    var href = safeHref(url);
    var link = href
      ? h('a', { href: href, target: '_blank', rel: 'noopener noreferrer', class: 'url', text: String(url) })
      : h('span', { class: 'url', text: String(url) });
    return h('div', { class: 'kv' }, h('span', { class: 'k', text: label }),
      h('span', { class: 'v' }, link, ' ', copyButton(url)));
  }

  /** Two-step inline confirmation (window.confirm() does not work in popups). */
  function confirmButton(label, confirmLabel, cls, onConfirm) {
    var b = h('button', { type: 'button', class: cls || 'btn', text: label });
    var armed = false, timer = null;
    b.addEventListener('click', function () {
      if (!armed) {
        armed = true;
        b.textContent = confirmLabel;
        b.classList.add('armed');
        timer = setTimeout(function () { armed = false; b.textContent = label; b.classList.remove('armed'); }, 4000);
        return;
      }
      clearTimeout(timer);
      armed = false; b.textContent = label; b.classList.remove('armed');
      onConfirm();
    });
    return b;
  }

  /** Error/state panel for ApiError. */
  function errorPanel(err, opts) {
    opts = opts || {};
    var kind = (err && err.kind) || 'server';
    var box = h('div', { class: 'state-panel state-' + kind, attrs: { role: 'alert' }, dataset: { kind: kind } });
    if (kind === 'offline' || kind === 'timeout') {
      box.appendChild(h('strong', { text: 'Backend offline' }));
      box.appendChild(h('p', {}, '— start it with ', h('code', { text: 'applypilot run' })));
      if (opts.backendUrl) box.appendChild(h('p', { class: 'muted small', text: 'Tried ' + opts.backendUrl + (kind === 'timeout' ? ' (timed out)' : '') }));
    } else if (kind === 'auth' || kind === 'no_token') {
      box.appendChild(h('strong', { text: kind === 'auth' ? 'Token rejected' : 'Token missing' }));
      box.appendChild(h('p', { text: kind === 'auth'
        ? 'The backend rejected the API token — open Settings and paste APPLYPILOT_TOKEN from your .env.'
        : 'No API token configured — open Settings and paste the token printed by `applypilot setup`.' }));
      box.appendChild(h('button', { type: 'button', class: 'btn btn-primary', text: 'Open Settings', on: { click: openOptions } }));
    } else {
      box.appendChild(h('strong', { text: 'Error' }));
      box.appendChild(h('p', { text: (err && err.message) || String(err) }));
    }
    return box;
  }

  function errorMessage(err) {
    if (!err) return '';
    if (err.kind === 'offline') return 'Backend offline — start it with `applypilot run`';
    if (err.kind === 'auth') return 'Token rejected — open Settings';
    if (err.kind === 'no_token') return 'Token missing — open Settings';
    return err.message || String(err);
  }

  /* ------------------------------------------------------------ navigation */

  function ext() {
    var b = root.browser;
    return b && b.runtime && typeof b.runtime.getURL === 'function' ? b : null;
  }

  function openOptions() {
    var b = ext();
    if (b && b.runtime.openOptionsPage) { b.runtime.openOptionsPage(); closePopupSoon(); return; }
    root.location.href = 'options.html';
  }

  function dashboardUrl(route) {
    var hash = route ? (route.charAt(0) === '#' ? route : '#' + route) : '';
    var b = ext();
    return b ? b.runtime.getURL('ui/dashboard.html') + hash : 'dashboard.html' + hash;
  }

  function openDashboard(route) {
    var b = ext();
    var url = dashboardUrl(route);
    if (b && b.tabs && b.tabs.create) { b.tabs.create({ url: url }); closePopupSoon(); return url; }
    root.open(url, '_blank', 'noopener');
    return url;
  }

  function closePopupSoon() {
    if (doc.body && doc.body.classList.contains('popup')) setTimeout(function () { try { root.close(); } catch (e) { /* ignore */ } }, 50);
  }

  /* ---------------------------------------------------------------- polling */

  /**
   * Poller(fn, intervalMs): calls fn() (returning a promise), waits, repeats.
   * Pauses while document.hidden; resumes immediately on visibility.
   */
  function Poller(fn, intervalMs) {
    this.fn = fn;
    this.interval = intervalMs;
    this.timer = null;
    this.running = false;
    this.inFlight = false;
    var self = this;
    this._vis = function () { if (!doc.hidden && self.running) self.now(); };
    doc.addEventListener('visibilitychange', this._vis);
  }
  Poller.prototype.start = function () { this.running = true; this.now(); return this; };
  /** Start polling but make the first call after one interval (data already loaded). */
  Poller.prototype.schedule = function (delayMs) {
    var self = this;
    this.running = true;
    clearTimeout(this.timer);
    this.timer = setTimeout(function () { self.now(); }, delayMs === undefined ? this.interval : delayMs);
    return this;
  };
  Poller.prototype.stop = function () {
    this.running = false;
    clearTimeout(this.timer);
    doc.removeEventListener('visibilitychange', this._vis);
  };
  Poller.prototype.now = function () {
    var self = this;
    clearTimeout(this.timer);
    if (!this.running || this.inFlight) return;
    if (doc.hidden) return; // resumed by visibilitychange
    this.inFlight = true;
    Promise.resolve().then(function () { return self.fn(); }).catch(function () { /* fn handles */ })
      .then(function () {
        self.inFlight = false;
        if (self.running) self.timer = setTimeout(function () { self.now(); }, self.interval);
      });
  };

  function onReady(fn) {
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  root.APUI = {
    STATES: STATES, METHODS: METHODS, ATS_LIST: ATS_LIST,
    stateGroup: stateGroup, h: h, append: append, clear: clear, replace: replace,
    renderIfChanged: renderIfChanged, badge: badge, methodBadge: methodBadge,
    toMs: toMs, fmtTime: fmtTime, fmtDuration: fmtDuration, elapsedSince: elapsedSince, fmtAgo: fmtAgo,
    toText: toText, pretty: pretty, humanize: humanize, copyText: copyText, copyButton: copyButton,
    safeHref: safeHref, linkRow: linkRow, confirmButton: confirmButton, errorPanel: errorPanel,
    errorMessage: errorMessage, openOptions: openOptions, openDashboard: openDashboard,
    dashboardUrl: dashboardUrl, Poller: Poller, onReady: onReady
  };
})(typeof window !== 'undefined' ? window : globalThis);
