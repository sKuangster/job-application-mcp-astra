/* ApplyPilot settings page: backend URL + token, test connection, save. */
(function (root) {
  'use strict';
  var api = root.ApplyPilotApi;
  var U = root.APUI;
  var h = U.h;
  var doc = root.document;

  function $(id) { return doc.getElementById(id); }

  function line(ok, text) { return h('div', { class: ok ? 'ok' : 'bad', text: (ok ? '✓ ' : '✗ ') + text }); }

  function current() {
    return { backendUrl: $('backend-url').value.trim() || api.DEFAULT_BACKEND, token: $('token').value.trim() };
  }

  function test() {
    var s = current();
    var out = $('result');
    U.replace(out, h('div', { class: 'muted', text: 'Testing ' + s.backendUrl + '…' }));
    return api.health({ backendUrl: s.backendUrl }).then(function (health) {
      U.replace(out, line(true, 'Backend reachable' + (health && health.version ? ' (version ' + health.version + ')' : '')));
      if (!s.token) { out.appendChild(line(false, 'No token entered — paste APPLYPILOT_TOKEN from .env')); return false; }
      return api.request('GET', '/api/status', { backendUrl: s.backendUrl, token: s.token }).then(function (status) {
        out.appendChild(line(true, 'Token accepted — autopilot ' + (status && status.control || '?') +
          ', bridge ' + (status && status.bridge && status.bridge.connected ? 'connected' : 'not connected')));
        return true;
      }, function (err) {
        out.appendChild(line(false, err.kind === 'auth' ? 'Token rejected (HTTP ' + err.status + ') — check APPLYPILOT_TOKEN in .env' : U.errorMessage(err)));
        return false;
      });
    }, function (err) {
      U.replace(out, line(false, err.isOffline ? 'Backend offline at ' + s.backendUrl + ' — start it with `applypilot run`' : U.errorMessage(err)));
      return false;
    });
  }

  function save(ev) {
    if (ev) ev.preventDefault();
    return api.saveSettings(current()).then(function (saved) {
      $('backend-url').value = saved.backendUrl;
      U.replace($('result'), line(true, 'Saved.'));
      return test();
    });
  }

  function init() {
    api.getSettings().then(function (s) {
      $('backend-url').value = s.backendUrl || api.DEFAULT_BACKEND;
      $('token').value = s.token || '';
    });
    $('settings-form').addEventListener('submit', save);
    $('test').addEventListener('click', test);
    $('open-dashboard').addEventListener('click', function () { U.openDashboard('#/overview'); });
    $('toggle-token').addEventListener('click', function () {
      var input = $('token');
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      this.textContent = show ? 'Hide' : 'Show';
      this.setAttribute('aria-pressed', show ? 'true' : 'false');
    });
  }

  root.ApplyPilotOptions = { test: test, save: save };
  U.onReady(init);
})(window);
