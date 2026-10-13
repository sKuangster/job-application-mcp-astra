from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import make_job

from applypilot.db import Database
from applypilot.hybrid import HybridRouter
from applypilot.providers import OpenAIProvider, ProviderOutputError
from applypilot.reasoning import ReasoningService


async def work(cfg, db, *, kind="question", qclass="WRITTEN"):
    app, _ = db.create_application(make_job(db), mode="backlog")
    db.update_application(app["id"], state="ANSWERING", blocked_on="reasoning")
    rs = ReasoningService(db, cfg)
    rs.enqueue(app_id=app["id"], kind=kind, payload={"question": "Why this role?", "field_type": "textarea",
               "required": True, "qclass": qclass, "dedupe_key": "hybrid-test"}, field_key="f")
    batch = await rs.get_batch(owner="hybrid")
    return app, rs, batch


def fake_provider(result=None, *, available=True, error=None):
    return SimpleNamespace(available=lambda: available, health=AsyncMock(return_value=True),
                           complete=AsyncMock(return_value=result, side_effect=error),
                           settings=SimpleNamespace(timeout=1), escalation_reason="")


async def test_local_success_and_provider_provenance(cfg, db):
    app, rs, batch = await work(cfg, db)
    item = batch["items"][0]["item_id"]
    local = fake_provider([{"item_id": item, "value": "I enjoy building software.", "confidence": .95}])
    cloud = fake_provider(available=False)
    router = HybridRouter(cfg, rs, db, providers={"ollama": local, "gemini": cloud, "openai": cloud})
    await router.process(batch)
    assert rs.results_for(app["id"])["f"]["provider"] == "ollama"
    cloud.complete.assert_not_awaited()
    assert db.get_application(app["id"])["blocked_on"] is None


@pytest.mark.parametrize("qclass", ["FACTUAL", "EEO", "UNKNOWN", "CONSENT", "PREFERENCE"])
async def test_sensitive_or_unknown_question_never_reaches_model(cfg, db, qclass):
    app, rs, batch = await work(cfg, db, qclass=qclass)
    local = fake_provider()
    router = HybridRouter(cfg, rs, db, providers={n: local for n in ("ollama", "gemini", "openai")})
    await router.process(batch)
    assert rs.results_for(app["id"])["f"]["needs_user"]
    local.complete.assert_not_awaited()


@pytest.mark.parametrize("kind", ["page", "eligibility"])
async def test_task_aware_cloud_review(cfg, db, kind):
    _, rs, batch = await work(cfg, db, kind=kind)
    item = batch["items"][0]["item_id"]
    result = {"item_id": item, "action": {"type": "wait"}} if kind == "page" else {"item_id": item, "eligible": True}
    local, cloud = fake_provider([result]), fake_provider([result])
    router = HybridRouter(cfg, rs, db, providers={"ollama": local, "gemini": cloud, "openai": cloud})
    await router.process(batch)
    assert router.route(item)["tier"] == 1
    await router.process(await rs.get_batch(owner="hybrid"))
    cloud.complete.assert_awaited_once()


async def test_low_confidence_escalates_to_cloud(cfg, db):
    app, rs, batch = await work(cfg, db)
    item = batch["items"][0]["item_id"]
    cfg.settings.reasoning.hybrid.retries_per_tier = 1
    result = {"item_id": item, "value": "I enjoy building software.", "confidence": .2}
    local = fake_provider([result])
    cloud = fake_provider([{**result, "confidence": .95}])
    router = HybridRouter(cfg, rs, db, providers={"ollama": local, "gemini": cloud, "openai": cloud})
    await router.process(batch)
    assert router.route(item)["tier"] == 1
    await router.process(await rs.get_batch(owner="hybrid"))
    assert rs.results_for(app["id"])["f"]["provider"] == "gemini"


async def test_durable_retry_and_restart(cfg, db):
    app, rs, batch = await work(cfg, db)
    clock = [db.clock()]
    db.clock = lambda: clock[0]
    item = batch["items"][0]["item_id"]
    local = fake_provider(error=httpx.ConnectError("secret url must not be logged"))
    providers = {n: local for n in ("ollama", "gemini", "openai")}
    await HybridRouter(cfg, rs, db, providers=providers).process(batch)
    assert not (await rs.get_batch(owner="hybrid"))["items"]
    clock[0] += 61
    router = HybridRouter(cfg, rs, db, providers=providers)
    assert router.route(item)["attempts"] == 1
    await router.process(await rs.get_batch(owner="hybrid"))
    assert router.route(item)["tier"] == 1
    assert "secret url" not in json.dumps(db.events(app["id"]))


async def test_premium_opt_in_and_final_human_escalation(cfg, db):
    app, rs, batch = await work(cfg, db)
    off = fake_provider(available=False)
    premium = fake_provider(error=ProviderOutputError("invalid"))
    cfg.settings.reasoning.hybrid.retries_per_tier = 1
    router = HybridRouter(cfg, rs, db, providers={"ollama": off, "gemini": off, "openai": premium})
    for _ in range(4):
        await router.process(batch)
        batch = await rs.get_batch(owner="hybrid")
    premium.complete.assert_not_awaited()
    assert db.get_application(app["id"])["state"] == "NEEDS_USER"


