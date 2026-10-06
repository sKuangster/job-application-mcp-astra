"""Service facade shared by the HTTP API, the MCP server and the stdio MCP proxy.

All methods return JSON-serializable dicts with secrets already removed.
"""

from __future__ import annotations

import time
from typing import Any

from applypilot import __version__
from applypilot.config import list_resume_files
from applypilot.engine.scheduler import Engine
from applypilot.logsetup import tail_logs
from applypilot.questions.resolver import facts_digest
from applypilot.resumes import classify_job
from applypilot.security import redact, redact_url
from applypilot.state import S
from applypilot.textutil import company_norm, normalize_question, truncate

METHOD_KEYS = ("deterministic", "fact", "preference", "eeo", "template", "reused", "generated", "user", "default",
               "research", "file")


class Service:
    def __init__(self, engine: Engine):
        self.e = engine
        self.db = engine.db
        self.cfg = engine.cfg

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        needs = []
        for app in self.db.needs_user():
            info = app["data"].get("needs_user") or {}
            needs.append({"application_id": app["id"], "company": app["company"], "title": app["title"],
                          "state": app["state"], "reason": info.get("reason") or app.get("state_reason"),
                          "kind": info.get("kind") or ("review" if app["state"] == S.READY_TO_SUBMIT else "unexpected"),
                          "instructions": info.get("instructions"), "since": info.get("since") or app["updated_at"],
                          "fields": info.get("fields") or []})
        counts = self.db.state_counts()
        queue_remaining = sum(v for k, v in counts.items() if k in {
            "QUEUED", "CHECKING_ELIGIBILITY", "READY", "OPENING", "APPLYING", "ACCOUNT_REQUIRED", "VERIFYING_EMAIL",
            "FILLING", "ANSWERING", "VALIDATING", "SUBMITTING"})
        readiness = self.cfg.readiness()
        recent = [{"ts": e["ts"], "application_id": e["application_id"], "message": e["message"]}
                  for e in self.db.recent_events(level="ERROR", limit=10)]
        return {
            "control": self.e.control(), "mode": self.e.mode(),
            "backlog_complete": bool(self.db.meta_get("backlog_complete", False)),
            "workers": self.e.workers(),
            "limits": {"browser_workers": self.cfg.settings.automation.max_concurrent_applications,
                       "prep_workers": self.cfg.settings.automation.max_prep_workers,
                       "per_domain": self.cfg.settings.automation.per_domain_concurrency,
                       "max_open_tabs": self.cfg.settings.automation.max_open_application_tabs},
            "counts": counts, "queue_remaining": queue_remaining,
            "today": self.db.today_counts(self.e.today_start()),
            "needs_user": needs, "pending_reasoning": self.db.pending_reasoning_count(),
            "bridge": self.e.bridge.status(),
            "config": {"ready": readiness["ready"], "missing": readiness["missing"], "warnings": readiness["warnings"][:10],
                       "auto_submit": readiness["auto_submit"]},
            "jobs": self.db.job_counts(), "last_sync_at": self.db.meta_get("last_sync_at"),
            "recent_errors": recent or self.e.recent_errors[:10], "version": __version__,
        }

    def compact_status(self) -> dict[str, Any]:
        """Token-efficient status for MCP."""
        s = self.status()
        return {
            "control": s["control"], "mode": s["mode"], "backlog_complete": s["backlog_complete"],
            "queue_remaining": s["queue_remaining"], "today": s["today"], "pending_reasoning": s["pending_reasoning"],
            "workers": [{k: w.get(k) for k in ("worker_id", "company", "title", "state", "activity")} for w in s["workers"]
                        if w.get("status") == "busy"],
            "needs_user": [{k: n[k] for k in ("application_id", "company", "title", "kind", "reason")} for n in s["needs_user"]][:15],
            "browser_connected": s["bridge"]["connected"], "config_ready": s["config"]["ready"],
            "config_missing": s["config"]["missing"], "auto_submit": s["config"]["auto_submit"],
            "next_step": self._next_step(s),
        }

    def _next_step(self, s: dict[str, Any]) -> str:
        if not s["config"]["ready"]:
            return "Configuration incomplete; tell the user which items in config_missing to fill in."
        if not s["bridge"]["connected"]:
            return "Firefox extension not connected; ask the user to open Firefox with the ApplyPilot extension."
        if s["pending_reasoning"]:
            return "Call get_reasoning_batch and answer the items with submit_reasoning."
        if s["control"] != "RUNNING":
            return "Autopilot is not running; call start_autopilot."
        if s["queue_remaining"]:
            return "Workers are busy; call get_reasoning_batch(wait_seconds=25) to wait for questions."
        return "Nothing left to do in this mode."

    # ------------------------------------------------------------------ control / ingest
    async def control(self, action: str, mode: str | None = None) -> dict[str, Any]:
        await self.e.set_control(action, mode)
        return self.status()

    async def sync(self, mode: str = "backlog") -> dict[str, Any]:
        return await self.e.sync(mode)

    async def start_autopilot(self, mode: str = "backlog", refresh: bool = True) -> dict[str, Any]:
        sync = await self.e.sync(mode if mode != "continuous" else "backlog") if refresh else None
        await self.e.set_control("start", mode)
        return {"sync": sync, "status": self.compact_status()}

    # ------------------------------------------------------------------ applications
    def _summary(self, row: dict[str, Any]) -> dict[str, Any]:
        data = row.get("data") or {}
        job = None
        if "company" not in row:
            job = self.db.get_job(row["job_id"]) or {}
        company = row.get("company") or (job or {}).get("company")
        title = row.get("title") or (job or {}).get("title")
        locations = row.get("locations") if "locations" in row else (job or {}).get("locations", [])
        mix: dict[str, int] = {}
        for a in self.db.query("SELECT method, COUNT(*) AS n FROM answers WHERE application_id=? GROUP BY method", (row["id"],)):
            if a["method"]:
                mix[a["method"]] = a["n"]
        return {
            "id": row["id"], "job_id": row["job_id"], "company": company, "title": title,
            "location": ", ".join(locations or [])[:200], "state": row["state"], "state_reason": row.get("state_reason"),
            "ats": row.get("ats") or row.get("job_ats"), "resume": row.get("resume"), "domain": row.get("domain"),
            "simplify_url": row.get("simplify_url") or (job or {}).get("simplify_url"),
            "application_url": redact_url(row.get("apply_url") or (job or {}).get("apply_url") or row.get("direct_url")),
            "current_url": row.get("current_url"), "created_at": row["created_at"], "updated_at": row["updated_at"],
            "submitted_at": row.get("submitted_at"), "attempts": row.get("attempts"), "worker_id": row.get("worker_id"),
            "needs_user": data.get("needs_user") if row["state"] in (S.NEEDS_USER, S.READY_TO_SUBMIT, S.SUBMISSION_UNCONFIRMED) else None,
            "eligibility_decision": (data.get("eligibility") or {}).get("decision"),
            "failure": row["state"] in (S.FAILED, S.NEEDS_USER, S.SUBMISSION_UNCONFIRMED), "method_mix": mix,
            "mode": row.get("mode"), "page_index": row.get("page_index"),
        }

    def search(self, **filters: Any) -> dict[str, Any]:
        rows, total = self.db.search_applications(**filters)
        return {"items": [self._summary(r) for r in rows], "total": total}

    def queue(self, limit: int = 50) -> dict[str, Any]:
        rows, _ = self.db.search_applications(state=",".join([
            "QUEUED", "CHECKING_ELIGIBILITY", "READY", "OPENING", "APPLYING", "ACCOUNT_REQUIRED", "VERIFYING_EMAIL",
            "FILLING", "ANSWERING", "VALIDATING", "SUBMITTING"]), limit=limit, order="priority ASC")
        return {"items": [self._summary(r) for r in rows]}

    def application(self, app_id: str) -> dict[str, Any]:
        app = self.db.get_application(app_id)
        job = self.db.get_job(app["job_id"]) or {}
        events = self.db.events(app_id)
        answers = self.db.answers(app_id)
        research_ids = sorted({rid for a in answers for rid in (a["source"] or {}).get("research_ids") or []})
        research = self.db.research_by_ids(research_ids) or self.db.research(company_norm(job.get("company")), fresh_only=False)
        data = app["data"]
        elig = dict(data.get("eligibility") or {})
        elig["reasoning"] = "; ".join(elig.get("reasons") or [])
        elig["matched_rules"] = elig.get("outcomes") or []
        resume = dict(data.get("resume") or {})
        if resume:
            resume.update({"file": resume.get("filename"), "why": resume.get("reason")})
        details = job.get("details") or {}
        return {
            "summary": self._summary({**app, "company": job.get("company"), "title": job.get("title"),
                                      "locations": job.get("locations"), "simplify_url": job.get("simplify_url"),
                                      "apply_url": job.get("apply_url"), "direct_url": job.get("direct_url"),
                                      "job_ats": job.get("ats")}),
            "job": {"id": job.get("id"), "company": job.get("company"), "title": job.get("title"),
                    "locations": job.get("locations"), "category": job.get("category"), "terms": job.get("terms"),
                    "sponsorship": job.get("sponsorship"), "degrees": job.get("degrees"),
                    "simplify_url": job.get("simplify_url"), "source_url": job.get("direct_url"),
                    "url": redact_url(job.get("apply_url")), "ats": job.get("ats"), "ats_job_key": job.get("ats_job_key"),
                    "date_posted": job.get("date_posted"), "first_seen": job.get("first_seen"),
                    "source_batch": job.get("source_batch"),
                    "description": truncate(details.get("description_text") or "", 6000),
                    "requirements": details.get("requirements") or []},
            "eligibility": elig, "resume": resume,
            "answers": [{**a, "value": a["value"]} for a in answers],
            "research": [{"id": r["id"], "topic": r["topic"], "summary": r["summary"], "facts": r["facts"],
                          "sources": r["sources"], "origin": r["origin"], "created_at": r["retrieved_at"]} for r in research],
            "events": events,
            "errors": [e for e in events if e["level"] in ("WARNING", "ERROR")],
            "pages": data.get("pages") or [],
            "submission": self.db.submission(app_id),
            "gate": data.get("gate"),
            "metrics": data.get("metrics") or {},
            "reasoning": [{"id": r["id"], "kind": r["kind"], "status": r["status"], "question": r["payload"].get("question"),
                           "result": r["result"]} for r in self.db.reasoning_for_application(app_id)],
        }

    def events(self, app_id: str, after_id: int = 0, limit: int = 500) -> dict[str, Any]:
        return {"events": self.db.events(app_id, after_id=after_id, limit=limit)}

    def application_log(self, app_id: str) -> dict[str, Any]:
        """Compact chronological audit for MCP (questions + answers + key events)."""
        d = self.application(app_id)
        return {
            "summary": d["summary"], "eligibility": {"decision": d["eligibility"].get("decision"),
                                                     "reasons": d["eligibility"].get("reasons")},
            "resume": d["resume"].get("filename"),
            "answers": [{"question": truncate(a["question"], 160), "value": a["value"] if not isinstance(a["value"], str)
                         else truncate(a["value"], 600), "method": a["method"], "qclass": a["qclass"],
                         "source": {k: v for k, v in (a["source"] or {}).items() if k in ("facts", "preferences", "eeo",
                                    "template", "memory_id", "derived", "user_answer", "research_ids")},
                         "status": a["status"]} for a in d["answers"]],
            "timeline": [f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} [{e['component']}] {e['message']}"
                         for e in d["events"]][-120:],
            "submission": d["submission"],
        }

    async def action(self, app_id: str, action: str, note: str | None = None) -> dict[str, Any]:
        action = action.lower()
        if action == "resume":
            self.e.resume_application(app_id)
        elif action == "retry":
            self.e.retry_application(app_id)
        elif action == "skip":
            self.e.skip_application(app_id, note)
        elif action == "submit":
            self.e.approve_submit(app_id)
        elif action == "confirm_submitted":
            self.e.confirm_submitted(app_id, note)
        elif action == "open":
            await self.e.open_application(app_id)
        elif action == "cancel":
            self.e.skip_application(app_id, note or "cancelled")
        else:
            raise ValueError(f"unknown action {action}")
        return self._summary(self.db.get_application(app_id))

    def answer(self, app_id: str, field_key: str, value: Any, remember: bool = False) -> dict[str, Any]:
        """User supplies an answer for a NEEDS_USER question (optionally remembered for all jobs)."""
        app = self.db.get_application(app_id)
        answers = {a["field_key"]: a for a in self.db.answers(app_id)}
        ans = answers.get(field_key)
        question = ans["question"] if ans else field_key
        if ans is None:
            fields = (app["data"].get("needs_user") or {}).get("fields") or []
            match = next((f for f in fields if f.get("field_key") == field_key), None)
            if match is None:
                raise KeyError(f"unknown field_key {field_key} for {app_id}")
            question = match["question"]
        record = {"field_key": field_key, "page_index": app["page_index"], "question": question,
                  "normalized": normalize_question(question), "qclass": ans["qclass"] if ans else "FACTUAL",
                  "catalog_id": ans.get("catalog_id") if ans else None, "field_type": ans["field_type"] if ans else "text",
                  "options": ans["options"] if ans else [], "required": True, "limits": ans["limits"] if ans else {},
                  "value": value, "method": "user", "source": {"provided_by": "user", "remembered": remember},
                  "confidence": 1.0, "status": "planned"}
        self.db.upsert_answer(app_id, record)
        if remember:
            from applypilot.questions.catalog import refers_to_company

            job = self.db.get_job(app["job_id"]) or {}
            scope = company_norm(job.get("company")) if refers_to_company(question, job.get("company") or "") else ""
            self.db.save_user_answer(normalize_question(question), question, value, scope)
        self.db.event(app_id, "user", "user_answer", f"User answered: {truncate(question, 100)}",
                      {"field_key": field_key, "remember": remember})
        app = self.db.get_application(app_id)
        info = app["data"].get("needs_user") or {}
        remaining = [f for f in info.get("fields") or [] if f.get("field_key") != field_key and
                     f.get("field_key") not in {a["field_key"] for a in self.db.answers(app_id) if a["method"] == "user"}]
        if app["state"] == S.NEEDS_USER and info.get("kind") == "missing_fact" and not remaining:
            self.e.resume_application(app_id, reason="all missing answers provided")
        return self._summary(self.db.get_application(app_id))

    # ------------------------------------------------------------------ reasoning (MCP)
    async def reasoning_batch(self, max_items: int | None = None, wait_seconds: float = 20) -> dict[str, Any]:
        return await self.e.reasoning.get_batch(owner="astra", max_items=max_items, wait_seconds=min(55.0, wait_seconds))

    def submit_reasoning(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        out = self.e.reasoning.submit(results, provider="astra")
        self.e.wake()
        return out

    def reasoning_items(self, status: str | None = None) -> dict[str, Any]:
        statuses = {"pending": "PENDING,LEASED"}.get((status or "").lower(), status)
        rows = self.db.reasoning_items(status=statuses, limit=200)
        items = []
        for r in rows:
            p = r["payload"]
            items.append({"id": r["id"], "kind": r["kind"], "status": r["status"], "created_at": r["created_at"],
                          "question": p.get("question") or p.get("problem"), "company": p.get("company"),
                          "application_id": (r["waiters"][0]["application_id"] if r["waiters"] else None),
                          "shared_by": len(r["waiters"]), "result": r["result"]})
        return {"items": items}

    def save_research(self, company: str, summary: str, sources: list[dict[str, Any]], facts: list[str] | None = None,
                      topic: str = "company") -> dict[str, Any]:
        rid = self.e.reasoning._save_research({"company": company, "summary": summary, "sources": sources,
                                               "facts": facts or [], "topic": topic})
        return {"research_id": rid}

    def candidate_context(self, sections: list[str] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"facts": facts_digest(self.cfg)}
        sections = sections or ["profile", "templates"]
        if "profile" in sections:
            out["profile"] = self.cfg.profile_text
        if "templates" in sections:
            out["templates"] = self.cfg.templates
        if "preferences" in sections:
            out["preferences"] = redact(self.cfg.preferences.model_dump(exclude={"accounts"}))
        return out

    # ------------------------------------------------------------------ misc
    def logs(self, *, level: str | None = None, component: str | None = None, application_id: str | None = None,
             limit: int = 300) -> dict[str, Any]:
        return {"lines": tail_logs(self.cfg.paths.logs_dir, limit=limit, level=level, component=component,
                                   application_id=application_id)}

    def metrics(self) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT state, started_at, finished_at, submitted_at, data FROM applications WHERE finished_at IS NOT NULL "
            "AND started_at IS NOT NULL ORDER BY finished_at DESC LIMIT 500")
        durations = sorted(r["finished_at"] - r["started_at"] for r in rows if r["state"] == "SUBMITTED")
        by_method = {r["method"]: r["n"] for r in self.db.query(
            "SELECT method, COUNT(*) AS n FROM answers WHERE method IS NOT NULL GROUP BY method")}
        calls = self.db.query("SELECT kind, provider, COUNT(*) AS n, SUM(items) AS items FROM model_calls GROUP BY kind, provider")
        per_day = self.db.query(
            "SELECT date(submitted_at, 'unixepoch', 'localtime') AS date, COUNT(*) AS value FROM applications "
            "WHERE submitted_at IS NOT NULL GROUP BY date ORDER BY date DESC LIMIT 30")
        page_waits, retries = [], 0
        for r in rows:
            import json as _json

            m = (_json.loads(r["data"] or "{}").get("metrics") or {})
            if m.get("page_wait_ms"):
                page_waits.append(m["page_wait_ms"] / 1000)
            retries += int(m.get("retries") or 0)
        return {
            "time_per_application_avg_seconds": round(sum(durations) / len(durations), 1) if durations else None,
            "time_per_application_median_seconds": round(durations[len(durations) // 2], 1) if durations else None,
            "page_wait_seconds_avg": round(sum(page_waits) / len(page_waits), 1) if page_waits else None,
            "reasoning_batches_delivered": sum(c["n"] for c in calls if c["kind"] == "batch_delivered"),
            "reasoning_items_delivered": sum(c["items"] or 0 for c in calls if c["kind"] == "batch_delivered"),
            "model_calls_openai": sum(c["n"] for c in calls if c["provider"] == "openai" and c["kind"] == "completion"),
            "research_runs": self.e.research.metrics["runs"], "research_cache_hits": self.e.research.metrics["cache_hits"],
            "research_pages_fetched": self.e.research.metrics["pages_fetched"],
            "answers_by_method": by_method, "retries": retries,
            "bridge": self.e.bridge.metrics, "engine": {k: v for k, v in self.e.metrics.items()},
            "submitted_per_day": list(reversed(per_day)),
        }

    def config_report(self) -> dict[str, Any]:
        readiness = self.cfg.readiness()
        resumes = []
        for p in list_resume_files(self.cfg.paths.resumes_dir):
            cat, _ = classify_job({"title": p.stem.replace("_", " ")}, self.cfg)
            resumes.append({"name": p.name, "bytes": p.stat().st_size, "category_guess": cat})
        return {"ready": readiness["ready"], "missing": readiness["missing"], "warnings": readiness["warnings"],
                "resumes": resumes, "settings": self.cfg.settings.model_dump(),
                "resume_mappings": self.cfg.preferences.resumes.mappings, "email_provider": self.e.email.name,
                "templates": sorted(self.cfg.templates), "data_dir": str(self.cfg.paths.data_dir),
                "database": str(self.cfg.paths.db_path), "accounts": [
                    {k: a[k] for k in ("account_key", "username", "status", "updated_at")} for a in self.db.accounts()]}

    async def screenshot(self, app_id: str) -> dict[str, Any]:
        res = await self.e.bridge.call(app_id, "screenshot", {"format": "jpeg", "quality": 60}, timeout=20)
        return {"data_url": (res or {}).get("data_url")}

    async def inspect_page(self, app_id: str, include_text: bool = False) -> dict[str, Any]:
        from applypilot.models import PageSnapshot

        raw = await self.e.bridge.call(app_id, "scan", {"include_text": include_text}, timeout=30)
        snap = PageSnapshot.model_validate(raw or {})
        out = snap.compact()
        if include_text:
            out["text"] = truncate(snap.text or "", 4000)
        return out

    async def page_action(self, app_id: str, action: dict[str, Any]) -> dict[str, Any]:
        """Bounded visual/DOM fallback for Astra while the application is parked."""
        from applypilot.engine.runner import _final_click
        from applypilot.models import PageSnapshot

        app = self.db.get_application(app_id)
        if app["lease_until"] and app["lease_until"] > time.time():
            raise ValueError("a worker is currently driving this application; wait until it parks")
        info = app["data"].get("needs_user") or {}
        sub = self.db.submission(app_id)
        if app["state"] != S.NEEDS_USER or info.get("kind") in ("captcha", "review", "verify_submission") or \
                (sub and sub["status"] != "ABORTED"):
            raise ValueError("page actions are only allowed on applications parked for an unexpected page "
                             "(never on review, CAPTCHA or submission states)")
        snap = PageSnapshot.model_validate(await self.e.bridge.call(app_id, "scan", {}, timeout=30) or {})
        if snap.has_filled_fields() and _final_click(action, snap):
            raise ValueError("that action could submit the application; only the engine submits, through its validation gate")
        kind = action.get("type")
        if kind == "click_point":
            res = await self.e.bridge.call(app_id, "click_point", {"x": float(action["x"]), "y": float(action["y"])})
        elif kind == "click_text":
            res = await self.e.bridge.call(app_id, "click_text", {"text": str(action["text"]), "exact": bool(action.get("exact"))})
        elif kind == "click_button":
            res = await self.e.bridge.call(app_id, "click", {"element_id": str(action["button_id"])})
        else:
            raise ValueError("action.type must be click_point, click_text or click_button")
        self.db.event(app_id, "astra", "page_action", f"Astra performed {kind}", {"action": action, "result": res})
        return {"result": res}
