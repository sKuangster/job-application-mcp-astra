"""Optional unattended reasoning provider (OpenAI-compatible Chat Completions API).

Default operation uses Astra through MCP. Setting ``reasoning.provider: openai`` in
settings.yaml plus ``OPENAI_API_KEY`` lets the backend answer its own reasoning queue
in batches (same validation as MCP answers), so applications progress without a chat
session. Model calls are rate-limited by ``max_concurrent_model_requests``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from applypilot.config import AppConfig
from applypilot.db import Database
from applypilot.reasoning import ReasoningService

log = logging.getLogger("applypilot.provider")


class OpenAIProvider:
    def __init__(self, cfg: AppConfig, reasoning: ReasoningService, db: Database):
        self.cfg = cfg
        self.reasoning = reasoning
        self.db = db
        self.settings = cfg.settings.reasoning.openai
        self._sem = asyncio.Semaphore(cfg.settings.automation.max_concurrent_model_requests)

    def available(self) -> bool:
        return bool(os.environ.get(self.settings.api_key_env))

    async def run(self, closing: Callable[[], bool]) -> None:
        backoff = 5.0
        while not closing():
            try:
                batch = await self.reasoning.get_batch(owner="openai", wait_seconds=10)
                if not batch["items"]:
                    continue
                async with self._sem:
                    results = await self.complete(batch)
                self.reasoning.submit(results, provider="openai")
                backoff = 5.0
            except Exception as exc:
                log.warning("openai provider error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(300.0, backoff * 2)

    async def complete(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = json.dumps({k: batch[k] for k in ("candidate", "templates", "applications", "items")}, default=str)
        body = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": batch["instructions"] + "\nRespond with JSON: {\"results\": [...]}"},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.settings.max_output_tokens,
        }
        started = time.monotonic()
        ok, error, text = True, None, ""
        usage: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=self.settings.timeout) as client:
                resp = await client.post(f"{self.settings.base_url.rstrip('/')}/chat/completions", json=body,
                                         headers={"Authorization": f"Bearer {os.environ[self.settings.api_key_env]}"})
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"] or "{}"
                usage = data.get("usage") or {}
                parsed = json.loads(text)
                results = parsed.get("results") if isinstance(parsed, dict) else parsed
                return [r for r in results or [] if isinstance(r, dict)]
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        finally:
            self.db.record_model_call(provider="openai", kind="completion", items=len(batch["items"]),
                                      application_ids=sorted(batch["applications"]), request_chars=len(prompt),
                                      response_chars=len(text), duration_ms=int((time.monotonic() - started) * 1000),
                                      ok=ok, error=error, input_tokens=usage.get("prompt_tokens"),
                                      output_tokens=usage.get("completion_tokens"))
