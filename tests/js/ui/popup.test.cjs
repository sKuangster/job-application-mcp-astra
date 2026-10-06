'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { loadPage, waitFor, tick, click } = require('./harness.cjs');
const F = require('./fixtures.cjs');

function statusRoutes(extra) {
  let current = F.status;
  return Object.assign({
    'GET /api/status': () => current,
    'POST /api/control': (req) => (current = Object.assign({}, F.status, { control: req.body.action === 'pause' ? 'PAUSED' : req.body.action === 'stop' ? 'STOPPED' : 'RUNNING' })),
    'POST /api/applications/*': (req) => F.summary({ id: req.path.split('/')[3] })
  }, extra || {});
}

test('popup renders header, workers, counters and needs-attention list', async () => {
  const p = loadPage('popup.html', { routes: statusRoutes() });
  try {
    await waitFor(() => p.doc.querySelectorAll('.worker').length === 3, 2000, 'worker cards');
    const header = p.doc.getElementById('header').textContent;
    assert.match(header, /AUTOPILOT: RUNNING/);
    assert.match(header, /backlog/);
    assert.ok(p.doc.querySelector('#header .dot.on'), 'bridge dot connected');
    assert.equal(p.doc.querySelector('.workers-heading').textContent, 'Workers: 2/3');
    const busy = p.doc.querySelectorAll('.worker:not(.idle)');
    assert.equal(busy.length, 2);
    assert.match(busy[0].textContent, /Acme Robotics/);
    assert.match(busy[0].textContent, /Filling 13 fields/);
    assert.match(busy[1].textContent, /Waiting for Astra answers/);
    assert.match(busy[0].querySelector('.w-elapsed').textContent, /^\d+m \d\ds$/);
    // counters
    const c = (k) => p.doc.querySelector('[data-counter="' + k + '"] .n').textContent;
    assert.equal(c('submitted'), '18');
    assert.equal(c('skipped'), '2');
    assert.equal(c('ineligible'), '5');
    assert.equal(c('needs_user'), '1');
    assert.equal(c('failed'), '3');
    assert.equal(c('active'), '2');
    assert.equal(p.doc.querySelector('.queue-remaining').textContent, '127');
    // needs attention
    const att = p.doc.querySelectorAll('.attention .att-item');
    assert.equal(att.length, 1);
    assert.match(att[0].textContent, /CAPTCHA/);
    assert.match(att[0].textContent, /Solve the hCaptcha/);
    // XSS: company name with markup rendered as text
    assert.equal(p.doc.querySelectorAll('img').length, 0);
    assert.ok(att[0].textContent.includes(F.XSS));
    // token header sent
    const st = p.calls.find((c) => c.path === '/api/status');
    assert.equal(st.headers['X-ApplyPilot-Token'], 'test-token');
    // controls for RUNNING
    assert.ok(p.doc.querySelector('[data-control="pause"]'));
    assert.ok(p.doc.querySelector('[data-control="stop"]'));
    assert.ok(!p.doc.querySelector('[data-control="start"]'));
  } finally { p.close(); }
});

test('popup controls call the right endpoints; stop needs confirmation', async () => {
  const p = loadPage('popup.html', { routes: statusRoutes() });
  try {
    await waitFor(() => p.doc.querySelector('[data-control="pause"]'));
    click(p.win, p.doc.querySelector('[data-control="pause"]'));
    await waitFor(() => p.calls.some((c) => c.path === '/api/control'));
    let ctl = p.calls.filter((c) => c.path === '/api/control');
    assert.deepEqual(ctl[0].body, { action: 'pause' });
    assert.equal(ctl[0].method, 'POST');
    await waitFor(() => p.doc.querySelector('[data-control="resume"]'), 2000, 'resume button after pause');
    // stop: first click arms only
    const stop = p.doc.querySelector('[data-control="stop"]');
    click(p.win, stop);
    await tick();
    assert.equal(p.calls.filter((c) => c.path === '/api/control').length, 1);
    assert.match(stop.textContent, /Confirm/);
    click(p.win, stop);
    await waitFor(() => p.calls.filter((c) => c.path === '/api/control').length === 2);
    ctl = p.calls.filter((c) => c.path === '/api/control');
    assert.deepEqual(ctl[1].body, { action: 'stop' });
    // attention actions
    const item = p.doc.querySelector('.att-item');
    click(p.win, item.querySelector('[data-action="open"]'));
    click(p.win, item.querySelector('[data-action="retry_captcha"]'));
    click(p.win, item.querySelector('[data-action="skip"]'));
    await waitFor(() => p.calls.filter((c) => c.path.endsWith('/action')).length === 3);
    const acts = p.calls.filter((c) => c.path.endsWith('/action'));
    assert.deepEqual(acts.map((a) => a.path), Array(3).fill('/api/applications/app-099/action'));
    assert.deepEqual(acts.map((a) => a.body.action), ['open', 'retry_captcha', 'skip']);
  } finally { p.close(); }
});

