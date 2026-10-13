"""Autonomous local -> cheap cloud -> optional premium -> human routing.

The resolver handles deterministic answers and validated answer memory before this
queue. Every item is independently routed, so one difficult item cannot escalate
an entire batch. Retry time, tier and circuit health are durable SQLite state.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from typing import Any

from applypilot.config import AppConfig
from applypilot.db import Database
from applypilot.providers import OpenAIProvider, ProviderOutputError
from applypilot.reasoning import ReasoningService

log = logging.getLogger("applypilot.hybrid")
TIERS = ("ollama", "gemini", "openai")


class HybridRouter:
    def __init__(self, cfg: AppConfig, reasoning: ReasoningService, db: Database, *, providers: Any = None):
        self.cfg, self.reasoning, self.db = cfg, reasoning, db
        self.settings = cfg.settings.reasoning.hybrid
        self.providers = providers or {name: OpenAIProvider(cfg, reasoning, db, name=name) for name in TIERS}

    def route(self, item_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM reasoning_routes WHERE item_id=?", (item_id,))
        return rows[0] if rows else {"tier": 0, "attempts": 0, "reason": "initial"}

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
        item = batch["items"][0]
        item_id = str(item["item_id"])
        if item["kind"] == "question" and item.get("qclass") in {"UNKNOWN", "FACTUAL", "EEO", "CONSENT", "PREFERENCE"}:
            self.reasoning.submit([{"item_id": item_id, "needs_user":
                                   "Unresolved fact, preference, consent or unclassified question requires human input."}],
                                  provider="hybrid")
            return
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
        if health.get("retry_at", 0) > self.db.clock():
            self._retry(item_id, tier + 1, 0, 0, f"{name}:circuit_open")
            return
        try:
            self.save(item_id, tier=tier, attempts=int(route["attempts"]) + 1, delay=0, reason=route["reason"])
            if (health.get("checked_at", 0) + self.settings.health_cooldown_seconds <= self.db.clock()
                    and not await asyncio.wait_for(provider.health(), timeout=10)):
                raise ConnectionError("provider unavailable")
            self.db.meta_set(health_key, {"checked_at": self.db.clock(), "retry_at": 0, "healthy": True})
            provider.escalation_reason = route["reason"]
            heartbeat = asyncio.create_task(self._heartbeat(item_id))
            try:
                results = await asyncio.wait_for(provider.complete(batch), timeout=provider.settings.timeout * 3 + 15)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            errors = self.reasoning.validate_provider_results([item_id], results)
            if errors:
                raise ProviderOutputError("invalid response")
            result = results[0]
            if result.get("needs_user"):
                self.reasoning.submit(results, provider=name)
                return
            if item["kind"] == "question":
                threshold = (self.settings.written_confidence if item.get("qclass") in {"WRITTEN", "JOB_SPECIFIC"}
                             else self.settings.question_confidence)
                confidence = float(result.get("confidence", 0))
                if not math.isfinite(confidence) or not 0 <= confidence <= 1 or confidence < max(
                        threshold, self.cfg.settings.reasoning.min_confidence):
                    raise ProviderOutputError("low confidence")
            elif name == "ollama" and self.settings.review_local_page_and_eligibility and self.providers["gemini"].available():
                self._retry(item_id, 1, 0, 0, f"{item['kind']}:cloud_review_required")
                return
            self.reasoning.submit(results, provider=name)
            self.save(item_id, tier=tier, attempts=0, delay=0, reason="accepted")
        except asyncio.CancelledError:
            # Keep leased item and unsettled cost reservations; lease expiry recovers it.
            raise
        except Exception as exc:
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
