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
MAX_OUTPUT_REPAIRS = 2


class ProviderOutputError(ValueError):
    """The model returned syntactically valid but unusable structured output."""


class OpenAIProvider:
    def __init__(self, cfg: AppConfig, reasoning: ReasoningService, db: Database, *, client_factory: Any = None):
        self.cfg = cfg
        self.reasoning = reasoning
        self.db = db
        self.settings = cfg.settings.reasoning.openai
        self._sem = asyncio.Semaphore(cfg.settings.automation.max_concurrent_model_requests)
        self.client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=self.settings.timeout))

    def available(self) -> bool:
        return bool(os.environ.get(self.settings.api_key_env))

    async def run(self, closing: Callable[[], bool]) -> None:
        backoff = 5.0
        while not closing():
            batch: dict[str, Any] | None = None
            try:
                if self.db.meta_get("control", "STOPPED") != "RUNNING":
                    await asyncio.sleep(1.0)
                    continue
                batch = await self.reasoning.get_batch(owner="openai", wait_seconds=10)
                if not batch["items"]:
                    continue
                async with self._sem:
                    results = await self.complete(batch)
                self.reasoning.submit(results, provider="openai")
                backoff = 5.0
            except ProviderOutputError as exc:
                if batch:
                    item_ids = [str(i["item_id"]) for i in batch["items"]]
                    self.reasoning.fail_provider_items(item_ids, str(exc), provider="openai")
                backoff = 5.0
            except Exception as exc:
                log.warning("openai provider error: %s", exc)
                if batch:
                    item_ids = [str(i["item_id"]) for i in batch["items"]]
                    self.db.return_reasoning(item_ids)
                    for app_id in batch["applications"]:
                        self.db.event(app_id, "reasoning", "provider_error",
                                      "OpenAI request failed; reasoning item returned to the queue",
                                      {"error": f"{type(exc).__name__}: {str(exc)[:300]}",
                                       "item_ids": item_ids, "decision": "retry"}, level="WARNING")
                await asyncio.sleep(backoff)
                backoff = min(300.0, backoff * 2)

    async def complete(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = json.dumps({k: batch[k] for k in ("candidate", "templates", "applications", "items")}, default=str)
        messages = [
            {"role": "system", "content": batch["instructions"] +
             "\nReturn one result for every item. Follow the supplied JSON schema exactly."},
            {"role": "user", "content": prompt},
        ]
        item_ids = [str(item["item_id"]) for item in batch["items"]]
        schema = _batch_schema(batch["items"])
        last_errors: list[str] = []
        structured = True
        for attempt in range(1, MAX_OUTPUT_REPAIRS + 2):
            try:
                text, parsed = await self._request(messages, schema=schema, structured=structured,
                                                   item_count=len(item_ids), app_ids=sorted(batch["applications"]))
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500].lower()
                if structured and exc.response.status_code == 400 and any(
                        word in detail for word in ("json_schema", "response_format", "structured output", "unsupported")):
                    structured = False
                    for app_id in batch["applications"]:
                        self.db.event(app_id, "reasoning", "structured_output_fallback",
                                      "Provider does not support strict JSON schema; using validated JSON mode",
                                      {"status": 400, "decision": "json_object_fallback"}, level="WARNING")
                    messages.append({"role": "user", "content": "Strict schema mode is unavailable. In JSON mode, "
                                     "follow this schema exactly: " + json.dumps(schema, separators=(",", ":"))})
                    continue
                raise
            except (ProviderOutputError, json.JSONDecodeError) as exc:
                text, parsed = "", None
                last_errors = [str(exc)]
                results = None
            else:
                results = parsed.get("results") if isinstance(parsed, dict) else None
                last_errors = self.reasoning.validate_provider_results(item_ids, results)
            if not last_errors:
                assert isinstance(results, list)
                return results
            shape = _response_outline(results)
            for app_id in batch["applications"]:
                self.db.event(app_id, "reasoning", "provider_validation_failed",
                              f"OpenAI response failed validation (attempt {attempt}/{MAX_OUTPUT_REPAIRS + 1})",
                              {"attempt": attempt, "max_attempts": MAX_OUTPUT_REPAIRS + 1,
                               "errors": last_errors[:10], "response_shape": shape,
                               "decision": "repair" if attempt <= MAX_OUTPUT_REPAIRS else "needs_user"},
                              level="WARNING")
            if attempt <= MAX_OUTPUT_REPAIRS:
                messages.extend([
                    {"role": "assistant", "content": text[:12000]},
                    {"role": "user", "content": "Your JSON failed application validation:\n- " +
                     "\n- ".join(last_errors[:10]) +
                     "\nReturn a corrected complete results array. Do not omit any item."},
                ])
        raise ProviderOutputError("; ".join(last_errors[:10]) or "model returned no usable results")

    async def _request(self, messages: list[dict[str, str]], *, schema: dict[str, Any], structured: bool,
                       item_count: int, app_ids: list[str]) -> tuple[str, Any]:
        response_format = ({"type": "json_schema", "json_schema": {
            "name": "applypilot_reasoning_results", "description": "Validated ApplyPilot reasoning actions",
            "strict": True, "schema": schema}}
            if structured else {"type": "json_object"})
        body = {"model": self.settings.model, "messages": messages, "response_format": response_format,
                "max_completion_tokens": self.settings.max_output_tokens}
        started = time.monotonic()
        ok, error, text = True, None, ""
        usage: dict[str, Any] = {}
        try:
            async with self.client_factory() as client:
                resp = await client.post(f"{self.settings.base_url.rstrip('/')}/chat/completions", json=body,
                                         headers={"Authorization": f"Bearer {os.environ[self.settings.api_key_env]}"})
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                choice = data["choices"][0]
                message = choice.get("message") or {}
                if message.get("refusal"):
                    raise ProviderOutputError(f"model refusal: {str(message['refusal'])[:300]}")
                if choice.get("finish_reason") not in (None, "stop"):
                    raise ProviderOutputError(f"incomplete model response: {choice.get('finish_reason')}")
                text = message.get("content") or "{}"
                return text, json.loads(text)
        except Exception as exc:
            ok, error = False, f"{type(exc).__name__}: {str(exc)[:300]}"
            raise
        finally:
            request_chars = len(json.dumps(messages, default=str))
            self.db.record_model_call(provider="openai", kind="completion_structured" if structured else "completion_json",
                                      items=item_count, application_ids=app_ids, request_chars=request_chars,
                                      response_chars=len(text), duration_ms=int((time.monotonic() - started) * 1000),
                                      ok=ok, error=error, input_tokens=usage.get("prompt_tokens"),
                                      output_tokens=usage.get("completion_tokens"))


