# ApplyPilot browser-side tests

Plain Node (22+) with `node:test`; no npm scripts needed. Put Node on the PATH first:

```sh
export PATH=$HOME/.local/node/bin:$PATH
```

## Run everything

```sh
node --test tests/js/            # resolves to tests/js/index.js, which loads every *.test.cjs
node --test tests/js/*.test.cjs  # equivalent, one process per file
```

| file | what it covers |
| --- | --- |
| `dom-engine.test.cjs` | `firefox/content/dom-engine.js` + `adapters.js` against every fixture in `tests/fixtures/ats/` loaded in jsdom from a local static server (scan, labels, required flags, options, every fill type, uploads, page kinds, captcha vs. invisible captcha, validation errors, full Workday walk-through, Greenhouse submit with exactly one recorded POST) |
| `bridge-core.test.cjs` | `firefox/background/bridge-core.js` with a fake driver (ownership isolation, per-app mutex, frame-id routing, scan merge priority, tab_closed, persistence, timeouts) |
| `fake-browser.test.cjs` | `fake_browser.cjs` driven over a real WebSocket by a throwaway server (needs the `ws` package from `node_modules`, otherwise skipped) |
| `helpers.cjs` | fixture static server (`POST /__fixture/submit` recording, `GET /__fixture/submissions`) and jsdom page loader |

## Fake browser for backend integration tests

`fake_browser.cjs` runs the real `bridge-core.js` and the real content scripts on jsdom
and connects to the backend bridge like the extension does:

```sh
node tests/js/fake_browser.cjs --url ws://127.0.0.1:8765/bridge --token "$APPLYPILOT_TOKEN" [--log]
```

* Prints `FAKE_BROWSER_READY` on stdout once connected and `hello` was sent (only once).
* Reconnects with backoff (cap 3 s) when the server goes away; exits 0 on SIGTERM/SIGINT.
* Sends no `Origin` header: start the backend with `APPLYPILOT_ALLOW_NO_ORIGIN=1`.
* Emulates anchor navigation (a click result with `href` loads that URL in the same tab);
  form-submit navigation and `location` assignments are not emulated, so fixtures use JS steps.
* Same-origin iframes become frames 1, 2, … and get the content scripts too.
* `screenshot` returns a 1×1 PNG placeholder; `focus` is a no-op.
* `--log` writes diagnostics to stderr (never field values or file contents).

## Fixtures

`tests/fixtures/ats/<name>/index.html` — local imitations of Greenhouse, Lever, Ashby,
Workday, iCIMS, SmartRecruiters, Simplify plus generic/custom, validation, captcha,
verification and closed pages. Each page shows a "LOCAL TEST FIXTURE — NOT A REAL
APPLICATION" banner and posts `{fixture, data, url, ts}` to `/__fixture/submit` on a
successful submission (`tests/fixtures/ats/kit.js`).

## Lint the extension

```sh
node_modules/.bin/web-ext lint --source-dir firefox
```
