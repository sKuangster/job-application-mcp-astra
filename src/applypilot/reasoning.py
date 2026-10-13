"""Shared reasoning service: the only path by which semantic work reaches Astra.

Workers never call a model. They enqueue *reasoning items* (one per question that
deterministic code could not answer; identical questions across concurrent applications
share one item) and park the application. Astra pulls batches through MCP
(``get_reasoning_batch``), answers many items in one call (``submit_reasoning``), and the
service validates each answer, stores it in answer memory and unblocks the waiting
applications. An optional OpenAI-compatible provider can consume the same queue for fully
unattended operation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

from applypilot.config import AppConfig, lookup
from applypilot.db import Database
from applypilot.questions.options import DECLINE, match_option
from applypilot.questions.resolver import facts_digest, fits_limits
from applypilot.security import redact_url
from applypilot.textutil import company_norm, normalize_question, truncate, word_count

log = logging.getLogger("applypilot.reasoning")

ANSWER_INSTRUCTIONS = """\
You are answering job-application questions for the candidate described in `candidate`.
Rules:
1. Facts (dates, schools, degrees, employers, titles, GPA, authorization, citizenship, awards) MUST come
   from `candidate.facts` / `candidate.profile`. Never invent or embellish a credential. If a factual answer
   cannot be established from them, return {"needs_user": "<what is missing>"} for that item.
2. For option questions return EXACTLY one of the provided option labels (a list of labels when `multiple`).
3. Written answers: use the named template as style/content guidance (not verbatim), tailor to the company,
   role and job description, cite specific real experiences from the profile, use the company research when
   provided, sound like the candidate, and respect `limits` (max_chars / max_words) with margin.