test('popup start buttons when stopped', async () => {
  const stopped = Object.assign({}, F.status, { control: 'STOPPED', workers: [], needs_user: [] });
  const p = loadPage('popup.html', { routes: { 'GET /api/status': stopped, 'POST /api/control': stopped } });
  try {
    await waitFor(() => p.doc.querySelector('[data-control="start"][data-mode="new_jobs"]'));
    assert.match(p.doc.getElementById('header').textContent, /STOPPED/);
    assert.ok(!p.doc.querySelector('.attention'));
    click(p.win, p.doc.querySelector('[data-control="start"][data-mode="new_jobs"]'));
    await waitFor(() => p.calls.some((c) => c.path === '/api/control'));
    assert.deepEqual(p.calls.find((c) => c.path === '/api/control').body, { action: 'start', mode: 'new_jobs' });
  } finally { p.close(); }
});

test('popup shows config warning banner', async () => {
  const st = Object.assign({}, F.status, { config: { ready: false, missing: ['candidate.identity.first_name', 'resumes'] } });
  const p = loadPage('popup.html', { routes: { 'GET /api/status': st } });
  try {
    const banner = await waitFor(() => p.doc.querySelector('.config-banner'));
    assert.match(banner.textContent, /candidate\.identity\.first_name/);
    assert.equal(banner.querySelectorAll('li').length, 2);
  } finally { p.close(); }
});

test('popup offline state', async () => {
  const p = loadPage('popup.html', { routes: () => ({ network: true }) });
  try {
    const panel = await waitFor(() => p.doc.querySelector('.state-panel'));
    assert.equal(panel.dataset.kind, 'offline');
    assert.match(panel.textContent, /Backend offline/);
    assert.match(panel.textContent, /applypilot run/);
  } finally { p.close(); }
});

test('popup 401 state', async () => {
  const p = loadPage('popup.html', { routes: { 'GET /api/status': { __status: 401, json: { detail: 'bad token' } } } });
  try {
    const panel = await waitFor(() => p.doc.querySelector('.state-panel'));
    assert.equal(panel.dataset.kind, 'auth');
    assert.match(panel.textContent, /Token rejected/);
    assert.ok([...panel.querySelectorAll('button')].some((b) => /Settings/.test(b.textContent)));
  } finally { p.close(); }
});

test('popup missing token state without any request', async () => {
  const p = loadPage('popup.html', { token: null, routes: { 'GET /api/status': F.status } });
  try {
    const panel = await waitFor(() => p.doc.querySelector('.state-panel'));
    assert.equal(panel.dataset.kind, 'no_token');
    assert.match(panel.textContent, /Token missing/);
    assert.equal(p.calls.filter((c) => c.path === '/api/status').length, 0);
  } finally { p.close(); }
});

test('popup in extension mode reads browser.storage and opens dashboard tab on worker click', async () => {
  const created = [];
  const ext = {
    storage: { local: { get: async () => ({ backendUrl: 'http://127.0.0.1:9999/', token: 'ext-token' }), set: async () => {} },
      onChanged: { addListener() {} } },
    runtime: { getURL: (p) => 'moz-extension://abc/' + p, openOptionsPage: () => {} },
    tabs: { create: (o) => { created.push(o); return Promise.resolve({}); } }
  };
  const p = loadPage('popup.html', { token: null, extension: ext, routes: { 'GET /api/status': F.status } });
  try {
    await waitFor(() => p.doc.querySelector('.worker[data-app-id="app-101"]'));
    const st = p.calls.find((c) => c.path === '/api/status');
    assert.ok(st.url.startsWith('http://127.0.0.1:9999/api/status'));
    assert.equal(st.headers['X-ApplyPilot-Token'], 'ext-token');
    p.win.close = () => {};
    click(p.win, p.doc.querySelector('.worker[data-app-id="app-101"]'));
    assert.deepEqual(JSON.parse(JSON.stringify(created)), [{ url: 'moz-extension://abc/ui/dashboard.html#/application/app-101' }]);
  } finally { p.close(); }
});
