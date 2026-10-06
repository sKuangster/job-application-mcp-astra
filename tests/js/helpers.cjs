"use strict";
// Shared helpers for the ApplyPilot JS tests: a static fixture server that
// records POST /__fixture/submit, and jsdom page loading with the engine.
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");
const FIXTURES = path.join(ROOT, "tests", "fixtures", "ats");
const FIREFOX = path.join(ROOT, "firefox");
const { createEngine } = require(path.join(FIREFOX, "content", "dom-engine.js"));
const Adapters = require(path.join(FIREFOX, "content", "adapters.js"));

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json", ".png": "image/png", ".txt": "text/plain"
};

function startFixtureServer(root = FIXTURES) {
  const submissions = [];
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    if (req.method === "POST" && url.pathname === "/__fixture/submit") {
      let body = "";
      req.on("data", c => { body += c; if (body.length > 5e6) req.destroy(); });
      req.on("end", () => {
        let parsed = null;
        try { parsed = JSON.parse(body); } catch (e) { parsed = { raw: body }; }
        submissions.push(parsed);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true, count: submissions.length }));
      });
      return;
    }
    if (req.method === "GET" && url.pathname === "/__fixture/submissions") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ submissions }));
      return;
    }
    let p = decodeURIComponent(url.pathname);
    if (p.endsWith("/")) p += "index.html";
    const file = path.normalize(path.join(root, p));
    if (!file.startsWith(root)) { res.writeHead(403); res.end(); return; }
    fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404, { "Content-Type": "text/html" }); res.end("<h1>Page not found</h1>"); return; }
      res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
      res.end(data);
    });
  });
  return new Promise(resolve => {
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      resolve({
        server, port, submissions,
        base: `http://127.0.0.1:${port}`,
        close: () => new Promise(r => { server.closeAllConnections && server.closeAllConnections(); server.close(() => r()); })
      });
    });
  });
}

async function loadPage(url, { timeout = 10000 } = {}) {
  const errors = [];
  const vc = new VirtualConsole();
  vc.on("jsdomError", e => {
    const msg = String(e && e.message || e);
    if (/Not implemented: (navigation|window\.scrollTo|HTMLFormElement\.prototype\.requestSubmit)/.test(msg)) return;
    errors.push(msg);
  });
  const dom = await JSDOM.fromURL(url, { runScripts: "dangerously", resources: "usable", pretendToBeVisual: true, virtualConsole: vc });
  const win = dom.window;
  if (win.document.readyState !== "complete") {
    await new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(new Error("load timeout " + url)), timeout);
      win.addEventListener("load", () => { clearTimeout(t); resolve(); });
    });
  }
  const engine = createEngine(win.document, Adapters);
  return { dom, win, doc: win.document, engine, errors, close: () => win.close() };
}

function engineFor(doc) {
  return createEngine(doc, Adapters);
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
async function waitFor(fn, timeout = 4000, interval = 50) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(interval);
  }
  return fn();
}

function field(snap, label, type) {
  const norm = s => String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
  const matches = snap.fields.filter(f => norm(f.label) === norm(label) && (!type || f.type === type));
  if (matches.length) return matches[0];
  const loose = snap.fields.filter(f => norm(f.label).startsWith(norm(label)) && (!type || f.type === type));
  return loose[0] || null;
}
function button(snap, text) {
  const norm = s => String(s || "").toLowerCase().replace(/\s+/g, " ").trim();
  return snap.buttons.find(b => norm(b.text) === norm(text)) || null;
}

const PDF_B64 = Buffer.from("%PDF-1.4\n% ApplyPilot test resume\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n").toString("base64");

module.exports = { ROOT, FIXTURES, FIREFOX, startFixtureServer, loadPage, engineFor, sleep, waitFor, field, button, PDF_B64, createEngine, Adapters };
