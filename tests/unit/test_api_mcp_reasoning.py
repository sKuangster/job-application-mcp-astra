import asyncio
import json
import threading
from email.message import EmailMessage

import httpx
import pytest
from conftest import edit_yaml, make_job
from fastapi.testclient import TestClient

from applypilot.accounts import parse_verification
from applypilot.api import create_app
from applypilot.bridge import BrowserBridge
from applypilot.config import ConfigError, load_config
from applypilot.db import Database
from applypilot.engine.scheduler import Engine
from applypilot.providers import OpenAIProvider, ProviderOutputError
from applypilot.reasoning import ReasoningService
from applypilot.research import ResearchService, extract_relevant
from applypilot.security import redact, redact_url

H = {"X-ApplyPilot-Token": "test-token-123"}


@pytest.fixture
def api(cfg):
    app = create_app(cfg, start_engine=False)
    with TestClient(app) as client:
        yield client, app


def test_api_requires_token(api):
    client, _ = api
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-ApplyPilot-Token": "wrong"}).status_code == 401
    st = client.get("/api/status", headers=H).json()
    assert st["control"] == "STOPPED" and st["config"]["ready"] is True


def test_bridge_rejects_bad_token_and_foreign_origin(api):
    client, _ = api
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/bridge?token=nope") as ws:
        ws.receive_text()
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/bridge?token=test-token-123", headers={"origin": "https://evil.example"}) as ws:
        ws.receive_text()


def test_cors_only_for_extension_origins(api):
    client, _ = api
    ok = client.options("/api/status", headers={"Origin": "moz-extension://abcd-1234", "Access-Control-Request-Method": "GET"})
    bad = client.options("/api/status", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "moz-extension://abcd-1234"
    assert "access-control-allow-origin" not in bad.headers


def test_control_refuses_incomplete_config(project):
    edit_yaml(project, "config/candidate.yaml", {"identity": {"first_name": None}})
    cfg = load_config(project)
    with TestClient(create_app(cfg, start_engine=False)) as client:
        r = client.post("/api/control", json={"action": "start"}, headers=H)
        assert r.status_code == 400 and "first_name" in r.json()["detail"]


def test_manual_job_and_search_and_detail(api):
    client, _ = api
    r = client.post("/api/manual", json={"url": "https://job-boards.greenhouse.io/acme/jobs/9", "company": "Acme",
                                         "title": "SWE Intern"}, headers=H).json()
    items = client.get("/api/applications", params={"state": "QUEUED,FAILED", "q": "Acme"}, headers=H).json()
    assert items["total"] == 1 and items["items"][0]["id"] == r["application_id"]
    detail = client.get(f"/api/applications/{r['application_id']}", headers=H).json()
    assert detail["summary"]["company"] == "Acme" and detail["events"][0]["event_type"] == "application_created"
    assert client.get("/api/applications/app_missing", headers=H).status_code == 404
    bad = client.post(f"/api/applications/{r['application_id']}/action", json={"action": "submit"}, headers=H)
    assert bad.status_code == 409  # not READY_TO_SUBMIT


def test_mcp_requires_auth_and_lists_tools(api):
    client, _ = api
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    assert client.post("/mcp", json=init, headers=hdr).status_code == 401
    assert client.post("/mcp/wrong-token", json=init, headers=hdr).status_code == 401
    r = client.post("/mcp/mcp-token-456", json=init, headers=hdr)
    assert r.status_code == 200, r.text
    tools = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        headers={**hdr, "Authorization": "Bearer test-token-123"})
    names = {t["name"] for t in tools.json()["result"]["tools"]}
    assert {"get_status", "start_autopilot", "get_reasoning_batch", "submit_reasoning", "get_application_log",
            "application_action", "get_page_screenshot"} <= names
    call = client.post("/mcp/mcp-token-456", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                   "params": {"name": "get_status", "arguments": {}}}, headers=hdr)
    body = call.json()["result"]
    status = body.get("structuredContent") or json.loads(body["content"][0]["text"])
    assert status["control"] == "STOPPED" and "next_step" in status


# ---------------------------------------------------------------- reasoning service
@pytest.fixture
def rs(cfg, db):
    return ReasoningService(db, cfg)


def _app(db, **kw):
    job_id = make_job(db, **kw)
    app, _ = db.create_application(job_id, mode="backlog")
    return app


