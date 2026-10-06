'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { loadPage, waitFor, tick, click } = require('./harness.cjs');
const F = require('./fixtures.cjs');

function baseRoutes(extra) {
  return Object.assign({
    'GET /api/status': F.status,
    'GET /api/queue': { items: F.appList.items },
    'GET /api/reasoning': { items: [
      { id: 'rq-1', application_id: 'app-102', company: 'Globex', kind: 'answer_batch', question: 'Why Globex?', status: 'pending', created_at: F.NOW - 20 }
    ] },
    'GET /api/metrics': F.metrics,
    'GET /api/config': { ready: true, missing: [], warnings: [], resumes: [{ name: 'swe_resume.pdf', categories: ['software'] }], settings: {} },
    'GET /api/applications': F.appList,
    'GET /api/health': { ok: true, version: '0.1.0' }
  }, extra || {});
}

function setInput(win, el, value, ev = 'input') {
  el.value = value;
  el.dispatchEvent(new win.Event(ev, { bubbles: true }));
}

test('overview renders workers, counters, attention, reasoning, metrics, queue', async () => {
  const p = loadPage('dashboard.html', { hash: '#/overview', routes: baseRoutes() });
  try {
    await waitFor(() => p.doc.querySelectorAll('#view .worker').length === 3, 2000, 'workers');
    assert.match(p.doc.getElementById('top-status').textContent, /AUTOPILOT: RUNNING/);
    assert.equal(p.doc.querySelector('#view .workers-heading').textContent, 'Workers: 2/3');
    assert.equal(p.doc.querySelector('#view [data-counter="submitted"] .n').textContent, '18');
    assert.equal(p.doc.querySelectorAll('#view .attention .att-item').length, 1);
    assert.equal(p.doc.getElementById('nav-attention').textContent, '1');
    await waitFor(() => p.doc.querySelector('.reasoning'), 2000, 'reasoning');
    assert.match(p.doc.querySelector('.reasoning').textContent, /Why Globex/);
    assert.ok([...p.doc.querySelectorAll('h2')].some((h) => /Pending Astra reasoning \(3\)/.test(h.textContent)));
    await waitFor(() => p.doc.querySelector('[data-metric="time_per_application_avg_seconds"]'), 2000, 'metrics');
    assert.equal(p.doc.querySelector('[data-metric="time_per_application_avg_seconds"] .mv').textContent, '5m 13s');
    assert.equal(p.doc.querySelector('[data-metric="reasoning_calls"] .mv').textContent, '41');
    assert.equal(p.doc.querySelectorAll('.bar-row').length, 3);
    assert.ok(p.doc.querySelector('.spark svg polyline'));
    await waitFor(() => p.doc.querySelectorAll('table.apps tbody tr[data-app-id]').length === 2, 2000, 'queue');
    assert.match(p.doc.querySelector('.recent-errors').textContent, /Workday dropdown/);
    assert.equal(p.doc.querySelectorAll('img').length, 0);
    // control: pause from top bar
    click(p.win, p.doc.querySelector('#top-controls [data-control="pause"]'));
    await waitFor(() => p.calls.some((c) => c.path === '/api/control'));
    assert.deepEqual(p.calls.find((c) => c.path === '/api/control').body, { action: 'pause' });
  } finally { p.close(); }
});

