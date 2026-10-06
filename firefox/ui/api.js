/*
 * ApplyPilot HTTP API client (docs/PROTOCOL.md §4).
 *
 * Exposes a global `ApplyPilotApi` (extension pages / backend-served /ui/) and
 * `module.exports` (node tests). Settings:
 *   - extension pages: browser.storage.local {backendUrl, token}
 *   - otherwise: localStorage ("applypilot.backendUrl", "applypilot.token");
 *     a one-time "#token=..." fragment is stored and stripped; backendUrl
 *     defaults to location.origin.
 */
(function (root, factory) {
  'use strict';
  var api = factory(root);
  if (typeof module === 'object' && module && module.exports) module.exports = api;
  if (root) root.ApplyPilotApi = api;
})(typeof window !== 'undefined' ? window : globalThis, function (root) {
  'use strict';

  var DEFAULT_BACKEND = 'http://127.0.0.1:8765';
  var DEFAULT_TIMEOUT_MS = 8000;
  var LS_URL = 'applypilot.backendUrl';
  var LS_TOKEN = 'applypilot.token';

  /* ------------------------------------------------------------------ errors */

  var MESSAGES = {
    offline: 'Backend offline — start it with `applypilot run`',
    timeout: 'Backend did not respond in time — is `applypilot run` still running?',
    auth: 'Token rejected — open Settings',
    no_token: 'No API token configured — open Settings',
    not_found: 'Not found',
    server: 'Backend error',
    client: 'Request rejected by backend'
  };

  /**
   * kind: 'offline' | 'timeout' | 'auth' | 'no_token' | 'not_found' | 'server' | 'client'
   */
  function ApiError(kind, message, status, detail) {
    var err = new Error(message || MESSAGES[kind] || 'Request failed');
    err.name = 'ApiError';
    err.kind = kind;
    err.status = status || 0;
    err.detail = detail;
    err.isAuth = kind === 'auth' || kind === 'no_token';
    err.isOffline = kind === 'offline' || kind === 'timeout';
    return err;
  }

  /* ---------------------------------------------------------------- settings */

  function hasExtensionStorage() {
    var b = root.browser;
    return !!(b && b.storage && b.storage.local && typeof b.storage.local.get === 'function');
  }

  function safeLocalStorage() {
    try { return root.localStorage || null; } catch (e) { return null; }
  }

  function normalizeUrl(url) {
    url = String(url || '').trim();
    if (!url) return '';
    if (!/^https?:\/\//i.test(url)) url = 'http://' + url;
    return url.replace(/\/+$/, '');
  }

  function defaultBackendUrl() {
    if (hasExtensionStorage()) return DEFAULT_BACKEND;
    var loc = root.location;
    if (loc && /^https?:$/.test(loc.protocol) && loc.origin && loc.origin !== 'null') return loc.origin;
    return DEFAULT_BACKEND;
  }

  /** Consume a one-time "#token=..." (also "#/route?token=..." or "&token=") fragment. */
  function consumeFragmentToken() {
    var loc = root.location;
    if (!loc || !loc.hash || loc.hash.indexOf('token=') === -1) return null;
    var hash = loc.hash.slice(1);
    var m = hash.match(/(^|[?&])token=([^&]*)/);
    if (!m) return null;
    var token = decodeURIComponent(m[2]);
    var rest = hash.replace(/(^|[?&])token=[^&]*/, function (all, sep) { return sep === '?' ? '?' : ''; })
      .replace(/\?&/, '?').replace(/[?&]$/, '').replace(/^&/, '');
    var ls = safeLocalStorage();
    if (ls && token) { try { ls.setItem(LS_TOKEN, token); } catch (e) { /* ignore */ } }
    try {
      var newUrl = loc.pathname + loc.search + (rest ? '#' + rest : '');
      if (root.history && root.history.replaceState) root.history.replaceState(null, '', newUrl);
      else loc.hash = rest;
    } catch (e) { /* ignore */ }
    return token;
  }

  var cache = null;

  function getSettings() {
    if (cache) return Promise.resolve(cache);
    if (hasExtensionStorage()) {
      return Promise.resolve(root.browser.storage.local.get(['backendUrl', 'token'])).then(function (s) {
        s = s || {};
        cache = {
          backendUrl: normalizeUrl(s.backendUrl) || DEFAULT_BACKEND,
          token: s.token ? String(s.token).trim() : '',
          source: 'extension'
        };
        return cache;
      });
    }
    var fragToken = consumeFragmentToken();
    var ls = safeLocalStorage();
    var url = '', token = '';
    if (ls) {
      try { url = ls.getItem(LS_URL) || ''; token = ls.getItem(LS_TOKEN) || ''; } catch (e) { /* ignore */ }
    }
    cache = {
      backendUrl: normalizeUrl(url) || defaultBackendUrl(),
      token: (fragToken || token || '').trim(),
      source: 'localStorage'
    };
    return Promise.resolve(cache);
  }

  function saveSettings(settings) {
    var next = {
      backendUrl: normalizeUrl(settings.backendUrl) || defaultBackendUrl(),
      token: String(settings.token || '').trim()
    };
    cache = null;
    if (hasExtensionStorage()) {
      return Promise.resolve(root.browser.storage.local.set(next)).then(function () { return next; });
    }
    var ls = safeLocalStorage();
    if (ls) {
      try { ls.setItem(LS_URL, next.backendUrl); ls.setItem(LS_TOKEN, next.token); } catch (e) { /* ignore */ }
    }
    return Promise.resolve(next);
  }

  function clearCache() { cache = null; }

  try {
    if (hasExtensionStorage() && root.browser.storage.onChanged) {
      root.browser.storage.onChanged.addListener(function () { cache = null; });
    }
  } catch (e) { /* ignore */ }

  /* ----------------------------------------------------------------- request */

  function buildQuery(params) {
    if (!params) return '';
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === undefined || v === null || v === '' || v === false) return;
      if (Array.isArray(v)) {
        if (!v.length) return;
        v = v.join(',');
      }
      if (v === true) v = 'true';
      parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(String(v)));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function request(method, path, opts) {
    opts = opts || {};
    return getSettings().then(function (s) {
      var base = opts.backendUrl !== undefined ? normalizeUrl(opts.backendUrl) : s.backendUrl;
      var token = opts.token !== undefined ? opts.token : s.token;
      if (!opts.noAuth && !token) throw ApiError('no_token');
      var url = base + path + buildQuery(opts.query);
      var headers = { Accept: 'application/json' };
      if (!opts.noAuth && token) headers['X-ApplyPilot-Token'] = token;
      var init = { method: method, headers: headers, cache: 'no-store' };
      if (opts.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
      }
      var fetchFn = root.fetch;
      if (typeof fetchFn !== 'function') throw ApiError('offline', 'fetch() unavailable');
      var Ctl = root.AbortController;
      var ctl = Ctl ? new Ctl() : null;
      var timedOut = false;
      var timer = null;
      if (ctl) {
        init.signal = ctl.signal;
        timer = setTimeout(function () { timedOut = true; ctl.abort(); }, opts.timeoutMs || DEFAULT_TIMEOUT_MS);
      }
      return Promise.resolve()
        .then(function () { return fetchFn.call(root, url, init); })
        .then(function (res) {
          return res.text().then(function (text) {
            var data = null;
            if (text) {
              try { data = JSON.parse(text); } catch (e) { data = null; }
            }
            if (res.status === 401 || res.status === 403) {
              throw ApiError('auth', MESSAGES.auth, res.status, data);
            }
            if (res.status === 404) {
              throw ApiError('not_found', detailMessage(data) || MESSAGES.not_found, 404, data);
            }
            if (res.status >= 500) {
              throw ApiError('server', MESSAGES.server + ' (HTTP ' + res.status + ')' +
                (detailMessage(data) ? ': ' + detailMessage(data) : ''), res.status, data);
            }
            if (!res.ok) {
              throw ApiError('client', (detailMessage(data) || MESSAGES.client) + ' (HTTP ' + res.status + ')',
                res.status, data);
            }
            if (data === null && text) throw ApiError('server', 'Backend returned invalid JSON', res.status);
            return data;
          });
        }, function (err) {
          if (timedOut) throw ApiError('timeout');
          throw ApiError('offline', MESSAGES.offline, 0, String(err && err.message || err));
        })
        .then(function (data) { if (timer) clearTimeout(timer); return data; },
          function (err) { if (timer) clearTimeout(timer); throw err; });
    });
  }

  function detailMessage(data) {
    if (!data) return '';
    var d = data.detail !== undefined ? data.detail : (data.error !== undefined ? data.error : data.message);
    if (!d) return '';
    if (typeof d === 'string') return d;
    if (d.message) return String(d.message);
    try { return JSON.stringify(d); } catch (e) { return ''; }
  }

  function enc(id) { return encodeURIComponent(String(id)); }

  /* ----------------------------------------------------------------- methods */

  var api = {
    DEFAULT_BACKEND: DEFAULT_BACKEND,
    MESSAGES: MESSAGES,
    ApiError: ApiError,
    getSettings: getSettings,
    saveSettings: saveSettings,
    clearCache: clearCache,
    buildQuery: buildQuery,
    request: request,
    isExtension: hasExtensionStorage,

    health: function (opts) {
      return request('GET', '/api/health', Object.assign({ noAuth: true, timeoutMs: 4000 }, opts || {}));
    },
    status: function (opts) { return request('GET', '/api/status', opts); },
    control: function (action, mode) {
      var body = { action: action };
      if (mode) body.mode = mode;
      return request('POST', '/api/control', { body: body });
    },
    sync: function (mode) {
      return request('POST', '/api/sync', { body: mode ? { mode: mode } : {}, timeoutMs: 60000 });
    },
    applications: function (filters) {
      return request('GET', '/api/applications', { query: filters || {} });
    },
    application: function (id) { return request('GET', '/api/applications/' + enc(id)); },
    events: function (id, afterId, limit) {
      return request('GET', '/api/applications/' + enc(id) + '/events', {
        query: { after_id: afterId === undefined || afterId === null ? '' : afterId, limit: limit || '' }
      });
    },
    appAction: function (id, action, note) {
      var body = { action: action };
      if (note) body.note = note;
      return request('POST', '/api/applications/' + enc(id) + '/action', { body: body, timeoutMs: 20000 });
    },
    answer: function (id, fieldKey, value, remember) {
      return request('POST', '/api/applications/' + enc(id) + '/answer', {
        body: { field_key: fieldKey, value: value, remember: !!remember }
      });
    },
    queue: function (limit) { return request('GET', '/api/queue', { query: { limit: limit || 50 } }); },
    reasoning: function (status) { return request('GET', '/api/reasoning', { query: { status: status || '' } }); },
    logs: function (filters) { return request('GET', '/api/logs', { query: filters || {} }); },
    metrics: function () { return request('GET', '/api/metrics'); },
    config: function () { return request('GET', '/api/config'); }
  };
  return api;
});