async def test_batch_shares_identical_questions_and_validates(rs, db):
    a, b = _app(db, company="A", url="https://a.example/1"), _app(db, company="B", url="https://b.example/1")
    payload = {"question": "Are you comfortable with on-call?", "field_type": "radio", "options": ["Yes", "No"],
               "required": True, "qclass": "UNKNOWN", "dedupe_key": "same"}
    rs.enqueue(app_id=a["id"], kind="question", payload=payload, field_key="f1")
    rs.enqueue(app_id=b["id"], kind="question", payload=payload, field_key="f2")
    db.update_application(a["id"], blocked_on="reasoning")
    db.update_application(b["id"], blocked_on="reasoning")
    batch = await rs.get_batch(wait_seconds=0)
    assert len(batch["items"]) == 1 and batch["items"][0]["shared_by"] == 2
    item_id = batch["items"][0]["item_id"]
    bad = rs.submit([{"item_id": item_id, "value": "Maybe"}])
    assert bad["rejected"] and bad["rejected"][0]["will_retry"]
    batch = await rs.get_batch(wait_seconds=0)
    assert batch["items"][0]["previous_attempt_rejected"]
    good = rs.submit([{"item_id": item_id, "value": "yes"}])
    assert good["accepted"] and good["applications_resumed"] == 2
    assert rs.results_for(a["id"])["f1"]["value"] == "Yes"
    assert db.get_application(b["id"])["blocked_on"] is None


async def test_written_answers_enforce_limits_and_truthfulness(rs, db, cfg):
    app = _app(db)
    p = {"question": "Why us?", "field_type": "textarea", "options": [], "required": True, "qclass": "WRITTEN",
         "limits": {"max_words": 5}, "dedupe_key": "w"}
    rs.enqueue(app_id=app["id"], kind="question", payload=p, field_key="f")
    item = (await rs.get_batch(wait_seconds=0))["items"][0]["item_id"]
    assert rs.submit([{"item_id": item, "value": "one two three four five six"}])["rejected"]
    assert "institution" in rs.submit([{"item_id": item, "value": "Studied at Stanford University"}])["rejected"][0]["reason"]
    res = rs.submit([{"item_id": item, "value": "My GPA 3.9 rocks"}])
    assert res["rejected"] == [] or "GPA" in json.dumps(res)


async def test_needs_user_answer_and_research_saved(rs, db):
    app = _app(db)
    rs.enqueue(app_id=app["id"], kind="question", payload={"question": "Security clearance level?", "field_type": "text",
                                                           "required": True, "dedupe_key": "c"}, field_key="f")
    item = (await rs.get_batch(wait_seconds=0))["items"][0]["item_id"]
    out = rs.submit([{"research": {"company": "Fixture Robotics", "summary": "Makes robots.",
                                   "sources": [{"url": "https://fixture.example/about", "official": True}]}},
                     {"item_id": item, "needs_user": "clearance not in profile"}])
    assert out["research_saved"] and rs.results_for(app["id"])["f"]["needs_user"]
    assert db.research("fixture robotics")[0]["origin"] == "astra"


async def test_get_batch_long_poll_wakes_on_enqueue(rs, db):
    app = _app(db)

    async def later():
        await asyncio.sleep(0.3)
        rs.enqueue(app_id=app["id"], kind="question", payload={"question": "Q?", "field_type": "text", "dedupe_key": "z"},
                   field_key="f")

    task = asyncio.create_task(later())
    batch = await rs.get_batch(wait_seconds=5)
    await task
    assert len(batch["items"]) == 1


async def test_page_batch_redacts_legacy_auth_state_without_rewriting_history(rs, db):
    app = _app(db, company="Auth Co", url="https://jobs.example/apply")
    raw_url = "https://login.example/mfa/webauthn?state=oauth-state-value&mode=passkey"
    rs.enqueue(app_id=app["id"], kind="page", payload={
        "url": raw_url, "page": {"url": raw_url, "title": "Use fingerprint or face recognition to login"},
        "dedupe_key": "legacy-auth-url",
    }, field_key="__page__auth")

    batch = await rs.get_batch(owner="openai", wait_seconds=0)

    encoded = json.dumps(batch)
    assert "oauth-state-value" not in encoded
    assert "passkey" in encoded
    # Durable evidence is left intact; redaction happens at the external-provider boundary.
    assert "oauth-state-value" in db.reasoning_items()[0]["payload"]["url"]