def _batch_schema(items: list[dict[str, Any]]) -> dict[str, Any]:
    variants: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item["item_id"])
        identity = {"type": "string", "enum": [item_id]}
        needs_user = _strict_object({"item_id": identity, "needs_user": {"type": "string"}})
        if item["kind"] == "page":
            action = _strict_object({
                "type": {"type": "string", "enum": ["click_button", "click_text", "click_point",
                                                            "needs_user", "skip_job", "wait"]},
                "button_id": {"type": ["string", "null"]}, "text": {"type": ["string", "null"]},
                "exact": {"type": ["boolean", "null"]}, "x": {"type": ["number", "null"]},
                "y": {"type": ["number", "null"]}, "seconds": {"type": ["number", "null"]},
                "reason": {"type": ["string", "null"]},
            })
            variants.append(_strict_object({"item_id": identity, "action": action,
                                            "reason": {"type": ["string", "null"]}}))
            variants.append(needs_user)
        elif item["kind"] == "eligibility":
            variants.append(_strict_object({"item_id": identity, "eligible": {"type": "boolean"},
                                            "reason": {"type": "string"}}))
            variants.append(needs_user)
        else:
            ftype = item.get("field_type")
            if ftype == "checkbox":
                value: dict[str, Any] = {"type": "boolean"}
            elif ftype == "checkbox_group" or item.get("multiple"):
                value = {"type": "array", "items": {"type": "string"}}
            else:
                value = {"type": "string"}
            variants.append(_strict_object({
                "item_id": identity, "value": value, "fact_paths": {"type": "array", "items": {"type": "string"}},
                "template": {"type": ["string", "null"]}, "confidence": {"type": "number"},
            }))
            variants.append(needs_user)
    item_schema = {"anyOf": variants} if len(variants) > 1 else variants[0]
    return _strict_object({"results": {"type": "array", "items": item_schema,
                                        "minItems": len(items), "maxItems": len(items)}})


def _strict_object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def _response_outline(results: Any) -> Any:
    if not isinstance(results, list):
        return {"results_type": type(results).__name__}
    return [{"item_id": str(r.get("item_id") or "")[:80],
             "keys": sorted(str(k) for k in r)[:20],
             "action_type": (r.get("action") or {}).get("type") if isinstance(r.get("action"), dict) else None}
            if isinstance(r, dict) else {"type": type(r).__name__} for r in results[:20]]
