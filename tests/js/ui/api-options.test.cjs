'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { loadPage, waitFor, click } = require('./harness.cjs');
const F = require('./fixtures.cjs');

test('api client: paths, methods, bodies and typed errors', async () => {
  const p = loadPage('options.html', { routes: (req) => {
    if (req.path === '/api/status') return { status: 500, json: { detail: 'db locked' } };
    if (req.path === '/api/config') return { status: 422, json: { detail: 'bad' } };
    return { status: 200, json: { ok: true } };
  } });
  try {
    const api = p.win.ApplyPilotApi;
    await api.health();
    assert.equal(p.calls[0].headers['X-ApplyPilot-Token'], undefined, 'health is unauthenticated');
    await api.control('start', 'continuous');
    await api.sync('backlog');
    await api.applications({ q: 'x y', state: ['FAILED', 'SKIPPED'], failure: true, company: '' });
    await api.application('a/b');
    await api.events('a1', 0, 100);
    await api.appAction('a1', 'retry', 'note');
    await api.answer('a1', 'f_1', 'v', false);
    await api.queue(50);
    await api.reasoning('pending');
    await api.logs({ level: 'error', application_id: 'a1', limit: 500 });
    await api.metrics();
    const got = p.calls.slice(1).map((c) => c.method + ' ' + c.path + c.search);
    assert.deepEqual(got, [
      'POST /api/control',
      'POST /api/sync',
      'GET /api/applications?q=x%20y&state=FAILED%2CSKIPPED&failure=true',
      'GET /api/applications/a%2Fb',
      'GET /api/applications/a1/events?after_id=0&limit=100',
      'POST /api/applications/a1/action',
      'POST /api/applications/a1/answer',
      'GET /api/queue?limit=50',
      'GET /api/reasoning?status=pending',
      'GET /api/logs?level=error&application_id=a1&limit=500',
      'GET /api/metrics'
    ]);
    assert.deepEqual(p.calls[1].body, { action: 'start', mode: 'continuous' });
    assert.deepEqual(p.calls[2].body, { mode: 'backlog' });
    assert.deepEqual(p.calls[6].body, { action: 'retry', note: 'note' });
    assert.deepEqual(p.calls[7].body, { field_key: 'f_1', value: 'v', remember: false });
    assert.equal(p.calls[1].headers['X-ApplyPilot-Token'], 'test-token');
    await assert.rejects(api.status(), (e) => e.kind === 'server' && e.status === 500 && /db locked/.test(e.message));
    await assert.rejects(api.config(), (e) => e.kind === 'client' && e.status === 422);
  } finally { p.close(); }
});

test('api client: timeout yields kind=timeout', async () => {
  const p = loadPage('options.html', {});
  try {
    p.win.fetch = (url, init) => new Promise((resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new p.win.DOMException('aborted', 'AbortError')));
    });
    await assert.rejects(p.win.ApplyPilotApi.request('GET', '/api/status', { timeoutMs: 30 }), (e) => e.kind === 'timeout' && e.isOffline);
  } finally { p.close(); }
});

test('options: loads settings, toggles token visibility, tests and saves', async () => {
  const p = loadPage('options.html', { routes: { 'GET /api/health': { ok: true, version: '9.9' }, 'GET /api/status': F.status } });
  try {
    const tokenInput = p.doc.getElementById('token');
    await waitFor(() => tokenInput.value === 'test-token');
    assert.equal(p.doc.getElementById('backend-url').value, 'http://127.0.0.1:8765');
    assert.equal(tokenInput.type, 'password');
    click(p.win, p.doc.getElementById('toggle-token'));
    assert.equal(tokenInput.type, 'text');
    tokenInput.value = 'new-token';
    p.doc.getElementById('settings-form').dispatchEvent(new p.win.Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => /Token accepted/.test(p.doc.getElementById('result').textContent), 2000, 'test result');
    assert.equal(p.win.localStorage.getItem('applypilot.token'), 'new-token');
    assert.match(p.doc.getElementById('result').textContent, /version 9\.9/);
    const st = p.calls.find((c) => c.path === '/api/status');
    assert.equal(st.headers['X-ApplyPilot-Token'], 'new-token');
  } finally { p.close(); }
});

test('options: rejected token and offline backend messages', async () => {
  const p = loadPage('options.html', { routes: { 'GET /api/health': { ok: true }, 'GET /api/status': { __status: 401, json: {} } } });
  try {
    await waitFor(() => p.doc.getElementById('token').value === 'test-token');
    click(p.win, p.doc.getElementById('test'));
    await waitFor(() => /Token rejected/.test(p.doc.getElementById('result').textContent), 2000, 'rejected');
  } finally { p.close(); }
  const q = loadPage('options.html', { routes: () => ({ network: true }) });
  try {
    await waitFor(() => q.doc.getElementById('token').value === 'test-token');
    click(q.win, q.doc.getElementById('test'));
    await waitFor(() => /Backend offline/.test(q.doc.getElementById('result').textContent), 2000, 'offline');
  } finally { q.close(); }
});

test('options: saves to browser.storage.local in extension mode', async () => {
  const store = { backendUrl: 'http://127.0.0.1:8765', token: 'old' };
  const ext = {
    storage: { local: { get: async () => Object.assign({}, store), set: async (o) => { Object.assign(store, o); } }, onChanged: { addListener() {} } },
    runtime: { getURL: (x) => 'moz-extension://id/' + x }
  };
  const p = loadPage('options.html', { token: null, extension: ext, routes: { 'GET /api/health': { ok: true }, 'GET /api/status': F.status } });
  try {
    await waitFor(() => p.doc.getElementById('token').value === 'old');
    p.doc.getElementById('backend-url').value = '127.0.0.1:9000/';
    p.doc.getElementById('token').value = ' fresh ';
    p.doc.getElementById('settings-form').dispatchEvent(new p.win.Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => store.token === 'fresh');
    assert.equal(store.backendUrl, 'http://127.0.0.1:9000');
    await waitFor(() => p.calls.some((c) => c.url.startsWith('http://127.0.0.1:9000/api/health')));
  } finally { p.close(); }
});
