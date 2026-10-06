/* ApplyPilot toolbar popup: compact live status, polls GET /api/status every 2s. */
(function (root) {
  'use strict';
  var api = root.ApplyPilotApi;
  var U = root.APUI;
  var W = root.APWidgets;
  var doc = root.document;
  var POLL_MS = 2000;

  var els = {};
  var poller = null;
  var ticker = null;
  var lastControl = null;

  function $(id) { return doc.getElementById(id); }

  function setMessage(text, isError) {
    els.message.textContent = text || '';
    els.message.classList.toggle('error', !!isError);
  }

  function openApp(id) { U.openDashboard('#/application/' + encodeURIComponent(id)); }

  var handlers = {
    control: function (action, mode) {
      setMessage('');
      return api.control(action, mode).then(function (status) {
        if (status && typeof status === 'object' && status.control) render(status);
        setMessage(action === 'start' ? 'Started (' + (mode || '') + ')' : 'Sent: ' + action);
        if (poller) poller.now();
      }, function (err) { setMessage(U.errorMessage(err), true); throw err; });
    },
    action: function (id, action) {
      return api.appAction(id, action).then(function (r) { if (poller) poller.now(); return r; });
    },
    dashboard: function () { U.openDashboard('#/overview'); }
  };

  function renderError(err) {
    U.renderIfChanged(els.state, { kind: err.kind, message: err.message }, function (el) {
      U.replace(el, U.errorPanel(err, { backendUrl: api.__lastBackend }));
    });
    [els.config, els.attention, els.workers, els.counters].forEach(function (el) { U.clear(el); el.__apKey = null; });
    // Still offer a dashboard link when offline.
    U.renderIfChanged(els.controls, 'err', function (el) {
      U.replace(el, h('div', { class: 'btn-row' },
        h('button', { type: 'button', class: 'btn', text: 'Open Dashboard', dataset: { control: 'dashboard' }, on: { click: handlers.dashboard } })));
    });
    U.replace(els.header, h('span', { class: 'control control-STOPPED', text: 'AUTOPILOT: —' }));
    els.header.__apKey = null;
  }

  var h = U.h;

  function render(status) {
    U.clear(els.state); els.state.__apKey = null;
    U.renderIfChanged(els.header, [status.control, status.mode, status.bridge && status.bridge.connected, status.bridge && status.bridge.owned_tabs], function (el) {
      U.replace(el, W.controlHeader(status));
    });
    var control = String(status.control || '').toUpperCase();
    if (lastControl && lastControl !== control) setMessage('Autopilot is now ' + control);
    lastControl = control;
    U.renderIfChanged(els.config, status.config || null, function (el, cfg) { U.replace(el, W.configBanner(cfg)); });
    var needs = Array.isArray(status.needs_user) ? status.needs_user : [];
    U.renderIfChanged(els.attention, needs, function (el) {
      U.replace(el, needs.length ? W.attentionList(needs, handlers) : null);
    });
    U.renderIfChanged(els.workers, status.workers || [], function (el) {
      U.replace(el, W.workerSection(status, openApp));
    });
    U.renderIfChanged(els.counters, [status.today, status.queue_remaining], function (el) {
      U.replace(el, W.counters(status));
    });
    U.renderIfChanged(els.controls, control, function (el) {
      U.replace(el, W.controls(status, handlers));
    });
  }

  function refresh() {
    return api.getSettings().then(function (s) {
      api.__lastBackend = s.backendUrl;
      return api.status();
    }).then(render, function (err) { renderError(err); });
  }

  function init() {
    ['header', 'message', 'state', 'config', 'attention', 'workers', 'counters', 'controls'].forEach(function (id) { els[id] = $(id); });
    $('settings-btn').addEventListener('click', U.openOptions);
    poller = new U.Poller(refresh, POLL_MS).start();
    ticker = setInterval(function () { W.tickElapsed(els.workers); }, 1000);
  }

  root.ApplyPilotPopup = {
    refresh: refresh,
    stop: function () { if (poller) poller.stop(); clearInterval(ticker); }
  };
  U.onReady(init);
})(window);
