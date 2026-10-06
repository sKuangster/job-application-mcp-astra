'use strict';
// Loads an extension UI page into jsdom with a stubbed fetch.
const fs = require('node:fs');
const path = require('node:path');
const { JSDOM } = require('jsdom');

const UI_DIR = path.resolve(__dirname, '../../../firefox/ui');

/**
 * routes: function(req) -> {status, json} | undefined, or object map "METHOD /path" -> json | function(req).
 * Options: hash, token (null → none), extension (browser stub), routes.
 */
function loadPage(page, opts) {
  opts = opts || {};
  const html = fs.readFileSync(path.join(UI_DIR, page), 'utf8');
  const dom = new JSDOM(html, {
    url: 'http://127.0.0.1:8765/ui/' + page + (opts.hash || ''),
    runScripts: 'outside-only',
    pretendToBeVisual: true
  });
  const win = dom.window;
  const realClose = win.close.bind(win);
  const calls = [];
  const routes = opts.routes || {};
  win.fetch = async (url, init) => {
    init = init || {};
    const u = new URL(url);
    const req = {
      method: (init.method || 'GET').toUpperCase(),
      url,
      path: u.pathname,
      query: Object.fromEntries(u.searchParams.entries()),
      search: u.search,
      headers: init.headers || {},
      body: init.body ? JSON.parse(init.body) : undefined
    };
    calls.push(req);
    let res;
    if (typeof routes === 'function') res = routes(req);
    else {
      let r = routes[req.method + ' ' + req.path];
      if (r === undefined) {
        for (const k of Object.keys(routes)) {
          const [m, p] = k.split(' ');
          if (m === req.method && p.endsWith('*') && req.path.startsWith(p.slice(0, -1))) { r = routes[k]; break; }
        }
      }
      if (typeof r === 'function') r = r(req);
      res = r === undefined ? { status: 404, json: { detail: 'no route' } } : (r && r.__status ? { status: r.__status, json: r.json } : { status: 200, json: r });
    }
    if (res && res.network) throw new TypeError('NetworkError when attempting to fetch resource.');
    if (!res) res = { status: 404, json: { detail: 'no route' } };
    const text = res.json === undefined ? '' : JSON.stringify(res.json);
    return {
      ok: res.status >= 200 && res.status < 300,
      status: res.status,
      headers: { get: () => 'application/json' },
      text: async () => text,
      json: async () => JSON.parse(text)
    };
  };
  if (opts.token !== null) win.localStorage.setItem('applypilot.token', opts.token || 'test-token');
  if (opts.extension) win.browser = opts.extension;
  win.alert = () => { throw new Error('alert called'); };
  win.open = (u) => { calls.push({ method: 'OPEN', url: u, path: u }); return null; };
  // execute page scripts in order
  const scripts = [...win.document.querySelectorAll('script[src]')].map((s) => s.getAttribute('src'));
  for (const src of scripts) {
    win.eval(fs.readFileSync(path.join(UI_DIR, src), 'utf8') + '\n//# sourceURL=' + src);
  }
  return { dom, win, doc: win.document, calls, close: realClose };
}

async function waitFor(fn, timeoutMs = 2000, label = 'condition') {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeoutMs) {
    try { last = fn(); if (last) return last; } catch (e) { last = e; }
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error('waitFor timed out: ' + label + (last instanceof Error ? ' (' + last.message + ')' : ''));
}

const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

function click(win, el) {
  el.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
}

module.exports = { loadPage, waitFor, tick, click, UI_DIR };
