# Local research and persuasive writing

ApplyPilot extends the existing research cache, SQLite locks, durable reasoning queue,
answer memory, provider adapters and atomic spending reservations. Browser filling,
submission approval, confirmation, CAPTCHA handoff and test-one scope remain unchanged.

## Stack and verified API

The requested upstream [Perplexica repository](https://github.com/ItzCrazyKns/Perplexica)
now redirects to the maintained Vane project. ApplyPilot retains the configuration name
`perplexica` for compatibility. It uses the official slim image with a separate SearXNG
service and local Ollama. All three images are pinned by digest in
`services/research/compose.yaml`; the source API reference commit is
`348feca3e378fb4157b217724ed508dc707f853f` in `services/research/upstream.json`.

The adapter verifies the maintained [search contract](https://github.com/ItzCrazyKns/Vane/blob/348feca3e378fb4157b217724ed508dc707f853f/docs/API/SEARCH.md):
`GET /api/providers` supplies UUIDs and model keys; `POST /api/search` takes chatModel,
embeddingModel, query, sources, optimizationMode, history and stream. The adapter uses
`sources: [web]`, empty history and non-streaming output. It refuses ambiguous provider
names, missing models, oversized bodies and changed response schemas. It does not invent
older `focusMode` or provider-name request formats.

Image digests and source reference commits are independent pins. Runtime discovery and
the opt-in smoke test validate installed compatibility; pinning alone is not a live test.
Upstream's documented API exposes no crawl-depth or server cancellation parameter.
ApplyPilot bounds its requests, deadlines and independently inspected documents; a timed-out
upstream search may finish internally. Stop the stack to terminate such work.

## Windows 11 / WSL2 installation

1. Install Docker Desktop, enable its WSL2 engine, and enable your distribution under
   Settings → Resources → WSL Integration. Confirm `docker info` works inside WSL.
2. Run ApplyPilot setup (`bash scripts/setup.sh` in WSL or `scripts/setup.ps1` on Windows),
   activate its virtual environment, and fill the ignored personal files. Existing files
   are preserved. Run `applypilot doctor` to check candidate data, resumes and dependencies.
3. In WSL, start the pinned stack:

```bash
bash scripts/research-stack.sh install
bash scripts/research-stack.sh start
```

Windows PowerShell equivalents:

```powershell
.\scripts\research-stack.ps1 -Action install
.\scripts\research-stack.ps1 -Action start
```

This downloads `qwen2.5:7b` and `nomic-embed-text` into the persistent Ollama volume. The
container stack publishes only loopback: Perplexica 3001, SearXNG 8081, Ollama 11435.
ApplyPilot remains on 8765; an existing host Ollama on 11434 is unaffected. Public
candidate/profile files are not mounted into any research container.

4. Open `http://127.0.0.1:3001` and complete upstream's initial setup once. Select the local
   **Ollama** provider, `qwen2.5:7b` for chat, and `nomic-embed-text:latest` for embeddings.
   The environment supplies `http://ollama:11434` and `http://searxng:8080` inside Docker.
   Do not configure paid providers in this research instance. Its configuration persists
   in the named volume. Ensure exactly one configured provider has the name `Ollama`;
   if you rename it, change `research.perplexica.provider_name` accordingly.
5. Create ignored `config/settings.local.yaml`:

```yaml
automation:
  auto_submit: false
reasoning:
  provider: hybrid
  ollama:
    base_url: http://127.0.0.1:11435
    model: qwen2.5:7b
    context_tokens: 16384
    timeout: 300                 # CPU inference can be slow; tune for your machine
  gemini:
    enabled: false
  hybrid:
    premium_enabled: false
    daily_budget_usd: 0          # all paid inference blocked
    local_warmup_patience_seconds: 180
    local_operational_retries: 5
    max_queue_wait_seconds: 600
  writing:
    enabled: true
    local_revision_limit: 2
    semantic_evaluation: true
    fit_synthesis: true
    cloud_share_full_profile: false
    experience_file: config/experiences.local.yaml
research:
  enabled: true
  provider: perplexica           # perplexica | official | none
  perplexica:
    base_url: http://127.0.0.1:3001
    searxng_url: http://127.0.0.1:8081
    provider_name: Ollama
    chat_model: qwen2.5:7b
    embedding_model: nomic-embed-text:latest
    optimization_mode: balanced
    timeout: 120
  max_requests_per_scope: 2
  max_pages: 3
  max_bytes: 2000000
  max_wait_seconds: 300
  retry_seconds: 15
  cache_ttl_seconds: 1209600
  role_ttl_seconds: 259200
  technology_ttl_seconds: 604800
```

6. Run diagnostics and start ApplyPilot:

```bash
bash scripts/research-stack.sh doctor
applypilot doctor
applypilot run
# In another activated terminal, pair Firefox and inspect one imported job:
applypilot pair
applypilot test-one --dry-run --no-sync
applypilot test-one --watch --no-sync
```

`test-one` may interact with the employer site, but final submission remains disabled.
No chat or tunnel is required. After backend restart, explicitly resume stopped control.

### Existing host Ollama

Use `start --host-ollama` in Bash or `-HostOllama` in PowerShell instead. Pull both models
using the host's `ollama pull` command. Set ApplyPilot's reasoning URL to the host address
reachable from WSL, usually `http://127.0.0.1:11434` with mirrored networking. Research
containers use `http://host.docker.internal:11434`. Host Ollama must accept connections
from Docker; configure its listening address and firewall narrowly for that traffic.
Do not expose Ollama or the research UI to the public network. Changing between host
and container modes may leave two upstream providers; remove/rename the unused one in
the UI and recheck discovery.

### Resources and shutdown

A 7B model plus 16K context needs several GB of RAM; 16 GB system RAM is a practical
starting point, with more headroom preferable for Firefox, Docker and embeddings.
Increase Docker/WSL memory allocation if its default is too small. CPU mode works but
can take minutes; NVIDIA GPU use requires a compatible Docker/WSL GPU configuration
and an explicit GPU-enabled Compose override. No GPU is assumed or enabled by this file.
Smaller models and shorter context can reduce resource use, but must be pulled and
configured consistently in ApplyPilot and upstream. Quality checks may then need more revisions.

```bash
bash scripts/research-stack.sh logs
bash scripts/research-stack.sh stop
```

Stop preserves all named volumes. No script removes data or volumes. Perplexica search
history/configuration, SearXNG cache and Ollama models remain outside Git.

## Research, writing and privacy policies

The planner separates company motivation, role motivation, technical motivation,
experience, hypothetical and real behavioral questions. Demographics and facts do
not trigger research. Behavioral prompts retrieve personal evidence locally; role
prompts first use meaningful stored job descriptions. Company and technology prompts
gather scoped web evidence. Company cache is reused across roles; role/technology cache
uses job identity, and fit cache additionally uses candidate/evidence hashes.

Only public company/title research queries reach Perplexica/SearXNG/search engines.
Candidate profiles, contact information, resumes and private anecdotes are never put
in research requests. Returned model prose and snippets are not accepted as facts.
ApplyPilot independently fetches source URLs with bounded response size, public-IP DNS
pinning, TLS hostname preservation and revalidation at every redirect. It avoids private
addresses, credentials in URLs, non-HTML content and access-denied responses. It extracts
visible quotations and records URL, title, retrieval date, document hash and source type.
Company statements about culture are company-reported, not employee-experience proof.
Detected conflicting founding dates are marked uncertain. Other contradictions may
require review; semantic verification is not a formal truth guarantee.

Retrieved text is untrusted data, never executable instructions. Common injection text
is filtered, quoted evidence is clearly separated in prompts, and the writing model has
no tools capable of executing a web page's instructions. Upstream research runs separately
without workspace mounts; its own crawling behavior remains an upstream responsibility.

Research failures use durable bounded scope retries and configured-document fallback.
Partial/empty research is reported honestly; after exhaustion the application parks with
an actionable research diagnostic rather than writing generic unsupported praise.
Explicit application Resume creates a fresh bounded attempt while preserving other
applications' shared items and every paid reservation. Completed answers remain intact.

Candidate/opportunity connections are ranked by shared evidence and synthesized with
local Ollama, using validated evidence IDs. The writer emphasizes strong professional
framing, initiative and meaningful interpretations of real experience. Hard formatting,
options, limits, sensitive facts and basic unsupported metrics remain checked. A separate
local critic evaluates relevance, specificity, persuasiveness, naturalness and unsupported
claims. Weak writing receives specific feedback for at most two local revisions before
reasoning escalation. Missing history is routed to the human rather than a paid model.
Good answers are not rejected solely for low model-reported confidence.

Ollama observations distinguish ready, cold, busy, missing-model and unavailable states.
Retry patience avoids immediately spending money on a cold or temporarily busy model.
Health observations are revalidated in a new router instance after restart. Local critic
outages wait or park; they do not automatically buy another writer without evaluation.

Cloud reasoning remains explicit opt-in through existing Gemini settings/premium limits.
When enabled, selected evidence needed for the answer may reach Gemini/OpenAI, but full
profile/resume/contact data is omitted by default and direct contact text is scrubbed.
Only `cloud_share_full_profile: true` explicitly permits the old full-profile behavior.
All generation, repair and billable inference still use atomic cost reservations.

### Optional cheap-cloud and premium fallback

Keep the local-only overlay above for free operation. To authorize paid fallback, put
`GEMINI_API_KEY` and optionally `OPENAI_API_KEY` in the ignored `.env`, then merge these
settings into the same local overlay (do not create duplicate YAML keys):

```yaml
reasoning:
  gemini:
    enabled: true
    model: gemini-2.5-flash
  openai:
    enabled: true
    model: gpt-5-mini
  hybrid:
    daily_budget_usd: 1.00
    application_budget_usd: 0.10
    premium_enabled: false       # set true only to authorize OpenAI escalation
    premium_calls_per_application: 3
```

Check and update configured input/output pricing whenever changing models or tariffs.
This enables fallback in ApplyPilot, not in the research containers. Local warmup waits
and local revisions come first; missing personal history is never a reason to buy guesses.

## Progressive personal experience bank

Public schema: `config/examples/experiences.example.yaml`. Private data lives in SQLite
and optionally ignored `config/experiences.local.yaml` or `experiences/` notes. Existing
profile sections, structured employers and locally extracted PDF text provide source
documents. Suggestions retain source provenance; they never become confirmed automatically.
YAML import is additive by stable ID; existing bank confirmations/rejections take priority.
Use the explicit bank commands/API to change an imported record's approval status.

```bash
applypilot experiences suggest
applypilot experiences list
applypilot experiences confirm --id EXPERIENCE_ID
applypilot experiences reject --id EXPERIENCE_ID
applypilot experiences add --title "Resolving a team disagreement" --notes-file experiences/conflict.md
```

Notes need only a real situation, your actions, and the outcome. Verified core notes are
reused across questions; the system does not need your full autobiography. Hypothetical
answers are explicitly hypothetical. A missing real event asks for a few sentences.
After confirming an anecdote, use the application's Resume action in the dashboard/API
to regenerate its parked answer. Suggestions and approval require explicit local user action.

## Observability and validation

Authenticated `GET /api/research/status` shows service health, last discovery outcome,
tasks, scope attempts, cache source counts/freshness and revision/routing state.
`GET/POST /api/experiences` provides authenticated suggestion/confirmation/rejection/addition.
Existing metrics add research requests, cache hits and per-provider latency/failure counts.
Existing application events retain routing reasons and answer/research provenance.

Normal tests use realistic HTML and mocked HTTP/LLM contracts, without paid credentials.
The local research stack is not started by the test suite. Explicit live smoke:

```bash
APPLYPILOT_LIVE_RESEARCH=1 .venv/bin/pytest -q tests/integration/test_live_research_stack.py
```

PowerShell: set `$env:APPLYPILOT_LIVE_RESEARCH = '1'`, then run the same test with
`.venv\Scripts\python.exe -m pytest`. Use an upstream instance configured exclusively
with local Ollama. The smoke makes a public web research request; no application is submitted.

## Remaining live validation and limitations

The development environment had no Docker or Ollama binary. The installed stack's
search/model compatibility, container health checks, first-run UI, Windows/WSL host
networking and real model answer quality remain unverified. Bash syntax and the missing-Docker
diagnostic were checked; PowerShell execution requires validation on Windows. Run the
opt-in smoke above after setup. Independent public-document retrieval was additionally
tested against IANA over real HTTPS; that is not a Perplexica/Ollama live integration test.

An inspected quotation verifies retrieval, not the publisher's truth or every semantic
claim a model derives from it. Culture statements remain company-reported, and a local
critic is not a formal factual proof. Keep human final review enabled. HTML extraction is
bounded and does not execute JavaScript or bypass blocked pages; scanned PDFs need user
notes. Research scopes include company, role and role-specific technology; a separate
team identifier is not inferred. The existing bounded Windows-native focus fallback is
unchanged and remains unverified on actual Windows; Firefox DOM automation stays primary.