4. Be specific, concise and truthful; no generic filler, no placeholders.
Return via submit_reasoning: [{"item_id": ..., "value": ..., "fact_paths": [...], "template": "...",
"confidence": 0-1}] — or {"item_id": ..., "needs_user": "reason"}. For eligibility items return
{"item_id": ..., "eligible": true|false, "reason": "..."}. For page items return {"item_id": ..., "action": {...}}.
If you researched the company, include {"research": {"company": ..., "summary": ..., "facts": [...],
"sources": [{"url": ..., "title": ..., "official": true|false}]}} once per company.
"""


class ReasoningService:
    def __init__(self, db: Database, cfg: AppConfig, research: Any = None):
        self.db = db
        self.cfg = cfg
        self.research = research
        self._new_work = asyncio.Event()
        self.metrics = {"batches": 0, "items_delivered": 0, "items_answered": 0, "items_rejected": 0}

    # ------------------------------------------------------------------ producer side
    def enqueue(self, *, app_id: str, kind: str, payload: dict[str, Any], field_key: str = "") -> dict[str, Any]:
        dedupe = payload.get("dedupe_key") or f"{kind}:{app_id}:{field_key}:{uuid.uuid4().hex}"
        scope = self.db.execution_scope()
        if scope and scope.get("kind") == "test_one" and scope.get("application_id") == app_id:
            dedupe = f"test-one:{app_id}:{dedupe}"
        out = self.db.enqueue_reasoning(kind=kind, dedupe_key=dedupe, payload=payload, app_id=app_id, field_key=field_key)
        self._new_work.set()
        return out

    def notify(self) -> None:
        self._new_work.set()

    def open_items_for(self, app_id: str) -> list[dict[str, Any]]:
        return [r for r in self.db.reasoning_for_application(app_id) if r["status"] in ("PENDING", "LEASED")]

    def results_for(self, app_id: str) -> dict[str, dict[str, Any]]:
        """field_key → latest completed result for this application."""
        out: dict[str, dict[str, Any]] = {}
        for r in self.db.reasoning_for_application(app_id):
            if r["status"] in ("DONE", "FAILED") and r["result"] is not None:
                out[r["waiter_field"]] = {**r["result"], "_item_id": r["id"], "_kind": r["kind"], "_status": r["status"]}
        return out

    # ------------------------------------------------------------------ consumer side
    async def get_batch(self, *, owner: str = "astra", max_items: int | None = None, wait_seconds: float = 0,
                        kinds: list[str] | None = None) -> dict[str, Any]:
        max_items = max_items or self.cfg.settings.reasoning.batch_max_items
        scope = self.db.execution_scope()
        scoped_app = str(scope.get("application_id")) if scope and scope.get("kind") == "test_one" else None
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while self.db.pending_reasoning_count(scoped_app) == 0 and time.monotonic() < deadline:
            self._new_work.clear()
            try:
                await asyncio.wait_for(self._new_work.wait(), timeout=min(2.0, max(0.05, deadline - time.monotonic())))
            except TimeoutError:
                pass
        if self.db.pending_reasoning_count(scoped_app) and wait_seconds > 0:
            # Briefly collect more items so one model call covers several questions.
            collect_until = min(deadline, time.monotonic() + self.cfg.settings.reasoning.batch_collect_seconds)
            while time.monotonic() < collect_until and self.db.pending_reasoning_count(scoped_app) < max_items:
                await asyncio.sleep(0.25)
        items = self.db.lease_reasoning(owner, limit=max_items * 2, lease_seconds=self.cfg.settings.reasoning.item_lease_seconds,
                                        kinds=kinds, application_id=scoped_app)
        items, deferred = self._defer_until_research(items)
        give_back = deferred + items[max_items:]
        items = items[:max_items]
        if give_back:
            self.db.return_reasoning([i["id"] for i in give_back])
        batch = self._build_batch(items)
        if items:
            self.metrics["batches"] += 1
            self.metrics["items_delivered"] += len(items)
            app_ids = sorted({w["application_id"] for i in items for w in i["waiters"]})
            self.db.record_model_call(provider=owner, kind="batch_delivered", items=len(items), application_ids=app_ids,
                                      request_chars=len(json.dumps(batch, default=str)), response_chars=0, duration_ms=0)
            for app_id in app_ids:
                self.db.event(app_id, "reasoning", "batch_delivered", f"{len(items)} item(s) sent to {owner}",
                              {"batch_id": batch["batch_id"], "items": [i["id"] for i in items
                                                                        if any(w["application_id"] == app_id for w in i["waiters"])]})
        return batch

    def _defer_until_research(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self.research:
            return items, []
        ready, deferred = [], []
        now = time.time()
        for item in items:
            p = item["payload"]
            company = p.get("company")
            if (p.get("research_recommended") and company and self.research.in_flight(company)
                    and now - item["created_at"] < 60):
                deferred.append(item)
            else:
                ready.append(item)
        return ready, deferred

    def _build_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        batch_id = "batch_" + uuid.uuid4().hex[:12]
        scope = self.db.execution_scope()
        scoped_app = str(scope.get("application_id")) if scope and scope.get("kind") == "test_one" else None
        if not items:
            return {"batch_id": batch_id, "items": [], "pending_total": self.db.pending_reasoning_count(scoped_app),
                    "note": "No reasoning work pending. Call get_status to check progress."}
        apps: dict[str, dict[str, Any]] = {}
        templates_needed: set[str] = set()
        needs_profile = False
        out_items = []
        for item in items:
            p = item["payload"]
            app_ids = [w["application_id"] for w in item["waiters"]]
            app_id = app_ids[0] if app_ids else None
            if app_id and app_id not in apps:
                apps[app_id] = self._application_context(app_id)
            entry: dict[str, Any] = {"item_id": item["id"], "kind": item["kind"], "application_id": app_id,
                                     "shared_by": len(app_ids)}
            if item["kind"] == "question":
                entry.update({k: p.get(k) for k in ("question", "description", "field_type", "options", "multiple",
                                                     "required", "limits", "qclass", "template", "hints")
                              if p.get(k) not in (None, "", [], {})})
                if p.get("research_recommended"):
                    entry["research_recommended"] = True
                if p.get("template"):
                    templates_needed.add(p["template"])
                if p.get("qclass") in {"WRITTEN", "JOB_SPECIFIC", "UNKNOWN"}:
                    needs_profile = True
                if p.get("rejections"):
                    entry["previous_attempt_rejected"] = p["rejections"][-1]
            else:
                entry.update({k: v for k, v in p.items() if k not in {"dedupe_key"}})
                # Older queued page items may predate newly-added URL redaction rules.
                # Sanitize again at the provider boundary without rewriting durable history.
                if item["kind"] == "page":
                    if entry.get("url"):
                        entry["url"] = redact_url(str(entry["url"]))
                    if isinstance(entry.get("page"), dict):
                        entry["page"] = dict(entry["page"])
                        if entry["page"].get("url"):
                            entry["page"]["url"] = redact_url(str(entry["page"]["url"]))
                needs_profile = needs_profile or item["kind"] == "eligibility"
            out_items.append(entry)
        candidate: dict[str, Any] = {"facts": facts_digest(self.cfg)}
        if needs_profile:
            candidate["profile"] = truncate(self.cfg.profile_text, 24000)
        templates = {name: truncate(self.cfg.templates[name], 6000) for name in sorted(templates_needed)
                     if name in self.cfg.templates}
        return {
            "batch_id": batch_id, "instructions": ANSWER_INSTRUCTIONS, "candidate": candidate,
            "templates": templates, "applications": apps, "items": out_items,
            "pending_total": self.db.pending_reasoning_count(scoped_app),
        }

    def _application_context(self, app_id: str) -> dict[str, Any]:
        app = self.db.get_application(app_id)
        job = self.db.get_job(app["job_id"]) or {}
        d = job.get("details") or {}
        ctx: dict[str, Any] = {
            "company": job.get("company"), "title": job.get("title"), "locations": job.get("locations"),
            "description": truncate(d.get("description_text") or "", 3500),
            "requirements": d.get("requirements") or [], "responsibilities": d.get("responsibilities") or [],
            "skills": d.get("skills") or [],
        }
        if self.research:
            res = self.research.cached(job.get("company") or "")
            if res:
                ctx["company_research"] = [{"id": r["id"], "summary": truncate(r["summary"], 2500), "facts": r["facts"][:12],
                                            "sources": r["sources"][:6], "retrieved_at": r["retrieved_at"],
                                            "origin": r["origin"]} for r in res[:2]]
            else:
                company = d.get("company") or {}
                ctx["company_profile"] = {k: company.get(k) for k in ("url", "short_description", "description")
                                          if company.get(k)}
        return ctx

    # ------------------------------------------------------------------ results
    def submit(self, results: list[dict[str, Any]], *, provider: str = "astra") -> dict[str, Any]:
        started = time.monotonic()
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        research_saved = []
        unblock: set[str] = set()
        failed_apps: dict[str, dict[str, str]] = {}
        for res in results:
            if not isinstance(res, dict):
                rejected.append({"item": None, "reason": "result must be an object"})
                continue
            if "research" in res and isinstance(res["research"], dict):
                rid = self._save_research(res["research"])
                if rid:
                    research_saved.append(rid)
                if "item_id" not in res:
                    continue
            item_id = str(res.get("item_id") or "")
            rows = self.db.reasoning_items(ids=[item_id]) if item_id else []
            if not rows:
                rejected.append({"item_id": item_id, "reason": "unknown item_id"})
                continue
            item = rows[0]
            scope = self.db.execution_scope()
            if scope and scope.get("kind") == "test_one":
                selected = str(scope.get("application_id") or "")
                waiter_ids = {str(w["application_id"]) for w in item["waiters"]}
                if waiter_ids != {selected}:
                    rejected.append({"item_id": item_id, "reason": "item is outside the active test-one scope"})
                    continue
            if item["status"] not in ("PENDING", "LEASED"):
                accepted.append({"item_id": item_id, "status": "already_completed"})
                continue
            ok, normalized, reason = self._validate(item, res)
            if not ok:
                payload = dict(item["payload"])
                payload.setdefault("rejections", []).append(reason)
                attempts = len(payload["rejections"])
                if attempts >= 3:
                    waiters = self.db.complete_reasoning(item_id, {"needs_user": f"Answer rejected 3 times: {reason}"},
                                                         status="FAILED")
                    unblock |= {w["application_id"] for w in waiters}
                    failed_apps.update({w["application_id"]: {"item_id": item_id, "reason": reason} for w in waiters})
                else:
                    self.db.requeue_reasoning(item_id, payload)
                rejected.append({"item_id": item_id, "reason": reason, "will_retry": attempts < 3})
                self.metrics["items_rejected"] += 1
                for w in item["waiters"]:
                    self.db.event(w["application_id"], "reasoning", "answer_rejected", reason,
                                  {"item_id": item_id, "attempt": attempts, "will_retry": attempts < 3,
                                   "response_shape": self._response_shape(res)}, level="WARNING")
                continue
            if research_saved:
                normalized.setdefault("research_ids", research_saved)
            normalized["provider"] = provider
            waiters = self.db.complete_reasoning(item_id, normalized)
            self.metrics["items_answered"] += 1
            accepted.append({"item_id": item_id, "status": "accepted"})
            for w in waiters:
                unblock.add(w["application_id"])
                self.db.event(w["application_id"], "reasoning", "answer_received",
                              f"Answer received for: {truncate(item['payload'].get('question') or item['kind'], 120)}",
                              {"item_id": item_id, "field_key": w["field_key"], "provider": provider,
                               "needs_user": normalized.get("needs_user")})
        ready = [a for a in unblock if not self.open_items_for(a)]
        recovered = [a for a in ready if a in failed_apps]
        for app_id in recovered:
            self._park_reasoning_failure(app_id, **failed_apps[app_id])
        self.db.unblock(a for a in ready if a not in failed_apps)
        self.db.record_model_call(provider=provider, kind="batch_answered", items=len(results), application_ids=sorted(unblock),
                                  request_chars=0, response_chars=len(json.dumps(results, default=str)),
                                  duration_ms=int((time.monotonic() - started) * 1000))
        return {"accepted": accepted, "rejected": rejected, "research_saved": research_saved,
                "applications_resumed": len(ready) - len(recovered), "applications_recovered": len(recovered),
                "pending_total": self.db.pending_reasoning_count()}

    def validate_provider_results(self, item_ids: list[str], results: Any) -> list[str]:
        """Validate a provider envelope without mutating queue state."""
        if not isinstance(results, list):
            return ["response.results must be an array"]
        errors: list[str] = []
        by_id: dict[str, dict[str, Any]] = {}
        for index, result in enumerate(results):
            if not isinstance(result, dict):
                errors.append(f"results[{index}] must be an object")
                continue
            item_id = str(result.get("item_id") or "")
            if not item_id:
                errors.append(f"results[{index}] is missing item_id")
            elif item_id in by_id:
                errors.append(f"duplicate result for {item_id}")
            else:
                by_id[item_id] = result
        expected = set(item_ids)
        for missing in sorted(expected - set(by_id)):
            errors.append(f"missing result for {missing}")
        for extra in sorted(set(by_id) - expected):
            errors.append(f"unexpected result for {extra}")
        rows = {r["id"]: r for r in self.db.reasoning_items(ids=item_ids)}
        for item_id in item_ids:
            if item_id not in by_id or item_id not in rows:
                continue
            ok, _, reason = self._validate(rows[item_id], by_id[item_id])
            if not ok:
                errors.append(f"{item_id}: {reason}")
        return errors

    def fail_provider_items(self, item_ids: list[str], reason: str, *, provider: str = "openai") -> None:
        """Convert exhausted provider-output repairs into an explicit recoverable user state."""
        affected: dict[str, dict[str, str]] = {}
        for item in self.db.reasoning_items(ids=item_ids):
            if item["status"] not in ("PENDING", "LEASED"):
                continue
            message = (f"Hybrid reasoning exhausted available tiers: {truncate(reason, 350)}" if provider == "hybrid"
                       else f"{provider} response validation failed after bounded repairs: {truncate(reason, 350)}")
            waiters = self.db.complete_reasoning(item["id"], {"needs_user": message}, status="FAILED")
            for waiter in waiters:
                affected[waiter["application_id"]] = {"item_id": item["id"], "reason": message}
        for app_id, info in affected.items():
            if not self.open_items_for(app_id):
                self._park_reasoning_failure(app_id, **info)

    @staticmethod
    def _response_shape(result: dict[str, Any]) -> dict[str, Any]:
        action = result.get("action")
        return {"keys": sorted(str(k) for k in result)[:20],
                "action_type": action.get("type") if isinstance(action, dict) else None,
                "action_is_object": isinstance(action, dict)}

    def _park_reasoning_failure(self, app_id: str, *, item_id: str, reason: str) -> None:
        app = self.db.get_application(app_id)
        if app["state"] in {"SUBMITTING", "SUBMITTED", "SUBMISSION_UNCONFIRMED", "FAILED", "SKIPPED", "INELIGIBLE"}:
            self.db.unblock([app_id])
            return
        info = {"kind": "reasoning", "reason": reason,
                "instructions": "The model could not produce a safe valid action. Open the focused application tab, "
                                "complete the authentication/page step manually, then press Resume.",
                "fields": [], "missing": [], "since": time.time(), "resume_state": app["state"],
                "reasoning_item_id": item_id}
        self.db.transition(app_id, "NEEDS_USER", reason=reason, data={"needs_user": info},
                           fields={"blocked_on": None, "worker_id": None, "lease_token": None,
                                   "lease_until": None, "available_at": time.time()})
        self.db.event(app_id, "reasoning", "reasoning_recovery",
                      "Reasoning validation exhausted; application parked safely for the user",
                      {"item_id": item_id, "decision": "needs_user", "error": reason}, level="WARNING")

    def _save_research(self, research: dict[str, Any]) -> str | None:
        company = str(research.get("company") or "").strip()
        summary = str(research.get("summary") or "").strip()
        if not company or not summary:
            return None
        sources = [s for s in research.get("sources") or [] if isinstance(s, dict) and str(s.get("url", "")).startswith("http")]
        return self.db.save_research(company=company, company_norm=company_norm(company), topic=research.get("topic") or "company",
                                     summary=truncate(summary, 6000), facts=[str(f) for f in research.get("facts") or []][:30],
                                     sources=sources[:12], origin="astra", ttl=self.cfg.settings.research.cache_ttl_seconds)

    # ------------------------------------------------------------------ validation of model output
    def _validate(self, item: dict[str, Any], res: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
        kind, p = item["kind"], item["payload"]
        if res.get("needs_user"):
            return True, {"needs_user": truncate(str(res["needs_user"]), 500)}, ""
        if kind == "eligibility":
            if not isinstance(res.get("eligible"), bool):
                return False, {}, "eligibility items need eligible: true|false"
            return True, {"eligible": res["eligible"], "reason": truncate(str(res.get("reason") or ""), 800)}, ""
        if kind == "page":
            action = res.get("action")
            if not isinstance(action, dict) or action.get("type") not in {
                    "click_button", "click_text", "click_point", "needs_user", "skip_job", "wait"}:
                return False, {}, "page items need action.type in click_button|click_text|click_point|needs_user|skip_job|wait"
            if action.get("type") == "needs_user":
                return True, {"needs_user": truncate(str(action.get("reason") or res.get("reason") or
                                                           "manual page action required"), 500)}, ""
            return True, {"action": action, "reason": truncate(str(res.get("reason") or ""), 500)}, ""
        if "value" not in res:
            return False, {}, "missing value"
        value = res["value"]
        options = p.get("options") or []
        ftype = p.get("field_type")
        if options and ftype in {"select", "radio", "combobox"}:
            if not isinstance(value, str):
                return False, {}, "option questions need one option label (string)"
            m = match_option(value, options, threshold=0.9)
            if not m.ok:
                return False, {}, f"value {value!r} is not one of the options {options[:20]}"
            value = m.value
        elif ftype == "checkbox_group":
            vals = value if isinstance(value, list) else [value]
            picked = []
            for v in vals:
                m = match_option(str(v), options, threshold=0.9)
                if not m.ok:
                    return False, {}, f"{v!r} is not one of the options"
                picked.append(m.value)
            value = picked
        elif ftype == "checkbox":
            if not isinstance(value, bool):
                if str(value).lower() in {"yes", "true"}:
                    value = True
                elif str(value).lower() in {"no", "false"}:
                    value = False
                else:
                    return False, {}, "checkbox needs true/false"
        elif ftype in ("date", "month"):
            import datetime as _dt

            try:
                _dt.date.fromisoformat(str(value) if ftype == "date" else f"{value}-01")
            except ValueError:
                return False, {}, f"{ftype} fields need an ISO value ({'YYYY-MM-DD' if ftype == 'date' else 'YYYY-MM'})"
            value = str(value)
        else:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str):
                return False, {}, "text questions need a string value"
            value = value.strip()
            if p.get("required") and not value:
                return False, {}, "required question needs a non-empty answer"
            limits = p.get("limits") or {}
            if not fits_limits(value, limits):
                return False, {}, (f"answer exceeds limits {limits} (chars={len(value)}, words={word_count(value)}); "
                                   "shorten it")
            problem = self._fabrication_check(value, p)
            if problem:
                return False, {}, problem
        if value == DECLINE:
            value = None
        return True, {
            "value": value, "fact_paths": [str(x) for x in res.get("fact_paths") or []][:20],
            "template": res.get("template") or p.get("template"), "confidence": _num(res.get("confidence"), 0.8),
            "research_ids": [str(x) for x in res.get("research_ids") or []][:10],
        }, ""

    _PLACEHOLDER = re.compile(r"\[(your|company|insert|name|role|position)[^\]]*\]|lorem ipsum|\bTODO\b|<[A-Z_]{3,}>", re.I)
    _INSTITUTION = re.compile(r"\b((?:University|College|Institute) of [A-Z][\w&.-]*(?: [A-Z][\w&.-]*){0,3}|"
                              r"[A-Z][\w&.-]*(?: [A-Z][\w&.-]*){0,3} (?:University|College|Institute of Technology))\b")
    _GPA = re.compile(r"\bGPA\b[^0-9]{0,15}(\d\.\d{1,2})", re.I)

    def _fabrication_check(self, text: str, payload: dict[str, Any]) -> str | None:
        if self._PLACEHOLDER.search(text):
            return "answer contains a placeholder; write the final text"
        haystack = (self.cfg.profile_text + "\n" + json.dumps(self.cfg.candidate, default=str)).casefold()
        company = str(payload.get("company") or "").casefold()
        for m in self._INSTITUTION.finditer(text):
            name = m.group(1)
            if name.casefold() not in haystack and not (company and company in name.casefold()):
                return (f"answer mentions institution {name!r}, which is not in the candidate profile; "
                        "do not invent credentials")
        gpa = lookup(self.cfg.candidate, "education.gpa")
        for m in self._GPA.finditer(text):
            if gpa is None or abs(float(m.group(1)) - float(gpa)) > 0.005:
                return f"answer states GPA {m.group(1)}, which does not match candidate.education.gpa ({gpa})"
        return None

    # ------------------------------------------------------------------ memory write-back
    def remember(self, *, app: dict[str, Any], job: dict[str, Any], answer: dict[str, Any], submitted: bool) -> None:
        if answer.get("method") in (None, "file", "default") or answer.get("value") in (None, "", []):
            return
        if answer.get("qclass") in {"FILE"}:
            return
        src = answer.get("source") or {}
        company_specific = answer.get("qclass") in {"JOB_SPECIFIC"} or bool(src.get("company_specific"))
        self.db.save_memory({
            "normalized": answer.get("normalized") or normalize_question(answer["question"]),
            "question": answer["question"], "qclass": answer["qclass"], "catalog_id": answer.get("catalog_id"),
            "company_norm": company_norm(job.get("company")) if (company_specific or answer["qclass"] == "WRITTEN") else "",
            "role": job.get("title"), "options_sig": src.get("options_sig", ""), "value": answer["value"],
            "method": answer["method"], "facts_used": src.get("fact_paths") or [f.get("path") for f in src.get("facts", [])
                                                                                  if isinstance(f, dict)],
            "template": src.get("template"), "research_ids": src.get("research_ids") or [],
            "limits": answer.get("limits") or {}, "profile_hash": self.cfg.profile_hash, "application_id": app["id"],
            "submitted": submitted,
        })


def _num(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default