test('applications: hash filters produce the right query string', async () => {
  const hash = '#/applications?q=acme&company=Acme&state=FAILED,NEEDS_USER&ats=workday&failure=true&date_from=2026-09-01&date_to=2026-10-01&resume=swe&offset=50';
  const p = loadPage('dashboard.html', { hash, routes: baseRoutes() });
  try {
    await waitFor(() => p.calls.some((c) => c.path === '/api/applications'), 2000, 'applications request');
    const req = p.calls.find((c) => c.path === '/api/applications');
    assert.deepEqual(req.query, {
      q: 'acme', company: 'Acme', state: 'FAILED,NEEDS_USER', ats: 'workday', failure: 'true',
      date_from: '2026-09-01', date_to: '2026-10-01', resume: 'swe', offset: '50', limit: '50'
    });
    await waitFor(() => p.doc.querySelectorAll('table.apps tbody tr[data-app-id]').length === 2);
    // form reflects hash
    const view = p.doc.getElementById('view');
    assert.equal(view.querySelector('input[type=search]').value, 'acme');
    assert.equal(view.querySelector('input[value="FAILED"]').checked, true);
    assert.equal(view.querySelector('input[value="NEEDS_USER"]').checked, true);
    assert.equal(view.querySelector('input[value="SUBMITTED"]').checked, false);
    assert.match(p.doc.querySelector('.pager').textContent, /Prev/);
    assert.match(view.textContent, /Showing 51–52 of 120/);
    // toggle a state checkbox -> new hash + new request with offset reset
    const before = p.calls.filter((c) => c.path === '/api/applications').length;
    const sub = view.querySelector('input[value="SUBMITTED"]');
    sub.checked = true;
    sub.dispatchEvent(new p.win.Event('change', { bubbles: true }));
    await waitFor(() => p.calls.filter((c) => c.path === '/api/applications').length > before, 2000, 'refetch');
    const last = p.calls.filter((c) => c.path === '/api/applications').pop();
    assert.equal(last.query.state, 'NEEDS_USER,SUBMITTED,FAILED');
    assert.equal(last.query.offset, '0');
    assert.match(p.win.location.hash, /state=NEEDS_USER%2CSUBMITTED%2CFAILED/);
    // row click -> detail route
    click(p.win, view.querySelector('tr[data-app-id="app-102"]'));
    assert.equal(p.win.location.hash, '#/application/app-102');
  } finally { p.close(); }
});

test('applications: next page and failures-only', async () => {
  const p = loadPage('dashboard.html', { hash: '#/applications', routes: baseRoutes() });
  try {
    await waitFor(() => p.doc.querySelector('[data-page="next"]'));
    const first = p.calls.find((c) => c.path === '/api/applications');
    assert.deepEqual(first.query, { limit: '50', offset: '0' });
    click(p.win, p.doc.querySelector('[data-page="next"]'));
    await waitFor(() => p.calls.filter((c) => c.path === '/api/applications').length >= 2);
    assert.equal(p.calls.filter((c) => c.path === '/api/applications').pop().query.offset, '50');
    const fail = p.doc.querySelector('#view .filters input[type=checkbox]:not([value])');
    fail.checked = true;
    fail.dispatchEvent(new p.win.Event('change', { bubbles: true }));
    await waitFor(() => p.calls.filter((c) => c.path === '/api/applications').pop().query.failure === 'true');
  } finally { p.close(); }
});

function detailRoutes(extra) {
  let polled = 0;
  return baseRoutes(Object.assign({
    'GET /api/applications/app-101': F.detail,
    'GET /api/applications/app-101/events': (req) => {
      polled++;
      if (req.query.after_id === '30' && polled === 1) {
        return { events: [{ id: 31, ts: F.NOW, application_id: 'app-101', component: 'browser', event_type: 'click',
          level: 'info', message: 'Clicked Next', data: { element_id: '0:40' } }] };
      }
      return { events: [] };
    },
    'POST /api/applications/app-101/answer': (req) => F.summary({ state: 'QUEUED' }),
    'POST /api/applications/app-101/action': () => F.summary()
  }, extra || {}));
}