async def test_enabled_premium_recovers_cloud_failure(cfg, db):
    app, rs, batch = await work(cfg, db)
    item = batch["items"][0]["item_id"]
    cfg.settings.reasoning.hybrid.premium_enabled = True
    cfg.settings.reasoning.hybrid.retries_per_tier = 1
    off = fake_provider(available=False)
    cloud = fake_provider(error=ProviderOutputError("invalid"))
    premium = fake_provider([{"item_id": item, "value": "I enjoy software.", "confidence": .95}])
    router = HybridRouter(cfg, rs, db, providers={"ollama": off, "gemini": cloud, "openai": premium})
    for _ in range(3):
        await router.process(batch)
        batch = await rs.get_batch(owner="hybrid")
    assert rs.results_for(app["id"])["f"]["provider"] == "openai"


@pytest.mark.parametrize("name", ["ollama", "gemini", "openai"])
async def test_real_adapter_wire_format_usage_cost_and_health(cfg, db, monkeypatch, name):
    app, rs, batch = await work(cfg, db)
    item = batch["items"][0]["item_id"]
    settings = getattr(cfg.settings.reasoning, name)
    settings.enabled = True
    monkeypatch.setenv(settings.api_key_env, "mock-key")
    requests = []
    result = {"results": [{"item_id": item, "value": "I enjoy software.", "confidence": .95}]}

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": settings.model}]})
        body = json.loads(request.content)
        if name == "ollama":
            assert body["stream"] is False and "format" in body and "/api/chat" in str(request.url)
            return httpx.Response(200, json={"message": {"content": json.dumps(result)}, "prompt_eval_count": 100,
                                            "eval_count": 20, "done_reason": "stop"})
        assert body["model"] == settings.model
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

    provider = OpenAIProvider(cfg, rs, db, name=name,
                              client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await provider.health()
    await provider.complete(batch)
    call = db.query("SELECT * FROM model_calls WHERE kind LIKE 'completion%'")[0]
    assert call["provider"] == name and call["model"] == settings.model
    assert call["input_tokens"] == 100 and call["output_tokens"] == 20
    assert call["estimated_usd"] == pytest.approx((100 * settings.input_usd_per_million + 20 * settings.output_usd_per_million) / 1e6)
    assert db.cost_totals()["by_application"][0]["application_id"] == app["id"]
    assert db.cost_totals()["pending_reserved_usd"] == 0


async def test_budget_blocks_before_paid_request(cfg, db, monkeypatch):
    _, rs, batch = await work(cfg, db)
    cfg.settings.reasoning.hybrid.daily_budget_usd = 0
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    requests = []
    provider = OpenAIProvider(cfg, rs, db, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: requests.append(request))))
    with pytest.raises(ValueError, match="daily budget"):
        await provider.complete(batch)
    assert not requests


def test_budget_reservations_survive_restart_and_block_races(cfg, db):
    kw = dict(provider="gemini", application_ids=["a"], amount=.06, daily_limit=.1,
              application_limit=.1, premium_limit=3)
    db.reserve_model_budget(**kw)
    restarted = Database(db.path)
    restarted.initialize()
    with pytest.raises(ValueError, match="daily budget"):
        restarted.reserve_model_budget(**kw)
    assert restarted.cost_totals()["pending_reserved_usd"] == .06


def test_premium_limit_and_application_budget(db):
    kw = dict(provider="openai", application_ids=["a"], amount=.01, daily_limit=1,
              application_limit=.02, premium_limit=1)
    db.reserve_model_budget(**kw)
    with pytest.raises(ValueError, match="premium"):
        db.reserve_model_budget(**kw)
    kw.update(provider="gemini", amount=.02)
    with pytest.raises(ValueError, match="application budget"):
        db.reserve_model_budget(**kw)


async def test_missing_usage_charges_conservative_estimate(cfg, db, monkeypatch):
    _, rs, batch = await work(cfg, db)
    monkeypatch.setenv("OPENAI_API_KEY", "mock-key")
    provider = OpenAIProvider(cfg, rs, db, client_factory=lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))))
    with pytest.raises(httpx.HTTPStatusError):
        await provider.complete(batch)
    call = db.query("SELECT * FROM model_calls WHERE kind LIKE 'completion%'")[0]
    assert call["usage_estimated"] == 1 and call["estimated_usd"] > 0
    assert call["error"] == "HTTPStatusError"


async def test_router_runs_without_mcp_session(cfg, db):
    app, rs, batch = await work(cfg, db)
    db.return_reasoning([batch["items"][0]["item_id"]])
    db.meta_set("control", "RUNNING")
    local = fake_provider([{"item_id": batch["items"][0]["item_id"], "value": "I enjoy software.", "confidence": .95}])
    router = HybridRouter(cfg, rs, db, providers={n: local for n in ("ollama", "gemini", "openai")})
    task = asyncio.create_task(router.run(lambda: bool(rs.results_for(app["id"]))))
    await asyncio.wait_for(task, timeout=5)
    assert rs.results_for(app["id"])["f"]["provider"] == "ollama"