@pytest.mark.asyncio
async def test_openai_page_reasoning_uses_strict_schema_and_repairs_invalid_action(rs, db, cfg, monkeypatch):
    app = _app(db, company="Auth Co", url="https://login.example/passkey")
    db.update_application(app["id"], state="APPLYING", blocked_on="reasoning")
    rs.enqueue(app_id=app["id"], kind="page", payload={"problem": "unknown passkey page", "dedupe_key": "page-auth"},
               field_key="__page__auth")
    batch = await rs.get_batch(owner="openai", wait_seconds=0)
    item_id = batch["items"][0]["item_id"]
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        result = ({"item_id": item_id, "action": {"type": "open_url"}, "reason": "try another page"}
                  if len(bodies) == 1 else {"item_id": item_id, "needs_user": "Complete passkey login manually"})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"results": [result]})}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    monkeypatch.setenv(cfg.settings.reasoning.openai.api_key_env, "test-key")
    provider = OpenAIProvider(cfg, rs, db,
                              client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    results = await provider.complete(batch)

    assert results == [{"item_id": item_id, "needs_user": "Complete passkey login manually"}]
    fmt = bodies[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    action_schema = json.dumps(fmt["json_schema"]["schema"])
    assert "click_button" in action_schema and "open_url" not in action_schema
    assert "failed application validation" in bodies[1]["messages"][-1]["content"]
    assert any(e["event_type"] == "provider_validation_failed" for e in db.events(app["id"]))


@pytest.mark.asyncio
async def test_exhausted_provider_repairs_park_application_in_recoverable_state(rs, db, cfg, monkeypatch):
    app = _app(db, company="Stall Co", url="https://login.example/webauthn")
    db.update_application(app["id"], state="APPLYING", blocked_on="reasoning")
    rs.enqueue(app_id=app["id"], kind="page", payload={"problem": "unknown auth page", "dedupe_key": "stalled"},
               field_key="__page__stalled")
    batch = await rs.get_batch(owner="openai", wait_seconds=0)
    item_id = batch["items"][0]["item_id"]

    def handler(_request: httpx.Request) -> httpx.Response:
        bad = {"results": [{"item_id": item_id, "action": {"type": "open_url"}}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(bad)}}]})

    monkeypatch.setenv(cfg.settings.reasoning.openai.api_key_env, "test-key")
    provider = OpenAIProvider(cfg, rs, db,
                              client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderOutputError) as exc:
        await provider.complete(batch)
    rs.fail_provider_items([item_id], str(exc.value), provider="openai")

    current = db.get_application(app["id"])
    assert current["state"] == "NEEDS_USER" and current["blocked_on"] is None
    assert current["data"]["needs_user"]["kind"] == "reasoning"
    assert db.reasoning_items(ids=[item_id])[0]["status"] == "FAILED"
    events = db.events(app["id"])
    assert sum(e["event_type"] == "provider_validation_failed" for e in events) == 3
    assert any(e["event_type"] == "reasoning_recovery" for e in events)


