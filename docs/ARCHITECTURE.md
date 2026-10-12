# ApplyPilot architecture

## Principle: Astra is the brain, not the hands

The local system does every deterministic thing: ingestion, identity and duplicates,
eligibility, resume choice, navigation, field detection, deterministic answers, uploads,
validation and submission. Astra (ChatGPT over MCP) is consulted only for semantic work:
novel questions, personalized prose, ambiguous requirements, option wording that does not map,
and unrecognized pages. Those requests are batched and deduplicated.

```
                 ┌──────────────────────────── applypilot backend (one process) ───────────────────────────┐
 SimplifyJobs    │  ingest/listings.py ──► jobs (+aliases) ──► applications (= persistent queue)          │
 listings.json ─►│                                                   │                                    │
                 │   prep pool (HTTP):  QUEUED → CHECKING_ELIGIBILITY → READY / INELIGIBLE / SKIPPED       │
 simplify.jobs ─►│     Simplify page, eligibility.py (+gradwindow.py), resumes.py                         │
                 │   browser pool:      READY → OPENING → APPLYING → (ACCOUNT_REQUIRED | VERIFYING_EMAIL) │
                 │                      → FILLING ⇄ ANSWERING → VALIDATING → SUBMITTING → SUBMITTED       │
                 │     engine/runner.py (one handler per state) · engine/validation.py (submission gate)   │
                 │   questions/: catalog → resolver (facts/prefs/EEO/derived/memory) → reasoning items     │
                 │   reasoning.py (dedupe, batch, validate) ◄──── MCP get_reasoning_batch/submit_reasoning │◄── Astra
                 │   research.py (single-flight, cached) · accounts.py (creds from env, IMAP verification) │
                 │   bridge.py ◄═══ WebSocket /bridge ═══► Firefox extension (background + content scripts)│
                 │   api.py: /api/* (dashboard, popup, stdio MCP proxy) · /mcp (streamable HTTP)          │
                 └──────────────────────── SQLite (WAL): db.py + migrations/ ───────────────────────────────┘
```

## Components

| Module | Responsibility |
| --- | --- |
| `config.py` | Load and validate `candidate.yaml`, `preferences.yaml`, `settings.yaml` (+ `.local` overlays), profile, templates; readiness report. |
| `db.py`, `migrations/` | All persistence: short `BEGIN IMMEDIATE` transactions with retry, atomic claims, leases with fencing tokens, audit events, answers, memory, reasoning queue, research cache, locks, domain health, accounts, submissions, model calls, ingest runs. |
| `state.py` | States, allowed transitions, claim priorities. |
| `identity.py` | URL normalization, ATS detection, ATS posting keys, per-domain concurrency keys, job aliases. |
| `ingest/listings.py` | `listings.json` (primary) / README tables (fallback), ETag checkpoints, term/category filters. |
| `ingest/simplify_page.py` | Parse the Simplify job page (`__NEXT_DATA__`), resolve the Simplify Apply redirect to the ATS URL. |
| `eligibility.py`, `gradwindow.py`, `data/default_rules.yaml` | Permissive, rule-as-data eligibility; deterministic graduation windows; Astra review for ambiguous wording. |
| `resumes.py` | Category classification and file mapping. |
| `questions/catalog.py`, `data/question_catalog.yaml` | Classify each field into FACTUAL / PREFERENCE / EEO / WRITTEN / JOB_SPECIFIC / CONSENT / FILE / UNKNOWN and map it to a source. |
| `questions/derived.py` | Facts computed from the source of truth (per-country authorization, sponsorship now/future, phone formats, graduation parts, prior employment, …). |
| `questions/options.py` | Map a truthful value onto the field's options and format (yes/no, synonyms, decline, dates). |
| `questions/resolver.py` | The resolution hierarchy (below). |
| `reasoning.py` | Reasoning queue: dedup across applications, batches with shared context, answer validation, unblocking, memory write-back. |
| `research.py` | Research decision, single-flight per company, Simplify company profile + official site pages, TTL cache, Astra-supplied research. |
| `accounts.py` | Credentials from env/keyring (never stored), per-tenant account keys, pluggable email verification (IMAP). |
| `bridge.py` | WebSocket bridge: command futures, timeouts, ownership tracking, reconnects. |
| `engine/scheduler.py` | Worker pools, heartbeats, failure isolation and backoff, domain cool-down, control (start/pause/stop), ingestion modes, CAPTCHA watcher, housekeeping. |
| `engine/runner.py` | Per-state handlers, page loop, filling, uploads, accounts, verification, submission protocol. |
| `engine/validation.py` | Submission gate. |
| `service.py`, `api.py`, `mcp_server.py`, `cli.py` | Facade, HTTP API + WS + MCP, MCP tools (one table for HTTP and stdio), command line. |
| `providers.py` | Optional unattended OpenAI-compatible reasoning provider. |
| `firefox/background/` | `bridge-core.js` (transport-agnostic router: ownership, per-app mutex, frame routing, scan merge) + `background.js` (Firefox driver, WebSocket, pairing, badge/notifications). |
| `firefox/content/` | `dom-engine.js` (scan/fill/upload/click, shadow DOM, custom widgets, page-kind and CAPTCHA detection) + `adapters.js` (Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters, Simplify, generic). |
| `firefox/ui/` | Popup, dashboard (history/audit/attention/logs/settings), options. |

