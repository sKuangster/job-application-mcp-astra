"use strict";
// End-to-end: a throwaway WebSocket "backend" drives tests/js/fake_browser.cjs
// (real bridge-core + real content scripts on jsdom) against the fixtures.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawn } = require("node:child_process");
const h = require("./helpers.cjs");

let WebSocketServer = null;
try { ({ WebSocketServer } = require("ws")); } catch (e) { WebSocketServer = null; }
const skip = WebSocketServer ? false : "the `ws` package is not installed in node_modules";

const TOKEN = "test-token-123";

function startBackend(port = 0) {
  return new Promise(resolve => {
    const wss = new WebSocketServer({ host: "127.0.0.1", port });
    const state = { wss, sock: null, hellos: [], events: [], pending: new Map(), seq: 0, badTokens: 0, waiters: [] };
    wss.on("connection", (sock, req) => {
      const url = new URL(req.url, "http://x");
      if (url.pathname !== "/bridge" || url.searchParams.get("token") !== TOKEN) { state.badTokens++; sock.close(4401, "bad token"); return; }
      state.sock = sock;
      sock.on("message", data => {
        const msg = JSON.parse(String(data));
        if (msg.type === "hello") { state.hellos.push(msg); state.waiters.splice(0).forEach(w => w()); }
        else if (msg.type === "event") state.events.push(msg);
        else if (msg.type === "result") {
          const p = state.pending.get(msg.id);
          if (p) { state.pending.delete(msg.id); p(msg); }
        } else if (msg.type === "pong") {
          const p = state.pending.get("ping"); if (p) { state.pending.delete("ping"); p(msg); }
        }
      });
    });
    state.command = (app_id, action, payload = {}, timeout_ms = 30000) => new Promise((res, rej) => {
      const id = "cmd-" + (++state.seq);
      state.pending.set(id, res);
      const t = setTimeout(() => { state.pending.delete(id); rej(new Error("no result for " + action)); }, timeout_ms + 5000);
      state.pending.set(id, m => { clearTimeout(t); res(m); });
      state.sock.send(JSON.stringify({ type: "command", id, app_id, action, payload, timeout_ms }));
    });
    state.waitHello = n => new Promise(r => { if (state.hellos.length >= n) r(); else state.waiters.push(function check() { if (state.hellos.length >= n) r(); else state.waiters.push(check); }); });
    state.close = () => new Promise(r => { for (const c of wss.clients) c.terminate(); wss.close(() => r()); });
    wss.on("listening", () => { state.port = wss.address().port; resolve(state); });
  });
}

async function ok(p) {
  const m = await p;
  assert.equal(m.ok, true, JSON.stringify(m.error || m));
  return m.result;
}