@pytest.mark.asyncio
async def test_openai_falls_back_to_validated_json_mode_when_schema_is_unsupported(rs, db, cfg, monkeypatch):
    app = _app(db, company="Compat Co", url="https://compat.example")
    rs.enqueue(app_id=app["id"], kind="page", payload={"problem": "unknown", "dedupe_key": "compat"},
               field_key="__page__compat")
    batch = await rs.get_batch(owner="openai", wait_seconds=0)
    item_id = batch["items"][0]["item_id"]
    formats = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        formats.append(body["response_format"]["type"])
        if len(formats) == 1:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported"}})
        good = {"results": [{"item_id": item_id, "needs_user": "Handle this page manually"}]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(good)}}]})

    monkeypatch.setenv(cfg.settings.reasoning.openai.api_key_env, "test-key")
    provider = OpenAIProvider(cfg, rs, db,
                              client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    results = await provider.complete(batch)

    assert formats == ["json_schema", "json_object"]
    assert results[0]["needs_user"]
    assert any(e["event_type"] == "structured_output_fallback" for e in db.events(app["id"]))


# ---------------------------------------------------------------- research
async def test_research_single_flight_and_cache(cfg, db):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            "<html><title>Acme</title><body><p>Our mission is to help every engineer build robots that customers love.</p>"
            "</body></html>"))

    cfg.settings.research.enabled = True
    research = ResearchService(db, cfg, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    job = {"company": "Acme", "details": {"company": {"url": "https://acme.example", "description": "Robots"}}}
    results = await asyncio.gather(*[research.ensure("Acme", job) for _ in range(5)])
    assert all(r for r in results)
    assert len(calls) <= cfg.settings.research.max_pages  # fetched once, not 5x
    before = len(calls)
    await research.ensure("Acme", job)
    assert len(calls) == before
    topics = {r["topic"] for r in research.cached("Acme")}
    assert topics == {"profile", "official"}


def test_extract_relevant():
    title, sentences, summary = extract_relevant(
        "<title>X</title><nav>Menu</nav><p>We build developer tools that help teams ship software faster.</p>")
    assert title == "X" and sentences and "developer tools" in summary


# ---------------------------------------------------------------- config / security / email
def test_config_validation_errors(project):
    edit_yaml(project, "config/candidate.yaml", {"education": {"currently_enrolled": "yes", "graduation_date": "May 2028"}})
    with pytest.raises(ConfigError) as exc:
        load_config(project)
    assert "currently_enrolled" in str(exc.value) and "graduation_date" in str(exc.value)
    edit_yaml(project, "config/candidate.yaml", {"education": {"currently_enrolled": True, "graduation_date": "2028-05"}})
    edit_yaml(project, "config/settings.yaml", {"automation": {"max_concurrent_applications": 99}})
    with pytest.raises(ConfigError):
        load_config(project)


def test_local_overlay_files(project):
    (project / "config" / "candidate.local.yaml").write_text("identity:\n  first_name: Overlay\n")
    assert load_config(project).candidate["identity"]["first_name"] == "Overlay"


def test_readiness_lists_missing(project):
    for f in (project / "resumes").iterdir():
        f.unlink()
    edit_yaml(project, "config/candidate.yaml", {"identity": {"phone": None}})
    r = load_config(project).readiness()
    assert not r["ready"] and "candidate.identity.phone" in r["missing"] and any("resume" in m for m in r["missing"])


def test_redaction():
    assert redact({"password": "x", "a": {"api_key": "k"}, "b": [{"token": "t"}]}) == \
        {"password": "[REDACTED]", "a": {"api_key": "[REDACTED]"}, "b": [{"token": "[REDACTED]"}]}
    assert "secret123" not in redact_url("https://x.example/verify?token=secret123&id=4")
    assert "oauth-state-value" not in redact_url("https://login.example/mfa?state=oauth-state-value&mode=passkey")


def test_parse_verification_code_and_link():
    msg = EmailMessage()
    msg["Subject"] = "Verify your account"
    msg["From"] = "noreply@myworkday.com"
    msg.set_content("Your verification code is 482913. It expires soon.")
    v = parse_verification(msg, sender_domains=["myworkday"])
    assert v and v.kind == "code" and v.value == "482913"
    bank = EmailMessage()
    bank["Subject"] = "Your verification code"
    bank["From"] = "security@mybank.example"
    bank.set_content("Your verification code is 111222.")
    assert parse_verification(bank, sender_domains=["myworkday", "acme"]) is None  # never another service's code
    msg2 = EmailMessage()
    msg2["From"] = "Acme Careers <no-reply@icims.com>"
    msg2["Subject"] = "Confirm your email"
    msg2.set_content("Click https://acme.icims.com/confirm?token=abc to activate your account.")
    v2 = parse_verification(msg2, sender_domains=["icims"])
    assert v2 and v2.kind == "link" and "confirm" in v2.value
    msg3 = EmailMessage()
    msg3["Subject"] = "Newsletter"
    msg3.set_content("Hello")
    assert parse_verification(msg3, sender_domains=[]) is None


def test_engine_requires_single_backend(cfg, tmp_path):
    db = Database(cfg.paths.db_path)
    db.initialize()
    assert db.acquire_lock("backend", "other-process", ttl=60)
    engine = Engine(cfg, db, BrowserBridge())

    async def go():
        with pytest.raises(RuntimeError, match="another ApplyPilot backend"):
            await engine.start()

    asyncio.run(go())


def test_concurrent_enqueue_dedupes(cfg, tmp_path):
    db = Database(tmp_path / "rq.sqlite3")
    db.initialize()
    apps = [_app(db, company=f"C{i}", url=f"https://c{i}.example/1") for i in range(6)]
    ids = []

    def go(app):
        ids.append(db.enqueue_reasoning(kind="question", dedupe_key="k", payload={}, app_id=app["id"])["id"])

    threads = [threading.Thread(target=go, args=(a,)) for a in apps]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 1


def test_tunnelled_requests_only_reach_mcp(api):
    client, _ = api
    proxied = {**H, "X-Forwarded-For": "203.0.113.9", "Origin": "moz-extension://forged"}
    assert client.get("/api/status", headers=proxied).status_code == 403
    assert client.post("/api/pair", headers=proxied).status_code == 403
    hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "CF-Connecting-IP": "1.2.3.4"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    assert client.post("/mcp/mcp-token-456", json=init, headers=hdr).status_code == 200


def test_pairing_window(api):
    client, app = api
    ext = {"Origin": "moz-extension://1234"}
    assert client.post("/api/pair", headers=ext).status_code == 403  # closed by default
    client.post("/api/pairing/open", json={"seconds": 60}, headers=H)
    assert client.post("/api/pair", headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.post("/api/pair", headers=ext)
    assert r.status_code == 200 and r.json()["token"] == "test-token-123"
    assert client.post("/api/pair", headers=ext).status_code == 403  # one pairing per window
