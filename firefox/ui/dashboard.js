/*
 * ApplyPilot dashboard SPA (hash routing). Consumes PROTOCOL.md §4.
 * Routes: #/overview, #/applications?…, #/application/<id>?tab=…, #/attention, #/logs?…, #/settings
 */
(function (root) {
  'use strict';
  var api = root.ApplyPilotApi;
  var U = root.APUI;
  var W = root.APWidgets;
  var h = U.h;
  var doc = root.document;

  var STATUS_MS = 2000;
  var EVENTS_MS = 2000;
  var DETAIL_MS = 5000;
  var PAGE_SIZE = 50;

  var els = {};
  var currentView = null;
  var currentKey = null;
  var latestStatus = null;
  var statusListeners = [];
  var statusPoller = null;
  var ticker = null;
  var lastControl = null;
  var lastAttention = null;

  function $(id) { return doc.getElementById(id); }

  function announce(text) { if (els.live) els.live.textContent = text; }

  function setTopMessage(text, isError) {
    els.topMessage.textContent = text || '';
    els.topMessage.classList.toggle('error', !!isError);
  }

  function go(hash) { root.location.hash = hash; }

  function appHash(id) { return '#/application/' + encodeURIComponent(id); }

  /* ================================================================ routing */

  function parseHash() {
    var raw = (root.location.hash || '').replace(/^#\/?/, '');
    var qi = raw.indexOf('?');
    var pathStr = qi >= 0 ? raw.slice(0, qi) : raw;
    var qs = qi >= 0 ? raw.slice(qi + 1) : '';
    var parts = pathStr.split('/').filter(Boolean).map(function (p) {
      try { return decodeURIComponent(p); } catch (e) { return p; }
    });
    var query = {};
    if (qs) {
      new URLSearchParams(qs).forEach(function (v, k) { query[k] = v; });
    }
    return { name: parts[0] || 'overview', args: parts.slice(1), query: query };
  }

  function queryString(obj) {
    var p = new URLSearchParams();
    Object.keys(obj).forEach(function (k) {
      var v = obj[k];
      if (v === undefined || v === null || v === '' || v === false) return;
      if (Array.isArray(v)) { if (!v.length) return; v = v.join(','); }
      p.set(k, v === true ? 'true' : String(v));
    });
    var s = p.toString();
    return s ? '?' + s : '';
  }

  var ROUTES = {
    overview: OverviewView,
    applications: ApplicationsView,
    application: DetailView,
    attention: AttentionView,
    logs: LogsView,
    settings: SettingsView
  };

  function route() {
    var r = parseHash();
    var View = ROUTES[r.name];
    if (!View) { r = { name: 'overview', args: [], query: {} }; View = OverviewView; }
    var key = r.name + '/' + r.args.join('/');
    var links = doc.querySelectorAll('.sidenav a[data-route]');
    for (var i = 0; i < links.length; i++) {
      var active = links[i].getAttribute('data-route') === r.name ||
        (r.name === 'application' && links[i].getAttribute('data-route') === 'applications');
      if (active) links[i].setAttribute('aria-current', 'page'); else links[i].removeAttribute('aria-current');
    }
    if (currentView && currentKey === key && currentView.update) { currentView.update(r.query); return; }
    if (currentView && currentView.destroy) currentView.destroy();
    U.clear(els.view);
    currentKey = key;
    currentView = new View(els.view, r.args, r.query);
    if (currentView.start) currentView.start();
  }

  /* ========================================================== global status */

  function subscribeStatus(fn) {
    statusListeners.push(fn);
    if (latestStatus) fn(latestStatus, null);
    return function () { statusListeners = statusListeners.filter(function (f) { return f !== fn; }); };
  }

  var controlHandlers = {
    control: function (action, mode) {
      setTopMessage('');
      return api.control(action, mode).then(function (status) {
        if (status && status.control) onStatus(status);
        setTopMessage(action === 'start' ? 'Autopilot started (' + mode + ')' : 'Sent: ' + action);
        statusPoller.now();
      }, function (err) { setTopMessage(U.errorMessage(err), true); throw err; });
    }
  };

  function onStatus(status) {
    latestStatus = status;
    U.clear(els.globalError); els.globalError.__apKey = null;
    var control = String(status.control || '').toUpperCase();
    U.renderIfChanged(els.topStatus, [control, status.mode, status.bridge && status.bridge.connected, status.bridge && status.bridge.owned_tabs], function (el) {
      U.replace(el, W.controlHeader(status));
    });
    U.renderIfChanged(els.topControls, control, function (el) { U.replace(el, W.controls(status, controlHandlers)); });
    if (lastControl && lastControl !== control) announce('Autopilot is now ' + control);
    lastControl = control;
    var n = Array.isArray(status.needs_user) ? status.needs_user.length : 0;
    els.navAttention.textContent = String(n);
    els.navAttention.classList.toggle('hidden', n === 0);
    if (lastAttention !== null && n > lastAttention) announce(n + ' application(s) need your attention');
    lastAttention = n;
    els.navVersion.textContent = status.version ? 'backend ' + status.version : '';
    statusListeners.forEach(function (fn) { try { fn(status, null); } catch (e) { if (root.console) root.console.error(e); } });
  }

  function onStatusError(err) {
    U.renderIfChanged(els.globalError, { kind: err.kind, message: err.message }, function (el) {
      api.getSettings().then(function (s) { U.replace(el, U.errorPanel(err, { backendUrl: s.backendUrl })); });
      U.replace(el, U.errorPanel(err));
    });
    U.replace(els.topStatus, h('span', { class: 'control control-STOPPED', text: 'AUTOPILOT: —' }));
    els.topStatus.__apKey = null;
    U.clear(els.topControls); els.topControls.__apKey = null;
    statusListeners.forEach(function (fn) { try { fn(null, err); } catch (e) { /* ignore */ } });
  }

  function refreshStatus() { return api.status().then(onStatus, onStatusError); }

  /* ================================================================ helpers */

  function panel(title, extra) {
    var p = h('section', { class: 'panel' });
    var hd = h('h2', {}, h('span', { text: title }));
    if (extra) hd.appendChild(extra);
    p.appendChild(hd);
    return p;
  }

  function inlineError(err) {
    return h('div', { class: 'inline-error muted', attrs: { role: 'alert' }, text: U.errorMessage(err) });
  }

  function resumeName(r) {
    if (!r) return '';
    if (typeof r === 'string') return r;
    return r.name || r.file || r.filename || r.path || U.toText(r);
  }

  function methodMix(s) {
    var mix = s.method_mix || s.methods || s.answer_methods;
    if (!mix || typeof mix !== 'object') return h('span', { class: 'muted', text: '—' });
    var wrap = h('span', { class: 'mix' });
    Object.keys(mix).forEach(function (k, i) {
      if (i) wrap.appendChild(doc.createTextNode(' '));
      wrap.appendChild(U.methodBadge(k));
      wrap.appendChild(doc.createTextNode(String(mix[k])));
    });
    return wrap;
  }

  function appTable(items, opts) {
    opts = opts || {};
    var cols = opts.columns || ['updated', 'company', 'role', 'state', 'ats', 'resume', 'mix'];
    var HEAD = { idx: '#', updated: 'Updated', company: 'Company', role: 'Role', state: 'State', ats: 'ATS', resume: 'Resume', mix: 'Methods', location: 'Location' };
    var table = h('table', { class: 'data apps' });
    var trh = h('tr');
    cols.forEach(function (c) { trh.appendChild(h('th', { attrs: { scope: 'col' }, text: HEAD[c] })); });
    table.appendChild(h('thead', {}, trh));
    var tb = h('tbody');
    items.forEach(function (s, i) {
      var tr = h('tr', { class: 'clickable', dataset: { appId: s.id }, attrs: { tabindex: '0' } });
      var open = function () { go(appHash(s.id)); };
      tr.addEventListener('click', open);
      tr.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') open(); });
      cols.forEach(function (c) {
        var td = h('td');
        switch (c) {
          case 'idx': td.textContent = String((opts.offset || 0) + i + 1); break;
          case 'updated': td.textContent = U.fmtTime(s.updated_at || s.created_at); td.className = 'mono'; break;
          case 'company': td.textContent = s.company || '—'; break;
          case 'role': td.textContent = s.title || '—'; break;
          case 'location': td.textContent = s.location || ''; break;
          case 'state':
            td.appendChild(U.badge(s.state));
            if (s.state_reason) td.appendChild(h('div', { class: 'small muted', text: s.state_reason }));
            break;
          case 'ats': td.textContent = s.ats || '—'; break;
          case 'resume': td.textContent = resumeName(s.resume) || '—'; break;
          case 'mix': td.appendChild(methodMix(s)); break;
        }
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    table.appendChild(tb);
    if (!items.length) {
      tb.appendChild(h('tr', {}, h('td', { attrs: { colspan: cols.length }, class: 'muted', text: opts.empty || 'No applications.' })));
    }
    return h('div', { class: 'table-wrap' }, table);
  }

  /** Generic key/value rendering of an object. */
  function kvList(obj, order, skip) {
    var wrap = h('div', { class: 'kvlist' });
    if (!obj || typeof obj !== 'object') {
      wrap.appendChild(h('div', { class: 'muted', text: obj == null ? 'No data.' : U.toText(obj) }));
      return wrap;
    }
    var keys = (order || []).filter(function (k) { return k in obj; });
    Object.keys(obj).forEach(function (k) { if (keys.indexOf(k) < 0) keys.push(k); });
    keys.forEach(function (k) {
      if (skip && skip.indexOf(k) >= 0) return;
      var v = obj[k];
      if (v === null || v === undefined || v === '') return;
      if (typeof v === 'string' && /^https?:\/\//i.test(v)) { wrap.appendChild(U.linkRow(U.humanize(k), v)); return; }
      var vEl;
      if (typeof v === 'object') vEl = h('pre', { text: U.pretty(v) });
      else if (/(_at|^ts|time)$/.test(k) && typeof v === 'number') vEl = h('span', { text: U.fmtTime(v) });
      else vEl = h('span', { class: 'answer-value', text: String(v) });
      wrap.appendChild(h('div', { class: 'kv' }, h('span', { class: 'k', text: U.humanize(k) }), h('span', { class: 'v' }, vEl)));
    });
    if (!wrap.firstChild) wrap.appendChild(h('div', { class: 'muted', text: 'No data.' }));
    return wrap;
  }

  /* =============================================================== metrics */

  function isNumMap(v) {
    if (!v || typeof v !== 'object' || Array.isArray(v)) return false;
    var ks = Object.keys(v);
    return ks.length >= 2 && ks.every(function (k) { return typeof v[k] === 'number'; });
  }

  function seriesValues(arr) {
    if (!Array.isArray(arr) || arr.length < 2) return null;
    var vals = arr.map(function (x) {
      if (typeof x === 'number') return { v: x, label: '' };
      if (x && typeof x === 'object') {
        var v = ['value', 'count', 'seconds', 'n', 'avg'].map(function (k) { return x[k]; }).filter(function (n) { return typeof n === 'number'; })[0];
        if (typeof v !== 'number') return null;
        return { v: v, label: U.toText(x.label || x.date || x.day || x.ts || x.hour || '') };
      }
      return null;
    });
    return vals.every(Boolean) ? vals : null;
  }

  function fmtMetric(key, v) {
    var k = key.toLowerCase();
    if (/(_ms|millis)/.test(k)) return U.fmtDuration(v / 1000);
    if (/(seconds|_secs?$|_s$|duration|time_per|wait_time|elapsed)/.test(k)) return U.fmtDuration(v);
    if (/(ratio|rate|share|fraction|pct|percent)/.test(k)) return v <= 1 ? Math.round(v * 1000) / 10 + '%' : Math.round(v * 10) / 10 + '%';
    if (Math.round(v) !== v) return String(Math.round(v * 100) / 100);
    return String(v);
  }

  function flattenNumbers(obj, prefix, out, maps, series) {
    Object.keys(obj || {}).forEach(function (k) {
      var v = obj[k];
      var key = prefix ? prefix + '.' + k : k;
      if (typeof v === 'number') out.push([key, v]);
      else if (Array.isArray(v)) { var s = seriesValues(v); if (s) series.push([key, s]); }
      else if (isNumMap(v)) maps.push([key, v]);
      else if (v && typeof v === 'object') flattenNumbers(v, key, out, maps, series);
    });
  }

  function barChart(title, map) {
    var keys = Object.keys(map);
    var max = Math.max.apply(null, keys.map(function (k) { return Math.abs(map[k]); }).concat([1]));
    var wrap = h('div', { class: 'bars', attrs: { role: 'figure', 'aria-label': U.humanize(title) } },
      h('div', { class: 'ml small muted', text: U.humanize(title) }));
    keys.forEach(function (k) {
      var pct = Math.max(0, Math.round(Math.abs(map[k]) / max * 100));
      wrap.appendChild(h('div', { class: 'bar-row', attrs: { title: k + ': ' + map[k] } },
        h('span', { text: k }),
        h('div', { class: 'bar-track' }, h('div', { class: 'bar-fill', style: 'width:' + pct + '%' })),
        h('span', { class: 'bar-val', text: fmtMetric(title, map[k]) })));
    });
    return wrap;
  }

  var SVGNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs) {
    var el = doc.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, String(attrs[k])); });
    return el;
  }

  function sparkline(title, vals) {
    var wv = 300, hv = 48, pad = 4;
    var nums = vals.map(function (x) { return x.v; });
    var min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
    var span = max - min || 1;
    var pts = vals.map(function (x, i) {
      return [pad + i * (wv - 2 * pad) / (vals.length - 1), hv - pad - (x.v - min) / span * (hv - 2 * pad)];
    });
    var s = svg('svg', { viewBox: '0 0 ' + wv + ' ' + hv, preserveAspectRatio: 'none', role: 'img', 'aria-label': U.humanize(title) + ' trend' });
    s.appendChild(svg('polyline', { points: pts.map(function (p) { return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ') }));
    pts.forEach(function (p, i) {
      var c = svg('circle', { cx: p[0].toFixed(1), cy: p[1].toFixed(1), r: 3 });
      var t = svg('title', {});
      t.textContent = (vals[i].label ? vals[i].label + ': ' : '') + fmtMetric(title, vals[i].v);
      c.appendChild(t);
      s.appendChild(c);
    });
    return h('div', { class: 'spark' },
      h('div', { class: 'ml small muted', text: U.humanize(title) + ' (min ' + fmtMetric(title, min) + ', max ' + fmtMetric(title, max) + ')' }), s);
  }

  function renderMetrics(el, m) {
    var nums = [], maps = [], series = [];
    flattenNumbers(m || {}, '', nums, maps, series);
    U.clear(el);
    if (!nums.length && !maps.length && !series.length) { el.appendChild(h('div', { class: 'muted', text: 'No metrics yet.' })); return; }
    var grid = h('div', { class: 'metric-grid' });
    nums.forEach(function (kv) {
      grid.appendChild(h('div', { class: 'metric', dataset: { metric: kv[0] } },
        h('div', { class: 'mv', text: fmtMetric(kv[0], kv[1]) }),
        h('div', { class: 'ml', text: U.humanize(kv[0]) })));
    });
    el.appendChild(grid);
    maps.forEach(function (kv) { el.appendChild(barChart(kv[0], kv[1])); });
    series.forEach(function (kv) { el.appendChild(sparkline(kv[0], kv[1])); });
  }

  /* ============================================================== Overview */

  function OverviewView(container) {
    var self = this;
    container.appendChild(h('h1', { class: 'sr-only', text: 'Overview' }));
    var config = h('div');
    var attention = h('div');
    var workersP = panel('Workers');
    var workers = h('div');
    workersP.appendChild(workers);
    var countersP = panel('Today');
    var countersEl = h('div');
    countersP.appendChild(countersEl);
    var syncMsg = h('span', { class: 'small muted', attrs: { 'aria-live': 'polite' } });
    var syncRow = h('div', { class: 'btn-row' },
      h('button', { type: 'button', class: 'btn btn-xs', text: 'Sync backlog', dataset: { sync: 'backlog' }, on: { click: function () { doSync('backlog'); } } }),
      h('button', { type: 'button', class: 'btn btn-xs', text: 'Sync new jobs', dataset: { sync: 'new_jobs' }, on: { click: function () { doSync('new_jobs'); } } }),
      syncMsg);
    countersP.appendChild(h('div', { class: 'section' }, syncRow));
    var queueP = panel('Queue (next 50)');
    var queueEl = h('div', {}, h('div', { class: 'loading', text: 'Loading…' }));
    queueP.appendChild(queueEl);
    var reasoningP = panel('Pending Astra reasoning');
    var reasoningEl = h('div');
    reasoningP.appendChild(reasoningEl);
    var errorsP = panel('Recent errors');
    var errorsEl = h('div');
    errorsP.appendChild(errorsEl);
    var metricsP = panel('Metrics');
    var metricsEl = h('div', {}, h('div', { class: 'loading', text: 'Loading…' }));
    metricsP.appendChild(metricsEl);

    container.appendChild(config);
    container.appendChild(attention);
    container.appendChild(h('div', { class: 'grid grid-2' }, workersP, countersP));
    container.appendChild(h('div', { class: 'grid grid-2 section' }, reasoningP, errorsP));
    container.appendChild(h('div', { class: 'section' }, metricsP));
    container.appendChild(h('div', { class: 'section' }, queueP));

    function doSync(mode) {
      syncMsg.textContent = 'Syncing…';
      api.sync(mode).then(function (r) {
        r = r || {};
        syncMsg.textContent = 'Fetched ' + (r.fetched || 0) + ', new ' + (r.new || 0) + ', updated ' + (r.updated || 0) +
          ', closed ' + (r.closed || 0) + ', queued ' + (r.queued || 0);
      }, function (err) { syncMsg.textContent = U.errorMessage(err); });
    }

    var attHandlers = {
      action: function (id, action) { return api.appAction(id, action).then(function (r) { statusPoller.now(); return r; }); },
      details: function (id) { go(appHash(id)); }
    };

    this.unsub = subscribeStatus(function (status) {
      if (!status) return;
      U.renderIfChanged(config, status.config || null, function (el, c) { U.replace(el, W.configBanner(c)); });
      var needs = Array.isArray(status.needs_user) ? status.needs_user : [];
      U.renderIfChanged(attention, needs, function (el) { U.replace(el, needs.length ? W.attentionList(needs, attHandlers) : null); });
      U.renderIfChanged(workers, status.workers || [], function (el) {
        U.replace(el, W.workerSection(status, function (id) { go(appHash(id)); }));
      });
      U.renderIfChanged(countersEl, [status.today, status.queue_remaining], function (el) { U.replace(el, W.counters(status)); });
      U.renderIfChanged(errorsEl, status.recent_errors || [], function (el, list) {
        if (!list.length) { U.replace(el, h('div', { class: 'muted', text: 'No recent errors.' })); return; }
        var ul = h('ul', { class: 'err-list recent-errors' });
        list.forEach(function (e) {
          var li = h('li', {}, h('span', { class: 'mono small muted', text: U.fmtTime(e.ts) + ' ' }));
          if (e.application_id) {
            li.appendChild(h('button', { type: 'button', class: 'linklike', text: e.message || '(no message)', on: { click: function () { go(appHash(e.application_id)); } } }));
          } else li.appendChild(h('span', { text: e.message || '' }));
          ul.appendChild(li);
        });
        U.replace(el, ul);
      });
      reasoningP.firstChild.firstChild.textContent = 'Pending Astra reasoning (' + (status.pending_reasoning || 0) + ')';
    });

    this.pollers = [
      new U.Poller(function () {
        return api.queue(50).then(function (r) {
          var items = (r && r.items) || [];
          U.renderIfChanged(queueEl, items, function (el) {
            U.replace(el, appTable(items, { columns: ['idx', 'company', 'role', 'state', 'ats', 'resume', 'updated'], empty: 'Queue is empty.' }));
          });
        }, function (err) { queueEl.__apKey = null; U.replace(queueEl, inlineError(err)); });
      }, 10000),
      new U.Poller(function () {
        return api.reasoning('pending').then(function (r) {
          var items = (r && r.items) || [];
          U.renderIfChanged(reasoningEl, items, function (el) { U.replace(el, reasoningList(items)); });
        }, function (err) { reasoningEl.__apKey = null; U.replace(reasoningEl, inlineError(err)); });
      }, 5000),
      new U.Poller(function () {
        return api.metrics().then(function (m) {
          U.renderIfChanged(metricsEl, m, renderMetrics);
        }, function (err) { metricsEl.__apKey = null; U.replace(metricsEl, inlineError(err)); });
      }, 15000)
    ];
    self.start = function () { self.pollers.forEach(function (p) { p.start(); }); };
  }
  OverviewView.prototype.destroy = function () {
    this.unsub();
    this.pollers.forEach(function (p) { p.stop(); });
  };

  function reasoningList(items) {
    if (!items.length) return h('div', { class: 'muted', text: 'Nothing pending.' });
    var ul = h('ul', { class: 'err-list reasoning' });
    items.forEach(function (it) {
      var text = it.question || it.prompt || it.summary || it.title || it.kind || it.type || it.id;
      var li = h('li', {},
        h('span', { class: 'mono small muted', text: U.fmtTime(it.created_at || it.ts) + ' ' }),
        it.kind || it.type ? h('span', { class: 'mbadge', text: String(it.kind || it.type) }) : null, ' ');
      var label = (it.company ? it.company + ': ' : '') + U.toText(text);
      if (it.application_id) {
        li.appendChild(h('button', { type: 'button', class: 'linklike', text: label, on: { click: function () { go(appHash(it.application_id)); } } }));
      } else li.appendChild(h('span', { text: label }));
      if (it.status) li.appendChild(h('span', { class: 'small muted', text: ' · ' + it.status }));
      ul.appendChild(li);
    });
    return ul;
  }

  /* ========================================================== Applications */

  var FILTER_KEYS = ['q', 'company', 'role', 'state', 'ats', 'resume', 'failure', 'date_from', 'date_to', 'offset'];

  function ApplicationsView(container, args, query) {
    var self = this;
    this.query = {};
    container.appendChild(h('h1', { text: 'Applications' }));
    var form = h('form', { class: 'filters', attrs: { role: 'search' } });
    form.addEventListener('submit', function (ev) { ev.preventDefault(); apply(0); });
    function field(label, input) { return h('label', { class: 'f' }, h('span', { text: label }), input); }
    var inputs = {
      q: h('input', { type: 'search', attrs: { placeholder: 'Search company, role, answers…', 'aria-label': 'Search' } }),
      company: h('input', { type: 'text', attrs: { placeholder: 'Company' } }),
      role: h('input', { type: 'text', attrs: { placeholder: 'Role' } }),
      ats: h('select', {}, h('option', { value: '', text: 'Any ATS' }), U.ATS_LIST.map(function (a) { return h('option', { value: a, text: a }); })),
      resume: h('input', { type: 'text', attrs: { placeholder: 'Resume', list: 'resume-options' } }),
      date_from: h('input', { type: 'date' }),
      date_to: h('input', { type: 'date' }),
      failure: h('input', { type: 'checkbox' })
    };
    var resumeList = h('datalist', { id: 'resume-options' });
    var stateBoxes = {};
    var stateSummary = h('summary', { text: 'All states' });
    var stateMenu = h('div', { class: 'state-menu' });
    U.STATES.forEach(function (s) {
      var cb = h('input', { type: 'checkbox', value: s });
      stateBoxes[s] = cb;
      cb.addEventListener('change', function () { apply(0); });
      stateMenu.appendChild(h('label', {}, cb, U.badge(s)));
    });
    var statePicker = h('details', { class: 'state-picker' }, stateSummary, stateMenu);
    form.appendChild(field('Search', inputs.q));
    form.appendChild(field('Company', inputs.company));
    form.appendChild(field('Role', inputs.role));
    form.appendChild(field('State', statePicker));
    form.appendChild(field('ATS', inputs.ats));
    form.appendChild(field('Resume', inputs.resume));
    form.appendChild(resumeList);
    form.appendChild(field('From', inputs.date_from));
    form.appendChild(field('To', inputs.date_to));
    form.appendChild(h('label', { class: 'f' }, h('span', { text: 'Failures' }), h('span', {}, inputs.failure, ' only')));
    form.appendChild(h('div', { class: 'btn-row' },
      h('button', { type: 'submit', class: 'btn btn-primary', text: 'Apply' }),
      h('button', { type: 'button', class: 'btn', text: 'Reset', on: { click: function () { go('#/applications'); } } })));
    container.appendChild(form);
    var info = h('div', { class: 'small muted', attrs: { 'aria-live': 'polite' } });
    var results = h('div', {}, h('div', { class: 'loading', text: 'Loading…' }));
    var pager = h('div', { class: 'pager' });
    container.appendChild(info);
    container.appendChild(results);
    container.appendChild(pager);

    var debounce = null;
    ['q', 'company', 'role', 'resume'].forEach(function (k) {
      inputs[k].addEventListener('input', function () { clearTimeout(debounce); debounce = setTimeout(function () { apply(0); }, 350); });
    });
    ['ats', 'date_from', 'date_to', 'failure'].forEach(function (k) { inputs[k].addEventListener('change', function () { apply(0); }); });

    function readForm() {
      var states = U.STATES.filter(function (s) { return stateBoxes[s].checked; });
      return {
        q: inputs.q.value.trim(), company: inputs.company.value.trim(), role: inputs.role.value.trim(),
        state: states.join(','), ats: inputs.ats.value, resume: inputs.resume.value.trim(),
        failure: inputs.failure.checked ? 'true' : '', date_from: inputs.date_from.value, date_to: inputs.date_to.value
      };
    }

    function apply(offset) {
      var f = readForm();
      if (offset) f.offset = offset;
      var hash = '#/applications' + queryString(f);
      if (root.location.hash !== hash) go(hash); else self.update(f);
    }
    this.apply = apply;

    function fillForm(q) {
      ['q', 'company', 'role', 'resume', 'date_from', 'date_to'].forEach(function (k) {
        if (doc.activeElement !== inputs[k]) inputs[k].value = q[k] || '';
      });
      inputs.ats.value = q.ats || '';
      inputs.failure.checked = q.failure === 'true' || q.failure === '1';
      var states = (q.state || '').split(',').filter(Boolean);
      U.STATES.forEach(function (s) { stateBoxes[s].checked = states.indexOf(s) >= 0; });
      stateSummary.textContent = states.length ? states.length + ' state' + (states.length > 1 ? 's' : '') : 'All states';
    }

    this.update = function (q) {
      self.query = q || {};
      fillForm(self.query);
      load();
    };

    var seq = 0;
    function load() {
      var q = self.query;
      var offset = parseInt(q.offset, 10) || 0;
      var params = {};
      FILTER_KEYS.forEach(function (k) { if (q[k]) params[k] = q[k]; });
      params.limit = PAGE_SIZE;
      params.offset = offset;
      var my = ++seq;
      return api.applications(params).then(function (r) {
        if (my !== seq) return;
        var items = (r && r.items) || [];
        var total = r && typeof r.total === 'number' ? r.total : items.length;
        info.textContent = total ? 'Showing ' + (offset + 1) + '–' + (offset + items.length) + ' of ' + total : 'No matching applications';
        U.renderIfChanged(results, items, function (el) { U.replace(el, appTable(items, { empty: 'No matching applications.' })); });
        U.replace(pager,
          h('button', { type: 'button', class: 'btn', text: '← Prev', disabled: offset <= 0, dataset: { page: 'prev' },
            on: { click: function () { apply(Math.max(0, offset - PAGE_SIZE)); } } }),
          h('button', { type: 'button', class: 'btn', text: 'Next →', disabled: offset + items.length >= total, dataset: { page: 'next' },
            on: { click: function () { apply(offset + PAGE_SIZE); } } }));
      }, function (err) {
        if (my !== seq) return;
        results.__apKey = null;
        U.replace(results, U.errorPanel(err));
      });
    }
    this.load = load;

    api.config().then(function (c) {
      (c && c.resumes || []).forEach(function (r) { resumeList.appendChild(h('option', { value: resumeName(r) })); });
    }, function () { /* optional */ });

    this.update(query);
    this.poller = new U.Poller(load, 15000);
    this.start = function () { self.poller.schedule(); };
  }
  ApplicationsView.prototype.destroy = function () { this.poller.stop(); };

  /* ================================================================ Detail */

  var TABS = [
    ['summary', 'Summary'], ['eligibility', 'Eligibility'], ['resume', 'Resume'], ['answers', 'Questions & Answers'],
    ['research', 'Research'], ['browser', 'Browser actions'], ['errors', 'Errors/Retries'], ['submission', 'Submission'],
    ['timeline', 'Timeline']
  ];
  var UNRESOLVED = ['missing', 'needs_user', 'unresolved', 'pending', 'unanswered', 'blocked'];

  function eventSortKey(a, b) {
    var ta = U.toMs(a.ts) || 0, tb = U.toMs(b.ts) || 0;
    if (ta !== tb) return ta - tb;
    var ia = Number(a.id), ib = Number(b.id);
    if (!isNaN(ia) && !isNaN(ib)) return ia - ib;
    return String(a.id).localeCompare(String(b.id));
  }

  function DetailView(container, args, query) {
    var self = this;
    this.id = args[0];
    this.tab = 'summary';
    this.events = [];
    this.eventIds = {};
    this.detail = null;
    this.methodFilter = '';
    this.container = container;

    this.head = h('div', { class: 'detail-head' }, h('div', { class: 'loading', text: 'Loading application…' }));
    this.actionsMsg = h('div', { class: 'small', attrs: { 'aria-live': 'polite' } });
    this.missing = h('div');
    this.tabBar = h('div', { class: 'tabs', attrs: { role: 'tablist' } });
    this.sections = {};
    this.tabButtons = {};
    TABS.forEach(function (t) {
      var b = h('button', { type: 'button', text: t[1], attrs: { role: 'tab', 'aria-selected': 'false', 'aria-controls': 'sec-' + t[0] }, dataset: { tab: t[0] } });
      b.addEventListener('click', function () { self.setTab(t[0], true); });
      self.tabButtons[t[0]] = b;
      self.tabBar.appendChild(b);
      self.sections[t[0]] = h('section', { class: 'panel tab-panel', id: 'sec-' + t[0], attrs: { role: 'tabpanel' }, dataset: { section: t[0] } });
    });
    container.appendChild(this.head);
    container.appendChild(this.actionsMsg);
    container.appendChild(this.missing);
    container.appendChild(this.tabBar);
    TABS.forEach(function (t) { container.appendChild(self.sections[t[0]]); });

    // timeline skeleton (append-only)
    this.timelineList = h('ol', { class: 'timeline' });
    this.timelineCount = h('span', { class: 'small muted' });
    U.replace(this.sections.timeline, h('h2', {}, h('span', { text: 'Timeline' }), this.timelineCount), this.timelineList);

    this.update(query);
  }

  DetailView.prototype.update = function (q) {
    this.setTab((q && q.tab) || 'summary', false);
  };

  DetailView.prototype.setTab = function (tab, push) {
    if (!this.sections[tab]) tab = 'summary';
    this.tab = tab;
    var self = this;
    TABS.forEach(function (t) {
      var on = t[0] === tab;
      self.tabButtons[t[0]].setAttribute('aria-selected', on ? 'true' : 'false');
      self.sections[t[0]].hidden = !on;
    });
    if (push) {
      var hash = appHash(this.id) + (tab === 'summary' ? '' : '?tab=' + tab);
      try { root.history.replaceState(null, '', hash); } catch (e) { root.location.hash = hash; }
    }
  };

  DetailView.prototype.start = function () {
    var self = this;
    this.detailPoller = new U.Poller(function () { return self.loadDetail(); }, DETAIL_MS).start();
  };

  DetailView.prototype.destroy = function () {
    if (this.detailPoller) this.detailPoller.stop();
    if (this.eventsPoller) this.eventsPoller.stop();
  };

  DetailView.prototype.loadDetail = function () {
    var self = this;
    return api.application(this.id).then(function (d) {
      d = d || {};
      self.render(d);
      if (!self.eventsPoller) {
        self.addEvents(Array.isArray(d.events) ? d.events : []);
        // first poll after the interval when the detail already carried events
        self.eventsPoller = new U.Poller(function () { return self.loadEvents(); }, EVENTS_MS)
          .schedule(Array.isArray(d.events) ? EVENTS_MS : 0);
      }
    }, function (err) {
      if (!self.detail) { U.replace(self.head, U.errorPanel(err)); self.head.__apKey = null; }
      else self.actionsMsg.textContent = U.errorMessage(err);
    });
  };

  DetailView.prototype.lastEventId = function () {
    var max = null;
    this.events.forEach(function (e) {
      var n = Number(e.id);
      if (!isNaN(n)) { if (max === null || n > max) max = n; }
    });
    if (max !== null) return max;
    return this.events.length ? this.events[this.events.length - 1].id : null;
  };

  DetailView.prototype.loadEvents = function () {
    var self = this;
    return api.events(this.id, this.lastEventId(), 500).then(function (r) {
      self.addEvents((r && r.events) || []);
    }, function () { /* transient; detail poller reports */ });
  };

  DetailView.prototype.addEvents = function (list) {
    var self = this;
    var fresh = list.filter(function (e) {
      var k = String(e.id);
      if (e.id === undefined || self.eventIds[k]) return false;
      self.eventIds[k] = true;
      return true;
    });
    if (!fresh.length && this.events.length) return;
    fresh.sort(eventSortKey);
    var needResort = fresh.length && this.events.length && eventSortKey(fresh[0], this.events[this.events.length - 1]) < 0;
    this.events = this.events.concat(fresh);
    if (needResort) {
      this.events.sort(eventSortKey);
      U.clear(this.timelineList);
      fresh = this.events;
    }
    var t0 = this.events.length ? U.toMs(this.events[0].ts) : null;
    fresh.forEach(function (e) { self.timelineList.appendChild(timelineRow(e, t0)); });
    this.timelineCount.textContent = this.events.length + ' events';
    this.tabButtons.timeline.textContent = 'Timeline (' + this.events.length + ')';
    this.renderDerived();
  };

  function relTime(ms, t0) {
    if (ms === null || t0 === null) return '';
    var s = (ms - t0) / 1000;
    var m = Math.floor(s / 60);
    var sec = s - m * 60;
    return '+' + (m ? m + 'm' : '') + (m ? (sec < 10 ? '0' : '') : '') + sec.toFixed(1) + 's';
  }

  function timelineRow(e, t0) {
    var lvl = String(e.level || 'info').toLowerCase();
    var ms = U.toMs(e.ts);
    var body = h('div', { class: 'tl-body' },
      h('span', { class: 'tl-type', text: e.event_type || '' }),
      lvl !== 'info' ? h('span', { class: 'small', text: ' [' + lvl + ']' }) : null,
      h('div', { class: 'tl-msg', text: e.message || '' }));
    var hasData = e.data && (typeof e.data !== 'object' || Object.keys(e.data).length);
    if (hasData) body.appendChild(h('details', {}, h('summary', { class: 'small muted', text: 'data' }), h('pre', { text: U.pretty(e.data) })));
    return h('li', { class: 'tl-row lvl-' + lvl, dataset: { eventId: e.id, component: e.component || '' } },
      h('span', { class: 'tl-rel', text: relTime(ms, t0) }),
      h('span', { class: 'tl-time', text: U.fmtTime(e.ts) }),
      h('span', { class: 'tl-comp', text: (e.component || '') + (e.worker_id ? ' · ' + e.worker_id : '') }),
      body);
  }

  function eventTable(list, empty) {
    if (!list.length) return h('div', { class: 'muted', text: empty });
    var t0 = U.toMs(list[0].ts);
    var ol = h('ol', { class: 'timeline' });
    list.forEach(function (e) { ol.appendChild(timelineRow(e, t0)); });
    return ol;
  }

  DetailView.prototype.renderDerived = function () {
    var browserEvents = this.events.filter(function (e) { return e.component === 'browser'; });
    U.renderIfChanged(this.sections.browser, browserEvents.map(function (e) { return e.id; }), function (el) {
      U.replace(el, h('h2', { text: 'Browser actions (' + browserEvents.length + ')' }), eventTable(browserEvents, 'No browser actions recorded.'));
    });
    this.tabButtons.browser.textContent = 'Browser actions (' + browserEvents.length + ')';
    var errs = [], seen = {};
    ((this.detail && this.detail.errors) || []).concat(this.events).forEach(function (e) {
      var lvl = String(e.level || '').toLowerCase();
      var isErr = lvl === 'warning' || lvl === 'error' || lvl === 'critical' || /retry|error|fail/i.test(e.event_type || '');
      if (!isErr) return;
      var k = String(e.id);
      if (seen[k]) return;
      seen[k] = true;
      errs.push(e);
    });
    errs.sort(eventSortKey);
    var attempts = this.detail && this.detail.summary ? this.detail.summary.attempts : null;
    U.renderIfChanged(this.sections.errors, [errs.map(function (e) { return e.id; }), attempts], function (el) {
      U.replace(el, h('h2', { text: 'Errors & retries (' + errs.length + ')' }),
        attempts != null ? h('p', { class: 'muted', text: 'Attempts: ' + attempts }) : null,
        eventTable(errs, 'No warnings or errors.'));
    });
    this.tabButtons.errors.textContent = 'Errors/Retries (' + errs.length + ')';
  };

  DetailView.prototype.render = function (d) {
    var self = this;
    this.detail = d;
    var s = d.summary || {};
    var id = this.id;

    U.renderIfChanged(this.head, s, function (el) {
      var title = h('div', {},
        h('h1', { text: (s.company || 'Unknown company') + ' — ' + (s.title || 'Unknown role') }),
        h('div', { class: 'section' }, U.badge(s.state), ' ',
          h('span', { class: 'muted', text: s.state_reason || '' })));
      U.replace(el, title, self.actionButtons(s));
    });

    // never wipe a half-typed answer: defer re-render while the form has focus
    if (this.missing.contains(doc.activeElement) && s.state === 'NEEDS_USER') this.missing.__apDeferred = true;
    else U.renderIfChanged(this.missing, [s.state, s.needs_user, d.answers], function (el) {
      U.replace(el, s.state === 'NEEDS_USER' ? self.missingForm(d) : null);
    });

    U.renderIfChanged(this.sections.summary, [s, d.job, d.pages, d.metrics], function (el) {
      var job = d.job || {};
      U.replace(el, h('h2', { text: 'Summary' }),
        kvRows([
          ['Company', s.company], ['Role', s.title], ['Location', s.location || job.location],
          ['State', U.badge(s.state)], ['State reason', s.state_reason], ['ATS', s.ats], ['Domain', s.domain],
          ['Resume', resumeName(s.resume)], ['Eligibility', s.eligibility_decision],
          ['Attempts', s.attempts], ['Worker', s.worker_id], ['Application id', s.id], ['Job id', s.job_id],
          ['Created', s.created_at && U.fmtTime(s.created_at)], ['Updated', s.updated_at && U.fmtTime(s.updated_at)],
          ['Submitted', s.submitted_at && U.fmtTime(s.submitted_at)], ['Failure', s.failure ? U.toText(s.failure) : null]
        ]),
        h('h3', { class: 'section', text: 'Links' }),
        U.linkRow('Source', job.url || job.source_url || job.job_url),
        U.linkRow('Simplify', s.simplify_url),
        U.linkRow('Application', s.application_url),
        U.linkRow('Current page', s.current_url),
        Array.isArray(d.pages) && d.pages.length ? h('div', { class: 'section' }, h('h3', { text: 'Pages visited (' + d.pages.length + ')' }),
          h('ol', {}, d.pages.map(function (p) {
            return h('li', {}, h('span', { class: 'mono small muted', text: U.fmtTime(p.ts) + ' ' }), h('span', { class: 'mbadge', text: p.kind || '?' }), ' ', h('span', { class: 'url', text: p.url || '' }));
          }))) : null,
        d.metrics && Object.keys(d.metrics).length ? h('div', { class: 'section' }, h('h3', { text: 'Metrics' }), metricsBlock(d.metrics)) : null,
        job && Object.keys(job).length ? h('details', { class: 'section' }, h('summary', { text: 'Job record' }), kvList(job, ['company', 'title', 'location', 'url'], ['description'])) : null,
        job.description ? h('details', { class: 'section' }, h('summary', { text: 'Job description' }), h('pre', { text: U.toText(job.description) })) : null);
    });

    U.renderIfChanged(this.sections.eligibility, d.eligibility || null, function (el, e) {
      e = e || {};
      var rules = e.matched_rules || e.rules || e.matches;
      U.replace(el, h('h2', { text: 'Eligibility' }),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'Decision' }), h('span', { class: 'v' }, U.badge(String(e.decision || s.eligibility_decision || 'unknown').toUpperCase()))),
        e.reasoning || e.reason ? h('div', { class: 'section' }, h('h3', { text: 'Reasoning' }), h('div', { class: 'answer-value', text: U.toText(e.reasoning || e.reason) })) : null,
        Array.isArray(rules) && rules.length ? h('div', { class: 'section' }, h('h3', { text: 'Matched rules' }),
          h('ul', {}, rules.map(function (r) { return h('li', { class: 'mono', text: U.toText(r) }); }))) : null,
        kvList(e, [], ['decision', 'reasoning', 'reason', 'matched_rules', 'rules', 'matches']));
    });

    U.renderIfChanged(this.sections.resume, [d.resume, s.resume], function (el) {
      var r = d.resume || {};
      U.replace(el, h('h2', { text: 'Resume' }),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'File' }), h('span', { class: 'v mono', text: resumeName(r.file || r.name || r.path ? r : s.resume) || '—' })),
        r.reason || r.why || r.reasoning ? h('div', { class: 'kv' }, h('span', { class: 'k', text: 'Why' }), h('span', { class: 'v answer-value', text: U.toText(r.reason || r.why || r.reasoning) })) : null,
        kvList(r, ['category'], ['file', 'name', 'path', 'reason', 'why', 'reasoning']));
    });

    var answers = Array.isArray(d.answers) ? d.answers : [];
    this.tabButtons.answers.textContent = 'Questions & Answers (' + answers.length + ')';
    U.renderIfChanged(this.sections.answers, [answers, this.methodFilter], function (el) { self.renderAnswers(el, answers); });

    var research = Array.isArray(d.research) ? d.research : [];
    this.tabButtons.research.textContent = 'Research (' + research.length + ')';
    U.renderIfChanged(this.sections.research, research, function (el) {
      U.replace(el, h('h2', { text: 'Research' }), research.length ? research.map(researchCard) : h('div', { class: 'muted', text: 'No company research used.' }));
    });

    U.renderIfChanged(this.sections.submission, d.submission || null, function (el, sub) {
      U.replace(el, h('h2', { text: 'Submission evidence' }), sub ? evidence(sub) : h('div', { class: 'muted', text: 'Not submitted yet.' }));
    });

    this.renderDerived();
  };

  function kvRows(rows) {
    var wrap = h('div', { class: 'kvlist' });
    rows.forEach(function (r) {
      if (r[1] === null || r[1] === undefined || r[1] === '') return;
      wrap.appendChild(h('div', { class: 'kv' }, h('span', { class: 'k', text: r[0] }),
        h('span', { class: 'v' }, typeof r[1] === 'object' ? r[1] : String(r[1]))));
    });
    return wrap;
  }

  function metricsBlock(m) { var el = h('div'); renderMetrics(el, m); return el; }

  function researchCard(r) {
    var sources = r.sources || r.urls || [];
    return h('div', { class: 'card section research-item' },
      h('div', { class: 'w-top' },
        h('strong', { text: r.topic || r.query || r.title || r.kind || ('Research ' + (r.id || '')) }),
        h('span', { class: 'small muted mono', text: U.fmtTime(r.created_at || r.ts || r.fetched_at) })),
      r.summary || r.content || r.text ? h('div', { class: 'answer-value', text: U.toText(r.summary || r.content || r.text) }) : null,
      Array.isArray(sources) && sources.length ? h('ul', { class: 'small' }, sources.map(function (src) {
        var url = typeof src === 'string' ? src : (src.url || src.href);
        var label = typeof src === 'string' ? src : (src.title || src.url || U.toText(src));
        var href = U.safeHref(url);
        return h('li', {}, href ? h('a', { href: href, target: '_blank', rel: 'noopener noreferrer', text: label }) : h('span', { text: label }),
          src && src.fetched_at ? h('span', { class: 'muted', text: ' · ' + U.fmtTime(src.fetched_at) }) : null);
      })) : null,
      r.id ? h('div', { class: 'small muted mono', text: 'id: ' + r.id }) : null);
  }

  function evidence(sub) {
    var wrap = h('div', { class: 'evidence' });
    var shot = sub.screenshot || sub.screenshot_data_url;
    wrap.appendChild(kvList(sub, ['submitted_at', 'confirmed', 'confirmation_text', 'url', 'method'], ['screenshot', 'screenshot_data_url']));
    if (typeof shot === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(shot)) {
      wrap.appendChild(h('img', { src: shot, alt: 'Submission screenshot' }));
    } else if (typeof shot === 'string') {
      wrap.appendChild(h('div', { class: 'kv' }, h('span', { class: 'k', text: 'Screenshot' }), h('span', { class: 'v mono', text: shot })));
    }
    return wrap;
  }

  DetailView.prototype.renderAnswers = function (el, answers) {
    var self = this;
    var filter = h('select', { attrs: { 'aria-label': 'Filter by method' }, class: 'method-filter' },
      h('option', { value: '', text: 'All methods' }),
      U.METHODS.map(function (m) {
        var n = answers.filter(function (a) { return a.method === m; }).length;
        return h('option', { value: m, text: m + ' (' + n + ')' });
      }));
    filter.value = this.methodFilter;
    filter.addEventListener('change', function () {
      self.methodFilter = filter.value;
      U.renderIfChanged(el, [answers, self.methodFilter], function (e) { self.renderAnswers(e, answers); });
      var f = el.querySelector('.method-filter'); if (f) f.focus();
    });
    var shown = this.methodFilter ? answers.filter(function (a) { return a.method === self.methodFilter; }) : answers;
    var table = h('table', { class: 'data answers' });
    table.appendChild(h('thead', {}, h('tr', {}, ['Question', 'Class', 'Method', 'Submitted value', 'Source', 'Conf.', 'Status'].map(function (c) {
      return h('th', { attrs: { scope: 'col' }, text: c });
    }))));
    var tb = h('tbody');
    shown.forEach(function (a) {
      var val = a.value === null || a.value === undefined ? '' : U.toText(a.value);
      var conf = typeof a.confidence === 'number' ? (a.confidence <= 1 ? Math.round(a.confidence * 100) + '%' : String(a.confidence)) : (a.confidence || '');
      tb.appendChild(h('tr', { dataset: { fieldKey: a.field_key || '', method: a.method || '' } },
        h('td', {}, h('div', { class: 'answer-q', text: a.question || a.normalized || a.field_key || '' }),
          h('div', { class: 'small muted mono', text: (a.field_key || '') + (a.field_type ? ' · ' + a.field_type : '') + (a.required ? ' · required' : '') + (a.limit ? ' · max ' + a.limit : '') })),
        h('td', { class: 'small', text: a.qclass || '' }),
        h('td', {}, U.methodBadge(a.method)),
        h('td', {}, h('div', { class: 'answer-value', text: val }), val ? U.copyButton(val, 'Copy answer') : null),
        h('td', { class: 'answer-source', text: U.toText(a.source) }),
        h('td', { class: 'small', text: conf }),
        h('td', {}, h('span', { class: 'small', text: a.status || '' }))));
    });
    if (!shown.length) tb.appendChild(h('tr', {}, h('td', { attrs: { colspan: 7 }, class: 'muted', text: answers.length ? 'No answers with this method.' : 'No answers recorded yet.' })));
    table.appendChild(tb);
    U.replace(el, h('h2', {}, h('span', { text: 'Questions & Answers' }), filter), h('div', { class: 'table-wrap' }, table));
  };

  DetailView.prototype.actionButtons = function (s) {
    var self = this;
    var id = this.id;
    var row = h('div', { class: 'btn-row detail-actions' });
    function act(label, action, cls) {
      var b = h('button', { type: 'button', class: cls || 'btn', text: label, dataset: { action: action } });
      b.addEventListener('click', function () { run(b, action); });
      return b;
    }
    function run(b, action) {
      b.disabled = true;
      self.actionsMsg.textContent = '';
      api.appAction(id, action).then(function () {
        self.actionsMsg.textContent = action === 'open' ? 'Application tab focused in Firefox.' : 'Action sent: ' + action;
        announce('Action ' + action + ' sent');
        self.detailPoller.now();
      }, function (err) { self.actionsMsg.textContent = U.errorMessage(err); })
        .then(function () { b.disabled = false; });
    }
    row.appendChild(act('Resume', 'resume', 'btn btn-primary'));
    row.appendChild(act('Retry', 'retry'));
    row.appendChild(act('Skip', 'skip'));
    row.appendChild(act('Open in Firefox', 'open'));
    if (s.state === 'READY_TO_SUBMIT') {
      var sub = U.confirmButton('Submit', 'Confirm submit?', 'btn btn-primary', function () { run(sub, 'submit'); });
      sub.dataset.action = 'submit';
      row.appendChild(sub);
    }
    if (s.state === 'SUBMISSION_UNCONFIRMED') {
      // The user verified (window/email) that the application really went through.
      var conf = U.confirmButton('Mark submitted', 'Confirm it was submitted?', 'btn btn-primary',
        function () { run(conf, 'confirm_submitted'); });
      conf.dataset.action = 'confirm_submitted';
      row.appendChild(conf);
    }
    return row;
  };

  /** Unresolved fields for NEEDS_USER: from summary.needs_user.fields/missing and answers. */
  function unresolvedFields(d) {
    var out = [], seen = {};
    var nu = d.summary && d.summary.needs_user;
    var extra = nu && typeof nu === 'object' ? (nu.fields || nu.missing || nu.missing_fields || []) : [];
    (Array.isArray(extra) ? extra : []).forEach(function (f) {
      if (typeof f === 'string') f = { field_key: f, question: f };
      if (f && f.field_key && !seen[f.field_key]) { seen[f.field_key] = true; out.push(f); }
    });
    (d.answers || []).forEach(function (a) {
      var st = String(a.status || '').toLowerCase();
      var empty = a.value === null || a.value === undefined || a.value === '';
      if ((UNRESOLVED.indexOf(st) >= 0 || (a.required && empty && st !== 'filled' && st !== 'skipped')) && a.field_key && !seen[a.field_key]) {
        seen[a.field_key] = true;
        out.push(a);
      }
    });
    return out;
  }

  DetailView.prototype.missingForm = function (d) {
    var self = this;
    var nu = d.summary && d.summary.needs_user;
    var fields = unresolvedFields(d);
    var wrap = h('div', { class: 'missing-form', attrs: { role: 'region', 'aria-label': 'Needs your input' } },
      h('h2', { text: 'Needs your input' }),
      nu && typeof nu === 'object' && (nu.reason || nu.instructions) ? h('div', {},
        nu.reason ? h('div', { class: 'att-reason', text: nu.reason }) : null,
        nu.instructions ? h('div', { class: 'att-instr', text: nu.instructions }) : null) : null);
    if (!fields.length) {
      wrap.appendChild(h('p', { class: 'muted', text: 'No unresolved fields — follow the instructions, then press Resume.' }));
      return wrap;
    }
    fields.forEach(function (f) {
      var input;
      var opts = Array.isArray(f.options) ? f.options : null;
      if (opts && opts.length) {
        input = h('select', {}, h('option', { value: '', text: 'Choose…' }), opts.map(function (o) {
          var label = typeof o === 'string' ? o : (o.label || o.value);
          return h('option', { value: String(label), text: String(label) });
        }));
      } else if (f.field_type === 'textarea' || (f.limit && f.limit > 200)) {
        input = h('textarea', { attrs: { maxlength: f.limit || null } });
      } else {
        input = h('input', { type: 'text' });
      }
      input.setAttribute('aria-label', f.question || f.field_key);
      input.className = 'missing-input';
      var remember = h('input', { type: 'checkbox', checked: true, class: 'remember' });
      var msg = h('span', { class: 'small', attrs: { 'aria-live': 'polite' } });
      var btn = h('button', { type: 'submit', class: 'btn btn-primary', text: 'Save answer' });
      var form = h('form', { class: 'missing-field', dataset: { fieldKey: f.field_key } },
        h('strong', { text: f.question || f.normalized || f.field_key }),
        h('span', { class: 'small muted mono', text: f.field_key }),
        input,
        h('div', { class: 'btn-row' }, h('label', { class: 'small' }, remember, 'Remember for future applications'), btn, msg));
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var value = input.value;
        if (!String(value).trim()) { msg.textContent = 'Enter a value first.'; return; }
        btn.disabled = true;
        api.answer(self.id, f.field_key, value, remember.checked).then(function () {
          msg.textContent = 'Saved.';
          self.detailPoller.now();
        }, function (err) { msg.textContent = U.errorMessage(err); btn.disabled = false; });
      });
      wrap.appendChild(form);
    });
    return wrap;
  };

  /* ============================================================= Attention */

  function AttentionView(container) {
    var self = this;
    container.appendChild(h('h1', { text: 'Needs attention' }));
    var list = h('div', {}, h('div', { class: 'loading', text: 'Loading…' }));
    container.appendChild(list);
    var fromStatus = [], fromApps = [];
    var handlers = {
      action: function (id, action) { return api.appAction(id, action).then(function (r) { statusPoller.now(); self.poller.now(); return r; }); },
      details: function (id) { go(appHash(id)); }
    };
    function draw() {
      var map = {}, items = [];
      fromStatus.forEach(function (n) { map[n.application_id] = true; items.push(n); });
      fromApps.forEach(function (a) {
        if (map[a.id]) return;
        var nu = a.needs_user && typeof a.needs_user === 'object' ? a.needs_user : {};
        items.push({ application_id: a.id, company: a.company, title: a.title, reason: nu.reason || a.state_reason, kind: nu.kind, instructions: nu.instructions, since: nu.since || a.updated_at });
      });
      U.renderIfChanged(list, items, function (el) {
        if (!items.length) { U.replace(el, h('div', { class: 'panel muted', text: 'Nothing needs your attention.' })); return; }
        U.replace(el, W.attentionList(items, handlers));
      });
    }
    this.unsub = subscribeStatus(function (status, err) {
      if (err) { list.__apKey = null; U.replace(list, U.errorPanel(err)); return; }
      fromStatus = Array.isArray(status.needs_user) ? status.needs_user : [];
      draw();
    });
    this.poller = new U.Poller(function () {
      return api.applications({ state: 'WAITING_FOR_CAPTCHA,NEEDS_USER', limit: 200 }).then(function (r) { fromApps = (r && r.items) || []; draw(); }, function () { /* status handles */ });
    }, 5000);
  }
  AttentionView.prototype.start = function () { this.poller.start(); };
  AttentionView.prototype.destroy = function () { this.unsub(); this.poller.stop(); };

  /* ================================================================== Logs */

  function LogsView(container, args, query) {
    var self = this;
    container.appendChild(h('h1', { text: 'Logs' }));
    var level = h('select', { attrs: { 'aria-label': 'Level' } }, ['', 'debug', 'info', 'warning', 'error'].map(function (l) { return h('option', { value: l, text: l || 'Any level' }); }));
    var component = h('input', { type: 'text', attrs: { placeholder: 'Component', 'aria-label': 'Component' } });
    var appId = h('input', { type: 'text', attrs: { placeholder: 'Application id', 'aria-label': 'Application id' } });
    var limit = h('select', { attrs: { 'aria-label': 'Lines' } }, ['200', '500', '1000'].map(function (l) { return h('option', { value: l, text: l + ' lines' }); }));
    var auto = h('input', { type: 'checkbox', checked: true });
    var form = h('form', { class: 'filters' }, level, component, appId, limit,
      h('label', { class: 'small' }, auto, 'Auto-refresh'),
      h('button', { type: 'submit', class: 'btn', text: 'Refresh' }));
    var out = h('div', { class: 'log', attrs: { role: 'log', 'aria-live': 'off' } }, h('div', { class: 'loading', text: 'Loading…' }));
    container.appendChild(form);
    container.appendChild(out);
    function sync() {
      var q = { level: level.value, component: component.value.trim(), application_id: appId.value.trim(), limit: limit.value === '200' ? '' : limit.value };
      var hash = '#/logs' + queryString(q);
      if (root.location.hash !== hash) { try { root.history.replaceState(null, '', hash); } catch (e) { /* ignore */ } }
      self.poller.now();
    }
    form.addEventListener('submit', function (ev) { ev.preventDefault(); sync(); });
    [level, limit].forEach(function (i) { i.addEventListener('change', sync); });
    var deb = null;
    [component, appId].forEach(function (i) { i.addEventListener('input', function () { clearTimeout(deb); deb = setTimeout(sync, 400); }); });
    auto.addEventListener('change', function () { if (auto.checked) self.poller.now(); });
    this.update = function (q) {
      level.value = q.level || ''; component.value = q.component || ''; appId.value = q.application_id || '';
      limit.value = q.limit || '200';
    };
    this.update(query || {});
    function load() {
      return api.logs({ level: level.value, component: component.value.trim(), application_id: appId.value.trim(), limit: Number(limit.value) })
        .then(function (r) {
          var lines = (r && r.lines) || [];
          U.renderIfChanged(out, lines, function (el) {
            var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
            U.clear(el);
            if (!lines.length) el.appendChild(h('div', { class: 'muted', text: 'No log lines.' }));
            lines.forEach(function (l) { el.appendChild(logLine(l)); });
            if (atBottom) el.scrollTop = el.scrollHeight;
          });
        }, function (err) { out.__apKey = null; U.replace(out, U.errorPanel(err)); });
    }
    // own loop: auto-refresh every 3s while checked and visible; explicit refreshes always load
    var timer = null;
    this.stopped = false;
    function tick() {
      clearTimeout(timer);
      if (self.stopped) return Promise.resolve();
      if (doc.hidden && auto.checked) { timer = setTimeout(tick, 3000); return Promise.resolve(); }
      return load().then(function () {
        if (!self.stopped && auto.checked) timer = setTimeout(tick, 3000);
      });
    }
    this.poller = { now: tick, start: tick, stop: function () { self.stopped = true; clearTimeout(timer); } };
    this.auto = auto;
  }
  LogsView.prototype.start = function () { this.poller.start(); };
  LogsView.prototype.destroy = function () { this.poller.stop(); };

  function logLine(l) {
    if (typeof l === 'string') {
      var m = l.match(/\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b/);
      return h('div', { class: 'log-line lvl-' + (m ? m[1].toLowerCase().replace('warn', 'warning').replace('warninging', 'warning') : 'info'), text: l });
    }
    var lvl = String(l.level || 'info').toLowerCase();
    var parts = [U.fmtTime(l.ts || l.time || l.created_at), (l.level || 'INFO').toUpperCase(), l.component || l.logger || ''];
    if (l.application_id) parts.push('[' + l.application_id + ']');
    parts.push(l.message || l.msg || '');
    var text = parts.filter(function (x) { return x !== ''; }).join('  ');
    if (l.data && typeof l.data === 'object' && Object.keys(l.data).length) text += '  ' + U.toText(l.data);
    return h('div', { class: 'log-line lvl-' + lvl, text: text });
  }

  /* ============================================================== Settings */

  function SettingsView(container) {
    container.appendChild(h('h1', { text: 'Settings' }));
    var conn = panel('Connection');
    var connEl = h('div', {}, h('div', { class: 'loading', text: 'Checking…' }));
    conn.appendChild(connEl);
    var cfgP = panel('Configuration readiness');
    var cfgEl = h('div', {}, h('div', { class: 'loading', text: 'Loading…' }));
    cfgP.appendChild(cfgEl);
    container.appendChild(h('div', { class: 'grid' }, conn, cfgP));

    Promise.all([api.getSettings(), api.health().catch(function (e) { return { __err: e }; })]).then(function (r) {
      var s = r[0], health = r[1];
      var st = latestStatus || {};
      var bridge = st.bridge || {};
      U.replace(connEl,
        kvRows([
          ['Backend URL', s.backendUrl],
          ['Token', s.token ? 'configured (' + s.token.length + ' chars, ' + (s.source === 'extension' ? 'extension storage' : 'browser localStorage') + ')' : 'missing'],
          ['Backend', health.__err ? U.errorMessage(health.__err) : 'reachable' + (health.version ? ' — version ' + health.version : '')],
          ['Firefox bridge', bridge.connected ? 'connected (' + (bridge.owned_tabs || 0) + ' owned tabs, last seen ' + U.fmtAgo(bridge.last_seen) + ')' : 'not connected']
        ]),
        h('div', { class: 'btn-row section' }, h('button', { type: 'button', class: 'btn btn-primary', text: 'Open connection settings', on: { click: U.openOptions } })));
    });

    api.config().then(function (c) {
      c = c || {};
      var missing = c.missing || [], warnings = c.warnings || [], resumes = c.resumes || [];
      U.replace(cfgEl,
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'Ready' }),
          h('span', { class: 'v' }, U.badge(c.ready ? 'SUBMITTED' : 'NEEDS_USER'), ' ', c.ready ? 'Ready' : 'Not ready')),
        h('h3', { class: 'section', text: 'Missing (' + missing.length + ')' }),
        missing.length ? h('ul', { class: 'cfg-missing' }, missing.map(function (m) { return h('li', { class: 'mono', text: U.toText(m) }); })) : h('div', { class: 'muted', text: 'Nothing missing.' }),
        h('h3', { class: 'section', text: 'Warnings (' + warnings.length + ')' }),
        warnings.length ? h('ul', {}, warnings.map(function (m) { return h('li', { text: U.toText(m) }); })) : h('div', { class: 'muted', text: 'No warnings.' }),
        h('h3', { class: 'section', text: 'Resumes (' + resumes.length + ')' }),
        resumes.length ? resumeTable(resumes) : h('div', { class: 'muted', text: 'No resumes discovered.' }),
        h('h3', { class: 'section', text: 'Effective settings' }),
        kvList(c.settings || {}));
    }, function (err) { U.replace(cfgEl, U.errorPanel(err)); });
  }

  function resumeTable(resumes) {
    var t = h('table', { class: 'data' }, h('thead', {}, h('tr', {}, ['Resume', 'Categories', 'Details'].map(function (c) { return h('th', { text: c }); }))));
    var tb = h('tbody');
    resumes.forEach(function (r) {
      if (typeof r === 'string') { tb.appendChild(h('tr', {}, h('td', { class: 'mono', text: r }), h('td'), h('td'))); return; }
      var cats = r.categories || r.category || r.roles || '';
      var rest = {};
      Object.keys(r).forEach(function (k) { if (['name', 'file', 'filename', 'path', 'categories', 'category', 'roles'].indexOf(k) < 0) rest[k] = r[k]; });
      tb.appendChild(h('tr', {}, h('td', { class: 'mono', text: resumeName(r) }), h('td', { text: U.toText(cats) }),
        h('td', { class: 'small muted', text: Object.keys(rest).length ? U.toText(rest) : (r.path && r.path !== resumeName(r) ? r.path : '') })));
    });
    t.appendChild(tb);
    return h('div', { class: 'table-wrap' }, t);
  }

  /* ================================================================== init */

  function init() {
    els.view = $('view');
    els.live = $('live');
    els.topStatus = $('top-status');
    els.topControls = $('top-controls');
    els.topMessage = $('top-message');
    els.globalError = $('global-error');
    els.navAttention = $('nav-attention');
    els.navVersion = $('nav-version');
    // getSettings first so a "#token=" fragment is consumed before routing.
    api.getSettings().then(function () {
      if (!root.location.hash || root.location.hash === '#') {
        try { root.history.replaceState(null, '', '#/overview'); } catch (e) { /* ignore */ }
      }
      root.addEventListener('hashchange', route);
      statusPoller = new U.Poller(refreshStatus, STATUS_MS).start();
      ticker = setInterval(function () { W.tickElapsed(els.view); }, 1000);
      route();
    });
  }

  root.ApplyPilotDashboard = {
    route: route,
    parseHash: parseHash,
    queryString: queryString,
    unresolvedFields: unresolvedFields,
    current: function () { return currentView; },
    refreshStatus: function () { return statusPoller && statusPoller.now(); },
    stop: function () {
      if (statusPoller) statusPoller.stop();
      clearInterval(ticker);
      if (currentView && currentView.destroy) currentView.destroy();
    }
  };
  U.onReady(init);
})(window);