test("fake browser: CLI, hello, open/scan/fill/upload/click flows, frames, navigation, reconnect, SIGTERM", { skip, timeout: 120000 }, async () => {
  const fixtures = await h.startFixtureServer();
  let backend = await startBackend();
  const port = backend.port;
  const child = spawn(process.execPath, [path.join(__dirname, "fake_browser.cjs"), "--url", `ws://127.0.0.1:${port}/bridge`, "--token", TOKEN], { stdio: ["ignore", "pipe", "pipe"] });
  let stdout = "", stderr = "";
  child.stdout.on("data", d => { stdout += d; });
  child.stderr.on("data", d => { stderr += d; });
  try {
    await h.waitFor(() => stdout.includes("FAKE_BROWSER_READY"), 10000);
    assert.match(stdout, /FAKE_BROWSER_READY/);
    await backend.waitHello(1);
    assert.equal(backend.hellos[0].browser, "fake-firefox");
    assert.deepEqual(backend.hellos[0].owned, []);

    // ping
    const pong = new Promise(r => backend.pending.set("ping", r));
    backend.sock.send(JSON.stringify({ type: "ping", ts: 42 }));
    assert.equal((await pong).ts, 42);

    // greenhouse: open -> scan -> fill -> upload -> click -> confirmation
    const opened = await ok(backend.command("app-gh", "open", { url: fixtures.base + "/greenhouse/index.html" }));
    assert.equal(opened.title.length > 0, true);
    assert.equal((await ok(backend.command("app-gh", "wait_ready", { timeout_ms: 5000, quiet_ms: 200 }))).ready, true);
    let s = await ok(backend.command("app-gh", "scan", { include_text: true, max_text: 500 }));
    assert.equal(s.ats, "greenhouse");
    assert.equal(s.page_kind, "form");
    assert.ok(s.text.length > 0 && s.text.length <= 500);
    const id = label => { const f = h.field(s, label); assert.ok(f, label); assert.match(f.id, /^0:\d+$/); return f.id; };
    const fill = await ok(backend.command("app-gh", "fill", { items: [
      { field_id: id("First Name"), value: "Grace" },
      { field_id: id("Last Name"), value: "Hopper" },
      { field_id: id("Email"), value: "grace@example.com" },
      { field_id: id("Location (City)"), value: "New York, New York, United States" },
      { field_id: id("Are you legally authorized to work in the United States?"), value: "Yes" },
      { field_id: id("Will you now or in the future require sponsorship"), value: "No" },
      { field_id: id("Why do you want to work at Fixture Robotics?"), value: "Compilers for robots." }
    ] }));
    assert.ok(fill.results.every(r => r.ok), JSON.stringify(fill.results));
    const up = await ok(backend.command("app-gh", "upload", { field_id: id("Resume/CV"), file: { name: "cv.pdf", mime: "application/pdf", base64: h.PDF_B64 } }));
    assert.deepEqual(up.files, ["cv.pdf"]);
    // another app can't see app-gh's tab
    const other = await backend.command("app-other", "scan", {});
    assert.equal(other.error.code, "NO_OWNED_TAB");
    await ok(backend.command("app-gh", "click", { element_id: h.button(s, "Submit Application").id }));
    await h.waitFor(() => fixtures.submissions.length === 1, 5000);
    await ok(backend.command("app-gh", "wait_ready", { timeout_ms: 3000, quiet_ms: 150 }));
    s = await ok(backend.command("app-gh", "scan", {}));
    assert.equal(s.page_kind, "confirmation");
    assert.equal(fixtures.submissions[0].fixture, "greenhouse");
    assert.equal(fixtures.submissions[0].data.location, "New York, New York, United States");
    const shot = await ok(backend.command("app-gh", "screenshot", { format: "png" }));
    assert.match(shot.data_url, /^data:image\/png;base64,/);

    // lever: anchor navigation emulation through click_text
    await ok(backend.command("app-lever", "open", { url: fixtures.base + "/lever/index.html", strategy: "tab" }));
    const ct = await ok(backend.command("app-lever", "click_text", { text: "Apply for this job" }));
    assert.match(ct.element_id, /^0:\d+$/);
    assert.equal(ct.href, fixtures.base + "/lever/apply.html");
    await ok(backend.command("app-lever", "wait_ready", { timeout_ms: 8000, quiet_ms: 150 }));
    s = await ok(backend.command("app-lever", "scan", {}));
    assert.equal(s.url, fixtures.base + "/lever/apply.html");
    assert.equal(s.page_kind, "form");
    assert.ok(backend.events.some(e => e.event === "tab_navigated" && e.app_id === "app-lever" && /apply\.html$/.test(e.data.url)));

    // icims: same-origin iframe gets its own frame id; fields are routed there
    await ok(backend.command("app-icims", "open", { url: fixtures.base + "/icims/index.html" }));
    await ok(backend.command("app-icims", "wait_ready", { timeout_ms: 8000, quiet_ms: 150 }));
    s = await ok(backend.command("app-icims", "scan", {}));
    assert.equal(s.frames, 2);
    assert.equal(s.page_kind, "form");
    const email = h.field(s, "Email Address");
    assert.match(email.id, /^1:\d+$/);
    assert.equal(email.frame, 1);
    await ok(backend.command("app-icims", "fill", { items: [{ field_id: email.id, value: "grace@example.com" }] }));
    await ok(backend.command("app-icims", "click", { element_id: h.button(s, "Next").id }));
    s = await ok(backend.command("app-icims", "scan", {}));
    assert.ok(h.field(s, "First Name"));
    const txt = await ok(backend.command("app-icims", "extract_text", { max_chars: 5000 }));
    assert.match(txt.text, /Fixture Health Careers[\s\S]*Candidate Profile/);

    const owned = await ok(backend.command(null, "list_owned", {}));
    assert.deepEqual(owned.owned.map(o => o.app_id).sort(), ["app-gh", "app-icims", "app-lever"]);
    assert.deepEqual((await ok(backend.command("app-lever", "close", {}))), { ok: true });

    // backend restart: fake browser reconnects and re-sends hello with ownership
    await backend.close();
    backend = await startBackend(port);
    await backend.waitHello(1);
    assert.deepEqual(backend.hellos[0].owned.map(o => o.app_id).sort(), ["app-gh", "app-icims"]);
    assert.equal(stdout.match(/FAKE_BROWSER_READY/g).length, 1);
    s = await ok(backend.command("app-gh", "scan", {}));
    assert.equal(s.page_kind, "confirmation");
  } finally {
    child.kill("SIGTERM");
    const code = await new Promise(r => child.on("exit", c => r(c)));
    assert.equal(code, 0, stderr);
    await backend.close();
    await fixtures.close();
  }
});

test("fake browser rejects nothing silently: bad token is refused by the server and retried", { skip, timeout: 20000 }, async () => {
  const backend = await startBackend();
  const child = spawn(process.execPath, [path.join(__dirname, "fake_browser.cjs"), "--url", `ws://127.0.0.1:${backend.port}/bridge`, "--token", "wrong"], { stdio: ["ignore", "pipe", "pipe"] });
  try {
    await h.waitFor(() => backend.badTokens >= 2, 8000);
    assert.ok(backend.badTokens >= 2, "reconnect attempts");
    assert.equal(backend.hellos.length, 0);
  } finally {
    child.kill("SIGTERM");
    await new Promise(r => child.on("exit", r));
    await backend.close();
  }
});
