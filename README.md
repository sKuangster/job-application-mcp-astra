# ApplyPilot

Autonomous, auditable internship applications through **your real Firefox**, with
**Astra (ChatGPT) as the brain** over MCP and a local engine as the hands.

> "Apply to every open internship I am eligible for."

ApplyPilot reads the [SimplifyJobs Summer 2027 list](https://github.com/SimplifyJobs/Summer2027-Internships),
follows each job's **Simplify link**, checks eligibility permissively, picks the right resume,
opens the application in a dedicated Firefox window, fills everything it can from your
source-of-truth files, asks Astra **only** for questions that need judgement (batched across
all running applications), validates the whole application, submits it, proves the
submission succeeded, and records exactly what happened.

```
Astra / ChatGPT ──MCP──►  applypilot backend (Python, 127.0.0.1:8765)
                          ├─ persistent queue + state machine (SQLite, WAL)
                          ├─ prep workers  (Simplify page, eligibility, resume)    ← HTTP only
                          ├─ browser workers (one Firefox window each)             ← via bridge
                          ├─ reasoning queue (dedup + batching for Astra)
                          ├─ research cache, answer memory, audit log
                          └─ HTTP API + dashboard
Firefox extension ◄──WebSocket──┘   DOM engine + ATS adapters in each owned window
```

Contents: [Quick start](#quick-start-windows) · [Configuration](#configure-your-profile) ·
[Firefox extension](#install-the-firefox-extension) · [Connect Astra](#connect-astra-chatgpt) ·
[Running](#running) · [Safety model](#safety-model) · [History & debugging](#history-debugging-and-logs) ·
[Concurrency](#concurrency) · [Tests](#tests) · [Extending](#extending) · [Troubleshooting](#troubleshooting) ·
[Architecture](docs/ARCHITECTURE.md) · [Protocol](docs/PROTOCOL.md)

---

## Quick start (Windows)

Prerequisites: **Python 3.11+** (3.12 recommended, "Add to PATH"), **Firefox**, optionally
**Node.js 20+** (only for extension tests/packaging) and **cloudflared** (only for ChatGPT).

```powershell
git clone https://github.com/sKuangster/job-application-mcp-astra
cd job-application-mcp-astra
.\scripts\setup.ps1          # venv + deps + .env tokens + database
# edit config\candidate.local.yaml, config\preferences.yaml, profile\profile.md, templates\*.md
# copy your resume PDFs into resumes\
.\.venv\Scripts\applypilot.exe doctor    # tells you exactly what is still missing
.\scripts\run.ps1 -Pair      # start the backend; Firefox extension pairs itself
```

macOS/Linux/WSL: `./scripts/setup.sh`, `./scripts/run.sh --pair`, `./scripts/test.sh`.

Everything after this is: install the extension once, connect Astra once, then talk to Astra.

---

## Configure your profile

All candidate information is **data**. Nothing about you is hard-coded, and nothing is guessed.

| File | What belongs there |
| --- | --- |
| `config/candidate.yaml` | **Facts**: identity, address, links, education, GPA, graduation date, work authorization *per country*, citizenship, age, employers, clearance, explicit `factual_answers`. `null` = unknown. |
| `config/preferences.yaml` | **Choices**: role/company filters, relocation/travel/start date/salary, "how did you hear", consents, **EEO answers**, resume mapping, account settings, research sources, **eligibility rules**. |
| `config/settings.yaml` | **System tuning**: auto-submit, concurrency, timeouts, retries, thresholds, logging. |
| `profile/profile.md` | Long-form narrative: education, employment, projects, leadership, teaching, activities, accomplishments, interests, and a **story bank** for essays. |
| `templates/*.md` | Your guidance for written answers (why company, why role, project, leadership, teamwork, conflict, challenge, failure, other, cover letter). See [templates/README.md](templates/README.md). |
| `resumes/` | Your resume files. See [resumes/README.md](resumes/README.md). |

Keep real data out of git: every file has a git-ignored twin that is merged on top
(`config/candidate.local.yaml`, `config/preferences.local.yaml`, `config/settings.local.yaml`,
`profile/profile.local.md`, `templates/<name>.local.md`). A fully filled **fictional** example
is in [`config/examples/candidate.example.yaml`](config/examples/candidate.example.yaml).

Required before autopilot will start (`applypilot doctor` lists what is missing):
name, email, phone, university, degree level, graduation date, US work authorization, and at
least one resume. Everything else is asked for only when an application needs it.

### candidate.yaml essentials

* **Work authorization is per country** (`work_authorization.US`, `.CA`, …). A Canadian question
  is never answered from the US block. `requires_sponsorship_future` answers the common
  "now **or in the future**" question.
* Set `experience.employers` to every employer and `employment_history_complete: true` so
  "Have you worked for X before?" is answered automatically (exact company match).
* `factual_answers` covers recurring factual questions the standard fields don't
  (relatives at the company, non-competes, …), optionally scoped to one company.

### Resumes

Name files so the category is visible (`Jane_Doe_Resume_SWE.pdf`, `Jane_Doe_Resume_ML.pdf`) or map
categories explicitly in `preferences.yaml → resumes.mappings`; set `resumes.default`. Selection is
deterministic and recorded with its reason on every application.

### EEO

`preferences.yaml → eeo` holds your self-identification answers. They are **never inferred**.
When a value is `null`, `optional_unknown` decides: `decline` (choose "prefer not to say", the
default), `skip` (leave optional questions blank) or `needs_user`.

### Eligibility (permissive by design)

Only **structural** incompatibilities skip a job, and only when your fact contradicts an
explicit requirement. Built-in rules: Simplify's "U.S. Citizenship is Required" / "Does Not Offer
Sponsorship" tags (checked against your facts), graduate-only roles (listing tag **and** posting
text must agree), explicit graduation windows (parsed deterministically; vague wording goes to
Astra), high-school-only programs. Your own rules are regex + one candidate fact, as data
(`preferences.yaml → eligibility_rules`). Skills, "preferred" qualifications and years of
experience never cause a skip. Unknown facts never cause a skip by default.

---

## Install the Firefox extension

The extension lives in `firefox/` and needs no build.

**Option A, quickest (temporary, until Firefox restarts):**
1. Open `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…**
2. Select `firefox\manifest.json`.

**Option B, permanent (recommended for daily use):** Firefox release builds only keep signed
add-ons. Signing is free and private ("unlisted"):
1. Create API credentials at <https://addons.mozilla.org/developers/addon/api/key/>.
2. Put them in `.env` as `WEB_EXT_API_KEY` / `WEB_EXT_API_SECRET`.
3. `.\scripts\package-extension.ps1 -Sign` → install the produced `dist\*.xpi` by dragging it into Firefox.

(Firefox Developer Edition/Nightly can instead set `xpinstall.signatures.required=false` and
install `.\scripts\package-extension.ps1`'s unsigned zip.)

**Pair it with the backend:** start the backend with `.\scripts\run.ps1 -Pair` (or run
`applypilot pair` while it is running). The extension fetches its token automatically within a
few seconds; the pairing window answers only extension origins and closes after one pairing.
Alternatively open the extension's **Options** and paste the token printed by `applypilot token`.

The toolbar button opens the live popup (status, workers, queue, today's counts, items needing
you, Pause/Resume/Stop, Open Dashboard). A red badge counts applications needing attention, and a
desktop notification appears when one starts waiting (for example, a CAPTCHA).

Each application gets **its own Firefox window**, owned by exactly one application at a time, so
workers never touch each other's pages. Your normal browsing stays untouched. Existing Firefox
logins and cookies are reused, which is why it runs in your real profile.

---

## Connect Astra (ChatGPT)

The backend serves MCP over **streamable HTTP** at `/mcp` and as a **stdio** proxy
(`applypilot mcp`). Both expose identical tools.

### ChatGPT (developer mode / custom connector)

ChatGPT connects to remote HTTPS MCP servers, so expose the backend with a tunnel. Tunnelled
requests can only reach `/mcp`, never the dashboard API, bridge or pairing.

1. `.\scripts\run.ps1`
2. In a second terminal: `.\scripts\tunnel.ps1` (Cloudflare quick tunnel; `winget install Cloudflare.cloudflared`).
   It prints `https://<name>.trycloudflare.com/mcp/<APPLYPILOT_MCP_TOKEN>`.
3. ChatGPT → **Settings → Apps & Connectors → Advanced settings → enable Developer mode** →
   **Create connector**: name `ApplyPilot`, MCP server URL = the URL above, authentication
   **No authentication** (the secret is in the URL; keep it private). Trust it.
4. In a chat, enable the ApplyPilot connector and say:
   *"Use ApplyPilot. Apply to every open internship I am eligible for."*

Rotate the URL secret any time with `applypilot setup --rotate` (re-pair the extension afterwards).

### Codex CLI / other stdio MCP clients

```toml
# ~/.codex/config.toml
[mcp_servers.applypilot]
command = "C:\\path\\to\\job-application-mcp-astra\\.venv\\Scripts\\applypilot.exe"
args = ["mcp"]
```

Clients that support HTTP with headers can use `http://127.0.0.1:8765/mcp` with
`Authorization: Bearer <APPLYPILOT_TOKEN>`.

### MCP tools

| Tool | Purpose |
| --- | --- |
| `get_status` | Compact status plus a `next_step` hint for the agent loop |
| `start_autopilot(mode)` | Sync jobs and start workers: `backlog`, `new_jobs`, or `continuous` |
| `pause_autopilot` / `resume_autopilot` / `stop_autopilot` | Safe control |
| `sync_jobs(mode)` | Ingest the repository and queue unprocessed jobs |
| `get_reasoning_batch(wait_seconds)` | **The work loop.** Questions only Astra can answer, batched and deduplicated across applications, with templates, profile, job context and cached research |
| `submit_reasoning(results)` | Answers, validated (options, limits, no invented credentials); accepted answers resume applications |
| `get_candidate_context` | Profile, templates, preferences |
| `save_company_research` | Cache research with sources and timestamps |
| `search_applications`, `get_application_log` | History and the exact audit trail |
| `application_action` | `resume` / `retry` / `skip` / `submit` / `open` / `confirm_submitted` |
| `provide_answer` | Supply a fact **the user told you** for a NEEDS_USER question |
| `apply_to_url` | Queue one specific posting |
| `inspect_page`, `get_page_screenshot`, `perform_page_action` | Bounded visual/DOM fallback on parked applications |
| `get_queue`, `get_metrics` | Queue and throughput/cost metrics |

Astra's loop is: `get_reasoning_batch` → answer all items → `submit_reasoning` → repeat.
Each round-trip covers many questions across all running applications. Identical questions in
concurrent applications become one item, and company research is fetched once per company.

**Fully unattended (optional):** set `reasoning.provider: openai` in `settings.yaml` and
`OPENAI_API_KEY` in `.env`; the backend then answers its own reasoning queue through the API
(same validation), with no chat session needed.

---

## Running

```powershell
.\scripts\run.ps1                         # backend + workers + dashboard + MCP
.\.venv\Scripts\applypilot.exe status      # terminal status
.\.venv\Scripts\applypilot.exe sync        # ingest without starting (queues backlog)
.\.venv\Scripts\applypilot.exe start --mode backlog
.\.venv\Scripts\applypilot.exe pause | resume | stop
```

Dashboard: the extension's **Open Dashboard**, or `http://127.0.0.1:8765/ui/dashboard.html#token=<APPLYPILOT_TOKEN>`.

### Backlog mode vs new-jobs mode

* **Backlog** (`start_autopilot(mode="backlog")`): queues every **open** job that has never been
  considered and works through all of them. The first sync is recorded as the **baseline**.
* **New jobs** (`mode="new_jobs"`): re-reads the repository every `poll_interval_seconds` and
  queues only jobs first seen **after** the baseline.
* **Continuous**: backlog, then automatically switches to new-jobs polling.

Jobs keep durable identities (Simplify id, ATS posting id, normalized URLs, company+title+location
aliases), so formatting changes never create duplicates. Ineligible jobs are re-checked only if
their listing data materially changes. Submitted or skipped jobs are never reconsidered.

### Safe first run (one application)

1. Keep `automation.auto_submit: false` (the default).
2. `applypilot apply-url "https://simplify.jobs/p/<id>"` (or Astra `apply_to_url`), then `applypilot start --no-sync`.
3. Answer Astra's questions (or let Astra do it). The application stops at **READY_TO_SUBMIT** with
   its window open. Review every field in Firefox and in the dashboard's Questions & Answers tab.
4. Click **Submit** in the dashboard (or `application_action(submit)`). The validation gate runs again,
   then it submits and records the confirmation evidence.
5. When you trust it, set `auto_submit: true`.

### Pausing, stopping, resuming

* **Pause**: no new work is claimed. In-flight steps finish at a safe point, and **no further
  submissions start**. State is persisted.
* **Stop**: same, and workers go idle with leases released.
* **Resume**: continues from the persisted state of every application (completed steps are not redone).
* Killing the process is also safe. On restart, expired leases are recovered, tabs are reopened if
  needed, and an application that was mid-submission is **never re-submitted blindly**: it is
  checked for confirmation, and otherwise marked SUBMISSION_UNCONFIRMED for you to verify.

### CAPTCHA workflow

ApplyPilot **never** solves or bypasses CAPTCHAs. When a visible challenge appears (invisible
reCAPTCHA badges are ignored), only that application moves to **NEEDS_USER**. You get a
notification and the badge turns red; other workers keep going. Open the application (dashboard →
**Open in Firefox**), complete the challenge, and either press **Resume** or just wait: the watcher
notices the challenge is gone and resumes automatically. If the CAPTCHA appeared **after** the submit
click, ApplyPilot never clicks Submit again: solve it, click the site's Submit button yourself if the
form is still shown, then press **Resume** so the confirmation is recorded (or **Mark submitted**).

### Other interventions

* **Missing fact**: the dashboard shows the exact question and the config path to fill. Answer it
  inline (optionally "remember for future applications"), or edit the YAML and Resume.
* **Login / verification**: set `APPLYPILOT_ACCOUNT_PASSWORD` (and IMAP for automatic email codes and
  links). Otherwise sign in or verify manually and press Resume.
* **Validation errors** that persist after automatic retries (including alternate value formats)
  are shown with the site's messages.

---

## Safety model

* **Facts only from source of truth.** Every factual, preference and EEO answer cites its config path.
  Astra's answers are validated: options must exist, limits are enforced, and placeholders,
  institutions or GPAs not in your profile are rejected.
* **Answer classification** (FACTUAL, PREFERENCE, EEO, WRITTEN, JOB_SPECIFIC, CONSENT, FILE,
  UNKNOWN) decides the strategy. Answer memory is **never** used for facts, and company-specific
  prose is never reused for another company.
* **Submission gate** (all must pass): no visible CAPTCHA, no validation errors, required fields
  filled, values on the page match what was planned, factual answers still match the source of
  truth, resume attached, no unresolved questions, final stage reached, not previously submitted.
* **Only the engine submits**: an "Apply"/"Submit"/"Finish" button on a filled form always goes through
  the gate. Astra's page-help actions and the MCP `perform_page_action` tool refuse anything that could
  send the application.
* **Double-submission protection**: an atomic submission intent (unique per job) plus an
  equivalence check (same company and role, or same ATS posting id, across all applications).
  Ambiguous duplicates are skipped and logged, not submitted.
* **SUBMITTED means proven**: a confirmation page or message must be detected. Otherwise the result
  is SUBMISSION_UNCONFIRMED with a screenshot.
* **Secrets**: tokens and passwords live in `.env` only. They are redacted from events, logs and MCP
  responses, and fill payloads are logged as `[filled]`. The backend binds 127.0.0.1, every channel
  needs the token, the bridge also requires a `moz-extension://` origin, and tunnels can reach only `/mcp`.

---

## History, debugging and logs

* **Dashboard → Applications**: search and filter by company, role, state, date, ATS, resume, or
  failures only. Open one (for example *NVIDIA – Machine Learning Intern*) to see Summary,
  Eligibility (rules and evidence), Resume (and why), **Questions & Answers** (exact submitted value,
  method `fact`/`preference`/`eeo`/`reused`/`generated`/`user`, and source such as
  `candidate.yaml → work_authorization.US.authorized`), Research (sources), Browser actions,
  Errors/Retries, Submission evidence, and the full chronological **Timeline**.
* `applypilot show <application_id>` prints the same audit in the terminal. Astra can call `get_application_log`.
* Logs: `data/logs/applypilot.jsonl` (structured, one JSON per line, with application/job/worker ids)
  and `data/logs/errors.jsonl`. Dashboard → Logs filters them. Use `--log-level DEBUG` and
  `logging.verbose_browser: true` for maximum detail.
* Screenshots on failures and confirmations: `data/artifacts/<application_id>/`.
* Metrics (dashboard overview / `get_metrics`): time per application, page waits, reasoning batches
  and items, research runs and cache hits, answers by method, retries.

### Database

SQLite at `data/applypilot.sqlite3` (WAL). Back it up while running with `applypilot backup`
(writes `data/backups/…`). The schema is migrated automatically on start (`src/applypilot/migrations`).
`data/` is git-ignored.

---

## Concurrency

Defaults are conservative: **2 browser workers**, 3 prep workers, **1 application per
domain/tenant**, and at most 4 open application windows.

| Setting | Effect |
| --- | --- |
| `max_concurrent_applications` | Firefox windows driven at once. More throughput, more RAM/CPU (about 150–300 MB per window), and more pages to watch. |
| `per_domain_concurrency` | Parallel applications per company/ATS tenant (Workday tenant, Greenhouse board). Keep at 1 to avoid session or account conflicts and rate limits. |
| `max_open_application_tabs` | Caps windows held by applications waiting on Astra or you. |
| `max_prep_workers` | Simplify page and eligibility throughput (no browser). |

Applications waiting for Astra **release their worker** (the window stays open), so workers keep
filling other applications. Astra usage grows with the number of *novel* questions, not with
concurrency: answers are deduplicated and batched. Failing domains cool down automatically, and
retries use exponential backoff with jitter. Debugging stays tractable because every event carries
application, job and worker ids, and each application's timeline is isolated.

---

## Tests

```powershell
.\scripts\test.ps1            # ruff + mypy + pytest (unit + integration) + JS tests + extension lint
.\scripts\test.ps1 -Quick     # unit tests only
```

* `tests/unit`: eligibility, graduation windows (real posting sentences), question classification and
  resolution, option mapping, answer memory rules, resume selection, identity and duplicates, state
  machine, database (atomic claims under thread races, lease expiry, fencing, duplicate-submission
  race, write contention), reasoning validation and batching, research single-flight, API/MCP auth,
  tunnel isolation, pairing.
* `tests/integration/test_e2e_fixtures.py`: full system (backend, workers, bridge, **the real
  extension code** in a jsdom fake browser) against mock **Greenhouse, Lever, Ashby, Workday (account
  creation), iCIMS (iframe), SmartRecruiters (shadow DOM), custom multi-page, validation-failure,
  email-verification, CAPTCHA, and closed** postings with a simulated Astra. Asserts exactly-once
  submission, posted values, provenance, duplicates, pause/resume, failure isolation, and restart recovery.
* `tests/integration/test_real_firefox.py`: the same against **real headless Firefox** with the real
  extension (runs when Firefox is found; set `APPLYPILOT_FIREFOX=C:\Program Files\Mozilla Firefox\firefox.exe`).
* `tests/js`: DOM engine on every fixture, bridge core (ownership isolation), fake browser, dashboard/popup UI.

---

## Extending

* **New ATS**: add an adapter object to `firefox/content/adapters.js`. Implement `matches()` (hostname
  or DOM markers) and any hooks you need: `labelFor`, `extraFields` (custom widgets), `classifyButton`,
  `pageKind`, `step`, `fillCustom`, `errors`. Shared primitives (visibility, labels, comboboxes,
  React-safe value setting) come from the DOM engine, and unknown sites use the generic engine. Add a
  fixture under `tests/fixtures/ats/<name>/` and a test in `tests/js/dom-engine.test.cjs`, plus an
  entry in `tests/integration/test_e2e_fixtures.py`.
* **New deterministic question**: add an entry to `config/question_catalog.yaml` (same format as
  `src/applypilot/data/question_catalog.yaml`; see `config/examples/question_catalog.example.yaml`).
* **New template**: add `templates/<name>.md` and reference it with `written: <name>` in a catalog entry.
* **New eligibility rule**: `preferences.yaml → eligibility_rules`.
* **New email provider**: implement `EmailProvider` in `src/applypilot/accounts.py`.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `doctor` says configuration incomplete | Fill the listed paths; autopilot refuses to start without them. |
| Popup: "Backend offline" | Start `.\scripts\run.ps1`; check port 8765 is free. |
| Popup: "Token rejected" / extension not connected | `applypilot pair` (or paste `applypilot token` output into Options). |
| ChatGPT cannot reach the connector | The tunnel must be running; use the `/mcp/<MCP token>` URL; tunnels reach only `/mcp`. |
| An application loops on validation | Dashboard → Errors shows the site's messages; fix the fact or answer and Resume. |
| SUBMISSION_UNCONFIRMED | Check the window or your email; click **Mark submitted** in the dashboard (or Astra `confirm_submitted`) if it went through. |
| Extension: "another Firefox profile is already connected" | Only one Firefox may drive a backend; remove/disable the extension in the other profile. |
| "another ApplyPilot backend is running" | Only one backend per database; stop the other one (the lock expires within 60 s of a crash). |
| Workday asks for an account | Set `APPLYPILOT_ACCOUNT_PASSWORD` in `.env` and restart. |
| Verification email not processed | Configure `APPLYPILOT_IMAP_*` (app password), or verify manually and Resume. |

## Known limitations

* No CAPTCHA solving, by design.
* Real ATSs change their DOM. The adapters target current Greenhouse, Lever, Ashby, Workday, iCIMS
  and SmartRecruiters patterns but were verified against local fixtures, not live submissions. Start
  with `auto_submit: false`.
* Sites that require trusted (OS-level) input events, or that run inside closed shadow roots or
  cross-origin frames that refuse content scripts, fall back to the visual tools or NEEDS_USER.
* ChatGPT must stay in a session to answer reasoning batches (or use `reasoning.provider: openai`).