## Answer resolution hierarchy

1. File fields → selected resume (or cover letter rendered to PDF, or a supplied document).
2. Explicit `candidate.factual_answers` and user-provided answers (dashboard / `provide_answer`).
3. Catalog → **candidate facts**, **preferences**, **EEO config**, **derived facts** (deterministic, cited by path).
4. **Answer memory** for non-factual classes: high similarity, same options, same profile hash,
   limits respected, never company-specific prose for another company.
5. Optional questions without a source are **skipped** (saves model calls), unless the catalog marks them worth answering.
6. Otherwise a **reasoning item** for Astra (templates + profile + job + research), deduplicated.
7. Required facts that cannot be established → **NEEDS_USER** with the exact config path.

When a widget's real options do not contain the planned wording, the value is re-mapped against
the live options. If that is ambiguous, Astra gets a constrained "choose the option that states
this known fact" item.

## Concurrency model

* **Queue = `applications` table.** Claims are a single `BEGIN IMMEDIATE` transaction that picks
  the highest-priority claimable row and writes a lease (`worker_id`, random `lease_token`,
  `lease_until`). Two workers can never hold the same application.
* **Fencing**: every state write checks the lease token, so a worker whose lease expired (crash or
  stall) can't write anything (`LeaseLost`).
* **Heartbeats** extend leases. Expired leases are reclaimable, which is how a crashed worker's job
  is recovered. On startup, leases from a dead process are released, guarded by a backend lock so
  only one backend runs per database.
* **Parking**: an application waiting on Astra is released with `blocked_on='reasoning'` and keeps
  its window. The reasoning service unblocks it when all its items are answered, and housekeeping
  re-checks to close any race. The OpenAI provider requests strict schema output, validates complete
  item coverage and action semantics before mutation, and performs two bounded repairs. Exhaustion
  completes the item as failed and parks the application in `NEEDS_USER` with recovery diagnostics.
* **CAPTCHA parking**: `WAITING_FOR_CAPTCHA` persists the page/resume evidence and releases the
  worker lease. A separate watcher only scans the DOM; once the challenge is absent and the page is
  recognizable, it transitions back to the persisted resume state. Long waits remain recoverable.
* **Manual-login parking**: Watch Mode focuses the owned tab and releases the lease. The same
  read-only watcher recognizes a successful login/navigation and resumes the same application.
  Passwordless/passkey pages take this path directly rather than entering page reasoning.
* **Domain limits and cool-down** are part of the claim query; consecutive failures cool a domain down.
* **Tab budget**: new browser work isn't claimed when too many windows are held.
* **Submissions**: `reserve_submission` atomically fences the lease, rejects existing or equivalent
  submissions, inserts the intent (`submissions.job_id UNIQUE`) and moves the state to SUBMITTING.
  `submit_clicked_at` is written **before** the click, so a crash after the click never triggers a
  second click.
* **SQLite**: WAL, short transactions, jittered retry on `database is locked`. All access goes
  through `Database`, so another engine could replace it.

## Browser isolation

The extension maps `app_id → {tab_id, window_id}`, persisted in `storage.local`. Commands carry
the app id and are routed only to that tab's frames, and commands for one application are
serialized. Element ids embed the frame id. CAPTCHA provider frames are never injected or messaged.

## Observability

`application_events` is the per-application, ordered audit trail (component, type, level,
message, redacted data, worker id). Structured JSON logs carry application, job and worker ids
via context variables. `model_calls` counts reasoning batches, items and characters (and tokens
for the API provider). Per-application metrics record page wait time, retries, deterministic vs
generated answers, and active seconds.
