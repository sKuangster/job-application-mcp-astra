"""Autonomous local -> cheap cloud -> optional premium -> human routing.

The resolver handles deterministic answers and validated answer memory before this
queue. Every item is independently routed, so one difficult item cannot escalate
an entire batch. Retry time, tier and circuit health are durable SQLite state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from applypilot.config import AppConfig
from applypilot.db import Database
from applypilot.experiences import ExperienceBank
from applypilot.providers import OpenAIProvider, ProviderOutputError
from applypilot.reasoning import ReasoningService
from applypilot.textutil import stable_hash
from applypilot.writing import (
    WritingReviewError,
    deterministic_review,
    fit_candidates,
    question_intent,
)

log = logging.getLogger("applypilot.hybrid")
TIERS = ("ollama", "gemini", "openai")


class LocalUnavailable(ConnectionError):
    def __init__(self, state: str):
        self.state = state
        super().__init__(state)


class HybridRouter:
    def __init__(self, cfg: AppConfig, reasoning: ReasoningService, db: Database, *, providers: Any = None):
        self.cfg, self.reasoning, self.db = cfg, reasoning, db
        self.settings = cfg.settings.reasoning.hybrid
        self.providers = providers or {name: OpenAIProvider(cfg, reasoning, db, name=name) for name in TIERS}
        self.experiences = ExperienceBank(cfg, db)
        self._probed: set[str] = set()

    def route(self, item_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM reasoning_routes WHERE item_id=?", (item_id,))
        return rows[0] if rows else {"tier": 0, "attempts": 0, "reason": "initial", "revisions": 0,
                                    "feedback": "[]", "first_attempt_at": None}

    def save(self, item_id: str, *, tier: int, attempts: int, delay: float, reason: str) -> None:
        self.db.write(lambda c: c.execute(
            "INSERT INTO reasoning_routes(item_id,tier,attempts,available_at,reason,updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(item_id) DO UPDATE SET tier=excluded.tier,attempts=excluded.attempts,"
            "available_at=excluded.available_at,reason=excluded.reason,updated_at=excluded.updated_at",
            (item_id, tier, attempts, self.db.clock() + delay, reason, self.db.clock())))

    async def run(self, closing: Callable[[], bool]) -> None:
        while not closing():
            if self.db.meta_get("control", "STOPPED") != "RUNNING":
                await asyncio.sleep(0.5)
                continue
            try:
                batch = await self.reasoning.get_batch(owner="hybrid", max_items=1, wait_seconds=1)
                if batch["items"]:
                    await self.process(batch)
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("router iteration failed: %s", type(exc).__name__)
                await asyncio.sleep(1)

    async def process(self, batch: dict[str, Any]) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(str(batch["items"][0]["item_id"])))
        try:
            await self._process(batch)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _process(self, batch: dict[str, Any]) -> None:
        item = batch["items"][0]
        item_id = str(item["item_id"])
        if item["kind"] == "question" and item.get("qclass") in {"UNKNOWN", "FACTUAL", "EEO", "CONSENT", "PREFERENCE"}:
            self.reasoning.submit([{"item_id": item_id, "needs_user":
                                   "Unresolved fact, preference, consent or unclassified question requires human input."}],
                                  provider="hybrid")
            return
        route = self.route(item_id)
        if route.get("first_attempt_at") is None:
            self.save(item_id, tier=int(route["tier"]), attempts=int(route["attempts"]), delay=0, reason=route["reason"])
            self.db.write(lambda c: c.execute("UPDATE reasoning_routes SET first_attempt_at=? WHERE item_id=?",
                                             (self.db.clock(), item_id)))
            route = self.route(item_id)
        tier = int(route["tier"])
        if tier >= len(TIERS):
            self.reasoning.fail_provider_items([item_id], route["reason"], provider="hybrid")
            return
        name = TIERS[tier]
        provider = self.providers[name]
        if not provider.available() or (name == "openai" and not self.settings.premium_enabled):
            self._retry(item_id, tier + 1, 0, 0, f"{name}:disabled_or_unconfigured")
            return
        health_key = f"provider_health:{name}"
        health = self.db.meta_get(health_key, {})
        if health.get("retry_at", 0) > self.db.clock() and name in self._probed:
            if name == "ollama":
                self._local_retry(item_id, route, health.get("state", "unavailable"))
                return
            self._retry(item_id, tier + 1, 0, 0, f"{name}:circuit_open")
            return
        try:
            self.save(item_id, tier=tier, attempts=int(route["attempts"]) + 1, delay=0, reason=route["reason"])
            readiness = {"state": "ready"}
            if name not in self._probed or health.get("checked_at", 0) + self.settings.health_cooldown_seconds <= self.db.clock():
                self._probed.add(name)
                if name == "ollama" and hasattr(provider, "readiness"):
                    readiness = await asyncio.wait_for(provider.readiness(), timeout=12)
                    if readiness["state"] == "model_missing":
                        self.db.meta_set(health_key, {**health, "state": "model_missing", "model_present": False,
                                                     "healthy": False, "checked_at": self.db.clock()})
                        self._retry(item_id, 1, 0, 0, "ollama:model_missing")
                        return
                    if readiness["state"] not in {"ready", "cold"}:
                        raise LocalUnavailable(readiness["state"])
                elif not await asyncio.wait_for(provider.health(), timeout=10):
                    raise ConnectionError("provider unavailable")
            self.db.meta_set(health_key, {**health, "checked_at": self.db.clock(), "retry_at": 0,
                                         "healthy": True, "state": readiness["state"], "model_present": True,
                                         "loaded": readiness["state"] == "ready"})
            if self.cfg.settings.reasoning.writing.enabled and item["kind"] == "question":
                if not await self._prepare_writing(batch, route):
                    return
                if name == "ollama" and self.cfg.settings.reasoning.writing.fit_synthesis:
                    await self._fit(batch, provider)
            provider.escalation_reason = route["reason"]
            heartbeat = asyncio.create_task(self._heartbeat(item_id))
            try:
                started = time.monotonic()
                results = await asyncio.wait_for(provider.complete(batch), timeout=provider.settings.timeout * 3 + 15)
                self.db.meta_set(health_key, {"checked_at": self.db.clock(), "retry_at": 0, "healthy": True, "state": "ready",
                                             "last_success": self.db.clock(), "latency_ms": int((time.monotonic() - started) * 1000),
                                             "consecutive_failures": 0})
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            errors = self.reasoning.validate_provider_results([item_id], results)
            if errors:
                if self.cfg.settings.reasoning.writing.enabled and item["kind"] == "question":
                    raise WritingReviewError("format", [error[:250] for error in errors[:6]])
                raise ProviderOutputError("invalid response")
            result = results[0]
            if result.get("needs_user"):
                self.reasoning.submit(results, provider=name)
                return
            if item["kind"] == "question":
                if self.cfg.settings.reasoning.writing.enabled:
                    await self._review(batch, result)
                    result["research_ids"] = sorted({source["research_id"] for source in
                                                     item["grounding"]["opportunity_evidence"] if source.get("research_id")})
                threshold = (self.settings.written_confidence if item.get("qclass") in {"WRITTEN", "JOB_SPECIFIC"}
                             else self.settings.question_confidence)
                confidence = float(result.get("confidence", 0))
                if (not self.cfg.settings.reasoning.writing.enabled and
                        (not math.isfinite(confidence) or not 0 <= confidence <= 1 or confidence < max(
                            threshold, self.cfg.settings.reasoning.min_confidence))):
                    raise ProviderOutputError("low confidence")
            elif name == "ollama" and self.settings.review_local_page_and_eligibility and self.providers["gemini"].available():
                self._retry(item_id, 1, 0, 0, f"{item['kind']}:cloud_review_required")
                return
            self.reasoning.submit(results, provider=name)
            self.save(item_id, tier=tier, attempts=0, delay=0, reason="accepted")
        except asyncio.CancelledError:
            # Keep leased item and unsettled cost reservations; lease expiry recovers it.
            raise
        except WritingReviewError as exc:
            if exc.category == "evaluator_unavailable":
                elapsed = self.db.clock() - float(route.get("first_attempt_at") or self.db.clock())
                if elapsed >= self.settings.max_queue_wait_seconds:
                    self.reasoning.fail_provider_items([item_id], "Local writing evaluation remained unavailable; check Ollama and retry.", provider="writing")
                else:
                    self._retry(item_id, tier, int(route["attempts"]), self.settings.health_cooldown_seconds, "writing:evaluator_unavailable")
                return
            revisions = int(route.get("revisions") or 0)
            if revisions < self.cfg.settings.reasoning.writing.local_revision_limit:
                feedback_json = json.dumps(exc.feedback[:6])
                self.db.write(lambda c: c.execute("UPDATE reasoning_routes SET revisions=revisions+1,feedback=? WHERE item_id=?",
                                                 (feedback_json, item_id)))
                self._retry(item_id, 0, 0, 0, "local_revision:" + exc.category)
            elif exc.category in {"unsupported_claim", "missing_personal"}:
                self.reasoning.submit([{"item_id": item_id, "needs_user": "Please confirm the missing factual core: " +
                                       "; ".join(exc.feedback[:2])}], provider="hybrid")
            else:
                self._retry(item_id, min(tier + 1, len(TIERS)), 0, 0, "writing_quality:" + exc.category)
        except Exception as exc:
            if name == "ollama" and not isinstance(exc, ValueError):
                state = exc.state if isinstance(exc, LocalUnavailable) else "temporarily_unavailable"
                self.db.meta_set(health_key, {**health, "healthy": False, "state": state, "checked_at": self.db.clock(),
                                            "consecutive_failures": int(health.get("consecutive_failures", 0)) + 1,
                                            "retry_at": self.db.clock() + self.settings.health_cooldown_seconds})
                self._local_retry(item_id, route, state)
                return
            attempts = int(route["attempts"]) + 1
            reason = f"{name}:{type(exc).__name__}"
            if isinstance(exc, ValueError) and not isinstance(exc, ProviderOutputError):
                reason = f"{name}:budget_or_request_rejected"
                attempts = self.settings.retries_per_tier
            if not isinstance(exc, (ValueError, ProviderOutputError)):
                self.db.meta_set(health_key, {"healthy": False, "checked_at": self.db.clock(),
                                            "retry_at": self.db.clock() + self.settings.health_cooldown_seconds})
            advance = attempts >= self.settings.retries_per_tier
            # Retry an unavailable tier after its cooldown rather than hot-looping.
            delay = 0 if advance else max(self.settings.retry_seconds * 2 ** (attempts - 1),
                                          self.settings.health_cooldown_seconds if not isinstance(exc, ValueError) else 0)
            self._retry(item_id, tier + 1 if advance else tier, 0 if advance else attempts, delay, reason)

    def _local_retry(self, item_id: str, route: dict[str, Any], state: str) -> None:
        attempts = int(route["attempts"]) + 1
        elapsed = self.db.clock() - float(route.get("first_attempt_at") or self.db.clock())
        exhausted = elapsed >= self.settings.max_queue_wait_seconds or (
            attempts >= self.settings.local_operational_retries and elapsed >= self.settings.local_warmup_patience_seconds)
        self._retry(item_id, 1 if exhausted else 0, 0 if exhausted else attempts,
                    0 if exhausted else self.settings.health_cooldown_seconds, "ollama:" + state)

    async def _prepare_writing(self, batch: dict[str, Any], route: dict[str, Any]) -> bool:
        item = batch["items"][0]
        item_id = str(item["item_id"])
        rows = self.db.reasoning_items(ids=[item_id])
        waiters = rows[0]["waiters"] if rows else []
        app_id = waiters[0]["application_id"] if waiters else item.get("application_id")
        job = self.db.get_job(self.db.get_application(app_id)["job_id"]) if app_id else {}
        job = job or {}
        intent = question_intent(item.get("question", ""), item.get("template"))
        query = item.get("question", "") + " " + str(job.get("title") or "") + " " + str((job.get("details") or {}).get("description_text") or "")[:2000]
        evidence = self.experiences.retrieve(query, anecdote=intent == "behavioral")
        if intent == "behavioral" and not evidence:
            self.reasoning.submit([{"item_id": item_id, "needs_user": "Share one real relevant event: what happened, "
                                   "what you personally did, and the outcome. A few sentences are enough."}], provider="hybrid")
            return False
        research: dict[str, Any] = await self.reasoning.research.prepare(item, job) if self.reasoning.research else {
            "intent": intent, "sufficient": True, "evidence": []}
        if not research["sufficient"]:
            elapsed = self.db.clock() - float(route["first_attempt_at"])
            tasks = self.db.query("SELECT * FROM research_tasks WHERE company=? AND state<>'DONE'", (job.get("company", ""),))
            exhausted = tasks and all(t["attempts"] >= self.cfg.settings.research.max_requests_per_scope for t in tasks)
            if exhausted or elapsed >= self.cfg.settings.research.max_wait_seconds:
                self.reasoning.fail_provider_items([item_id], "Inspected company evidence is unavailable. Check research services "
                                                  "or configure a reachable public primary source, then retry.", provider="research")
            else:
                self._retry(item_id, int(route["tier"]), int(route["attempts"]), self.cfg.settings.research.retry_seconds,
                            "research:insufficient_evidence")
            return False
        item["grounding"] = {"intent": intent, "candidate_evidence": evidence,
                             "opportunity_evidence": research["evidence"],
                             "connections": fit_candidates(evidence, research["evidence"]),
                             "research_sufficient": True}
        feedback = json.loads(route.get("feedback") or "[]")
        if feedback:
            item["revision_feedback"] = feedback
        return True

    async def _fit(self, batch: dict[str, Any], provider: Any) -> None:
        grounding = batch["items"][0]["grounding"]
        if not grounding["candidate_evidence"] or not grounding["opportunity_evidence"] or not hasattr(provider, "synthesize_fit"):
            return
        key = "fit:" + stable_hash({"grounding": grounding, "profile": self.cfg.profile_hash}, 24)
        company = next(iter(batch["applications"].values())).get("company", "") if batch["applications"] else ""
        from applypilot.textutil import company_norm

        cached = self.db.research(company_norm(company), topic=key)
        if cached:
            grounding["connections"] = json.loads(cached[0]["summary"])
            return
        try:
            connections = await provider.synthesize_fit(batch)
            grounding["connections"] = connections
            self.db.save_research(company=company, company_norm=company_norm(company), topic=key,
                                  summary=json.dumps(connections), facts=[], sources=[], origin="local_fit",
                                  ttl=self.cfg.settings.research.role_ttl_seconds)
        except ProviderOutputError:
            # A bad fit response does not remove the deterministic evidence-ranked pairs.
            pass

    async def _review(self, batch: dict[str, Any], result: dict[str, Any]) -> None:
        item = batch["items"][0]
        feedback = deterministic_review(item, result)
        if feedback:
            category = "unsupported_claim" if any("unsupported" in f for f in feedback) else "substance"
            raise WritingReviewError(category, feedback)
        if self.cfg.settings.reasoning.writing.semantic_evaluation:
            local = self.providers["ollama"]
            if not local.available() or not hasattr(local, "evaluate_writing"):
                raise WritingReviewError("evaluator_unavailable", ["Local semantic evaluation is unavailable."])
            try:
                verdict = await local.evaluate_writing(batch, result)
            except (httpx.HTTPError, ProviderOutputError, TimeoutError) as exc:
                raise WritingReviewError("evaluator_unavailable", ["Local semantic evaluation is temporarily unavailable."]) from exc
            if verdict["unsupported_claims"]:
                raise WritingReviewError("unsupported_claim", [str(x)[:250] for x in verdict["unsupported_claims"]])
            if not all(verdict[key] for key in ("relevant", "specific", "persuasive", "natural")):
                raise WritingReviewError("substance", [str(x)[:250] for x in verdict["feedback"]] or ["Improve relevance, specificity and natural persuasive writing."])

    async def _heartbeat(self, item_id: str) -> None:
        seconds = self.cfg.settings.reasoning.item_lease_seconds
        while True:
            self.db.write(lambda c: c.execute("UPDATE reasoning_items SET lease_until=? "
                                             "WHERE id=? AND status='LEASED' AND lease_owner='hybrid'",
                                             (self.db.clock() + seconds, item_id)))
            await asyncio.sleep(seconds / 3)

    def _retry(self, item_id: str, tier: int, attempts: int, delay: float, reason: str) -> None:
        self.save(item_id, tier=tier, attempts=attempts, delay=delay, reason=reason)
        self.db.return_reasoning([item_id])
        for item in self.db.reasoning_items(ids=[item_id]):
            for waiter in item["waiters"]:
                self.db.event(waiter["application_id"], "reasoning", "provider_escalation", reason,
                              {"item_id": item_id, "tier": tier, "attempts": attempts, "retry_in": delay})