test('detail renders answers with methods, sources and the timeline in order', async () => {
  const p = loadPage('dashboard.html', { hash: '#/application/app-101?tab=answers', routes: detailRoutes() });
  try {
    await waitFor(() => p.doc.querySelectorAll('table.answers tbody tr[data-field-key]').length === 11, 2000, 'answers');
    const rows = [...p.doc.querySelectorAll('table.answers tbody tr[data-field-key]')];
    const methods = rows.map((r) => r.querySelector('.mbadge').textContent);
    for (const m of ['deterministic', 'fact', 'preference', 'eeo', 'template', 'reused', 'generated', 'user', 'default', 'research']) {
      assert.ok(methods.includes(m), 'method ' + m);
    }
    const fact = rows.find((r) => r.dataset.method === 'fact');
    assert.equal(fact.querySelector('.answer-source').textContent, 'candidate.identity.first_name');
    const gen = rows.find((r) => r.dataset.method === 'generated');
    assert.equal(gen.querySelector('.answer-value').textContent, 'Paragraph one.\n\nParagraph two ' + F.XSS);
    assert.ok(gen.querySelector('button').textContent.includes('Copy'));
    // XSS: answer text never becomes markup
    assert.equal(p.doc.querySelectorAll('img').length, 0);
    assert.ok(p.doc.querySelector('[data-section="answers"]').textContent.includes(F.XSS));
    // answers tab is active
    assert.equal(p.doc.querySelector('[data-section="answers"]').hidden, false);
    assert.equal(p.doc.querySelector('[data-section="summary"]').hidden, true);
    // method filter
    const sel = p.doc.querySelector('.method-filter');
    sel.value = 'fact';
    sel.dispatchEvent(new p.win.Event('change', { bubbles: true }));
    assert.equal(p.doc.querySelectorAll('table.answers tbody tr[data-field-key]').length, 1);

    // timeline chronological
    const tl = [...p.doc.querySelectorAll('[data-section="timeline"] .tl-row')];
    assert.equal(tl.length, 30);
    assert.deepEqual(tl.map((r) => Number(r.dataset.eventId)), Array.from({ length: 30 }, (_, i) => i + 1));
    assert.equal(tl[0].querySelector('.tl-rel').textContent, '+0.0s');
    assert.ok(tl[2].querySelector('details pre').textContent.includes('"step": 3'));
    // browser actions / errors derived
    assert.equal(p.doc.querySelectorAll('[data-section="browser"] .tl-row').length, 12);
    const errs = [...p.doc.querySelectorAll('[data-section="errors"] .tl-row')].map((r) => r.dataset.eventId);
    assert.deepEqual(errs, ['7', '21']);
    // summary links: text + copy, rel=noopener
    const a = p.doc.querySelector('[data-section="summary"] a.url');
    assert.equal(a.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(a.getAttribute('target'), '_blank');
    // eligibility + resume + research
    assert.match(p.doc.querySelector('[data-section="eligibility"]').textContent, /location\.us/);
    assert.match(p.doc.querySelector('[data-section="resume"]').textContent, /Role title matches/);
    assert.match(p.doc.querySelector('[data-section="research"]').textContent, /About Acme/);

    // live-append: events polled with after_id=30
    await waitFor(() => p.doc.querySelectorAll('[data-section="timeline"] .tl-row').length === 31, 4000, 'live event');
    const evReq = p.calls.find((c) => c.path === '/api/applications/app-101/events');
    assert.equal(evReq.query.after_id, '30');
    const last = [...p.doc.querySelectorAll('[data-section="timeline"] .tl-row')].pop();
    assert.equal(last.dataset.eventId, '31');
  } finally { p.close(); }
});

test('detail: missing-fact form posts /answer; actions; submit only when READY_TO_SUBMIT', async () => {
  const p = loadPage('dashboard.html', { hash: '#/application/app-101', routes: detailRoutes() });
  try {
    const form = await waitFor(() => p.doc.querySelector('.missing-field[data-field-key="f_grad"]'), 2000, 'missing form');
    assert.equal(p.doc.querySelectorAll('.missing-field').length, 1);
    assert.match(p.doc.querySelector('.missing-form').textContent, /Enter your expected graduation date/);
    const input = form.querySelector('.missing-input');
    input.value = '2027-05';
    form.dispatchEvent(new p.win.Event('submit', { bubbles: true, cancelable: true }));
    await waitFor(() => p.calls.some((c) => c.path.endsWith('/answer')));
    const ans = p.calls.find((c) => c.path.endsWith('/answer'));
    assert.equal(ans.method, 'POST');
    assert.deepEqual(ans.body, { field_key: 'f_grad', value: '2027-05', remember: true });

    assert.ok(!p.doc.querySelector('.detail-actions [data-action="submit"]'));
    for (const a of ['resume', 'retry', 'skip', 'open']) {
      click(p.win, p.doc.querySelector('.detail-actions [data-action="' + a + '"]'));
    }
    await waitFor(() => p.calls.filter((c) => c.path.endsWith('/action')).length === 4);
    assert.deepEqual(p.calls.filter((c) => c.path.endsWith('/action')).map((c) => c.body.action), ['resume', 'retry', 'skip', 'open']);
  } finally { p.close(); }

  const ready = Object.assign({}, F.detail, { summary: F.summary({ state: 'READY_TO_SUBMIT', needs_user: null }) });
  const q = loadPage('dashboard.html', { hash: '#/application/app-101', routes: detailRoutes({ 'GET /api/applications/app-101': ready }) });
  try {
    const sub = await waitFor(() => q.doc.querySelector('.detail-actions [data-action="submit"]'));
    assert.ok(!q.doc.querySelector('.missing-form'));
    click(q.win, sub);
    await tick();
    assert.equal(q.calls.filter((c) => c.path.endsWith('/action')).length, 0, 'submit requires confirmation');
    click(q.win, sub);
    await waitFor(() => q.calls.some((c) => c.path.endsWith('/action')));
    assert.equal(q.calls.find((c) => c.path.endsWith('/action')).body.action, 'submit');
  } finally { q.close(); }
});

test('attention view lists NEEDS_USER applications', async () => {
  const p = loadPage('dashboard.html', { hash: '#/attention', routes: baseRoutes({
    'GET /api/applications': (req) => ({ items: req.query.state === 'NEEDS_USER' ? [F.summary({ id: 'app-200', company: 'Hooli' })] : [], total: 1 })
  }) });
  try {
    await waitFor(() => p.doc.querySelectorAll('#view .att-item').length === 2, 2000, 'attention items');
    assert.match(p.doc.getElementById('view').textContent, /Hooli/);
    assert.match(p.doc.getElementById('view').textContent, /Solve the hCaptcha/);
  } finally { p.close(); }
});

test('logs view renders lines colored by level with filters', async () => {
  const p = loadPage('dashboard.html', { hash: '#/logs?level=warning&component=browser', routes: baseRoutes({
    'GET /api/logs': { lines: [
      { ts: F.NOW - 5, level: 'WARNING', component: 'browser', application_id: 'app-1', message: 'slow page ' + F.XSS },
      { ts: F.NOW - 1, level: 'ERROR', component: 'browser', message: 'boom' },
      'plain text INFO line'
    ] }
  }) });
  try {
    await waitFor(() => p.doc.querySelectorAll('.log-line').length === 3, 2000, 'log lines');
    const req = p.calls.find((c) => c.path === '/api/logs');
    assert.equal(req.query.level, 'warning');
    assert.equal(req.query.component, 'browser');
    assert.ok(p.doc.querySelector('.log-line.lvl-warning'));
    assert.ok(p.doc.querySelector('.log-line.lvl-error'));
    assert.equal(p.doc.querySelectorAll('img').length, 0);
  } finally { p.close(); }
});

test('settings view shows config readiness', async () => {
  const p = loadPage('dashboard.html', { hash: '#/settings', routes: baseRoutes({
    'GET /api/config': { ready: false, missing: ['candidate.identity.phone'], warnings: ['No cover letter template'],
      resumes: [{ name: 'swe_resume.pdf', categories: ['software', 'data'] }], settings: { browser_workers: 2 } }
  }) });
  try {
    await waitFor(() => p.doc.querySelector('.cfg-missing'), 2000, 'config');
    const t = p.doc.getElementById('view').textContent;
    assert.match(t, /candidate\.identity\.phone/);
    assert.match(t, /No cover letter template/);
    assert.match(t, /software, data/);
    assert.match(t, /Browser workers/);
  } finally { p.close(); }
});

test('dashboard offline and 401 states', async () => {
  const p = loadPage('dashboard.html', { hash: '#/overview', routes: () => ({ network: true }) });
  try {
    const panel = await waitFor(() => p.doc.querySelector('#global-error .state-panel'));
    assert.equal(panel.dataset.kind, 'offline');
    assert.match(panel.textContent, /Backend offline/);
  } finally { p.close(); }
  const q = loadPage('dashboard.html', { hash: '#/overview', routes: () => ({ status: 403, json: { detail: 'forbidden' } }) });
  try {
    const panel = await waitFor(() => q.doc.querySelector('#global-error .state-panel'));
    assert.equal(panel.dataset.kind, 'auth');
    assert.match(panel.textContent, /Token rejected/);
  } finally { q.close(); }
});

test('one-time #token= fragment is stored and stripped', async () => {
  const p = loadPage('dashboard.html', { token: null, hash: '#token=frag-secret', routes: baseRoutes() });
  try {
    await waitFor(() => p.calls.some((c) => c.path === '/api/status'));
    assert.equal(p.calls.find((c) => c.path === '/api/status').headers['X-ApplyPilot-Token'], 'frag-secret');
    assert.equal(p.win.localStorage.getItem('applypilot.token'), 'frag-secret');
    assert.ok(!p.win.location.href.includes('frag-secret'));
    assert.equal(p.win.location.hash, '#/overview');
  } finally { p.close(); }
});
