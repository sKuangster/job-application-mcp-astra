"""Per-application state handlers.

``ApplicationRunner.run`` executes the handler for the application's persisted state,
persists the returned next state, and repeats until the application parks (needs Astra,
needs the user, is terminal) or the worker must yield (pause/stop, backoff). Every write
is fenced by the worker's lease token, so a worker that lost its lease (crash recovery
handed the job to another worker) can never corrupt state or double-submit.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import mimetypes
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from applypilot import eligibility as elig
from applypilot.accounts import account_key, credentials_for
from applypilot.bridge import BridgeUnavailable, BrowserError
from applypilot.db import DuplicateSubmission
from applypilot.engine.validation import submission_gate
from applypilot.identity import (
    ats_job_key,
    detect_ats,
    domain_key,
    normalize_url,
    summarize_duplicate,
)
from applypilot.ingest.simplify_page import SimplifyUnavailable, fetch_simplify
from applypilot.models import Button, Option, PageField, PageSnapshot
from applypilot.pdfgen import text_to_pdf
from applypilot.questions.options import real_options, to_field_value
from applypilot.questions.resolver import Resolution, ResolveContext, limits_for, options_signature
from applypilot.resumes import select_resume
from applypilot.security import redact_url
from applypilot.state import TERMINAL, S
from applypilot.textutil import normalize_question, truncate

if TYPE_CHECKING:
    from applypilot.engine.scheduler import Engine

log = logging.getLogger("applypilot.runner")
MAX_STEPS = 60
REDIRECT_KINDS = {"captcha", "confirmation", "already_applied", "closed", "login", "signup", "verification",
                  "job_description"}


@dataclass
class Stop:
    """Yield the application: release the lease (optionally delayed or blocked)."""
    delay: float = 0.0
    blocked_on: str | None = None
    reason: str = ""


class NeedsUser(Exception):
    def __init__(self, kind: str, reason: str, instructions: str, fields: list[dict[str, Any]] | None = None,
                 resume_state: str | None = None):
        super().__init__(reason)
        self.kind, self.reason, self.instructions = kind, reason, instructions
        self.fields = fields or []
        self.resume_state = resume_state


class ApplicationRunner:
    def __init__(self, engine: Engine, app: dict[str, Any], worker_id: str):
        self.e = engine
        self.db = engine.db
        self.cfg = engine.cfg
        self.app = app
        self.worker = worker_id
        self.token = app["lease_token"]
        job = self.db.get_job(app["job_id"])
        assert job is not None
        self.job = job
        self.snap: PageSnapshot | None = None
        self.secret_urls: list[str] = []  # verification links: never recorded beyond their host

    # ------------------------------------------------------------------ persistence helpers
    @property
    def data(self) -> dict[str, Any]:
        return self.app["data"]

    def event(self, component: str, etype: str, message: str, data: dict[str, Any] | None = None,
              level: str = "INFO") -> None:
        self.db.event(self.app["id"], component, etype, message, data, worker_id=self.worker, level=level)

    def save(self, data: dict[str, Any] | None = None, **fields: Any) -> None:
        self.app = self.db.update_application(self.app["id"], worker_id=self.worker, token=self.token, data=data, **fields)

    def go(self, state: str, reason: str | None = None, data: dict[str, Any] | None = None, **fields: Any) -> str:
        self.app = self.db.transition(self.app["id"], state, worker_id=self.worker, token=self.token, reason=reason,
                                      data=data, fields=fields or None)
        self.e.set_activity(self.worker, self.app, _ACTIVITY.get(state, state.title()))
        return state

    def metric(self, key: str, inc: float = 1) -> None:
        m = dict(self.data.get("metrics") or {})
        m[key] = round(m.get(key, 0) + inc, 3)
        self.app["data"]["metrics"] = m

    def flush_metrics(self) -> None:
        if self.data.get("metrics"):
            self.save(data={"metrics": self.data["metrics"]})

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        started = time.monotonic()
        try:
            for _ in range(MAX_STEPS):
                state = self.app["state"]
                if state in TERMINAL or state in (S.WAITING_FOR_CAPTCHA, S.NEEDS_USER, S.READY_TO_SUBMIT):
                    await self._finish_terminal()
                    return
                if self.e.should_yield() and state != S.SUBMITTING:
                    self.release(Stop(reason="paused"))
                    return
                handler = getattr(self, f"h_{state.lower()}", None)
                if handler is None:
                    raise RuntimeError(f"no handler for state {state}")
                self.e.set_activity(self.worker, self.app, _ACTIVITY.get(state, state.title()))
                try:
                    outcome = await handler()
                except NeedsUser as nu:
                    outcome = self.needs_user(nu)
                if isinstance(outcome, Stop):
                    self.release(outcome)
                    return
                if outcome and outcome != self.app["state"]:
                    self.go(outcome)
            raise NeedsUser("unexpected", "the application did not converge after many steps",
                            "Open the application tab, check where it is stuck, then press Resume or Skip.")
        except NeedsUser as nu:
            self.release(self.needs_user(nu))
        finally:
            self.metric("seconds_active", time.monotonic() - started)
            try:
                self.flush_metrics()
            except Exception:
                pass

    def release(self, stop: Stop) -> None:
        delay = stop.delay
        if delay:
            delay = delay * random.uniform(0.85, 1.15)
        self.db.release(self.app["id"], self.worker, self.token, delay=delay, blocked_on=stop.blocked_on)
        if stop.blocked_on == "reasoning" and not self.e.reasoning.open_items_for(self.app["id"]):
            # Answers may have landed between enqueue and park: never leave the app blocked.
            self.db.unblock([self.app["id"]])
        if stop.reason and stop.reason != "paused":
            self.event("queue", "released", f"Released: {stop.reason}", {"delay": round(delay, 1), "blocked_on": stop.blocked_on})

    def needs_user(self, nu: NeedsUser) -> Stop:
        info = {"kind": nu.kind, "reason": nu.reason, "instructions": nu.instructions, "fields": nu.fields,
                "missing": nu.fields, "since": time.time(), "resume_state": nu.resume_state or self.app["state"]}
        if self.app["state"] != S.NEEDS_USER:
            self.go(S.NEEDS_USER, nu.reason, data={"needs_user": info})
        self.event("intervention", "needs_user", f"{nu.kind}: {nu.reason}", info, level="WARNING")
        self.e.notify_attention(self.app, info)
        return Stop(reason=f"needs user: {nu.kind}")

    async def wait_for_captcha(self, snap: PageSnapshot, *, reason: str, resume_state: str,
                               phase: str, safe_to_retry_submit: bool = False) -> Stop:
        """Persist and park a CAPTCHA without retaining a worker lease.

        Only metadata needed for recovery is retained. The watcher subsequently issues
        read-only DOM scans; it never sends an action into a challenge frame or widget.
        """
        now = time.time()
        screenshot = await self.screenshot("captcha-detected")
        previous = dict(self.data.get("captcha_wait") or {})
        occurrence = max(int(previous.get("occurrence") or 0), len(self.data.get("captcha_history") or [])) + 1
        wait = {
            "detected_at": now,
            "last_checked_at": now,
            "check_count": 0,
            "occurrence": occurrence,
            "resume_state": resume_state,
            "phase": phase,
            "page_url": redact_url(snap.url),
            "domain": domain_key(snap.url),
            "page_fingerprint": snap.fingerprint,
            "challenge_type": snap.captcha.kind or "unknown",
            "detail": truncate(snap.captcha.detail or "challenge page", 160),
            "screenshot": screenshot,
            "safe_to_retry_submit": safe_to_retry_submit,
            "navigation_observed": False,
        }
        info = {
            "kind": "captcha", "reason": reason,
            "instructions": "Complete the human-verification challenge in Firefox. ApplyPilot will detect completion "
                            "and resume automatically; do not submit the application manually.",
            "fields": [], "missing": [], "since": now, "resume_state": resume_state,
        }
        self.go(S.WAITING_FOR_CAPTCHA, reason, data={"captcha_wait": wait, "needs_user": info})
        self.event("captcha", "captcha_detected", reason, wait, level="WARNING")
        self.e.notify_attention(self.app, info)
        # Bring the correct real Firefox window forward. Failure is harmless: the
        # notification/dashboard Open action can focus it later.
        with contextlib.suppress(BrowserError, BridgeUnavailable):
            await self.cmd("focus", {}, timeout=10, audit=False)
        return Stop(reason="waiting for human CAPTCHA completion")

    async def _finish_terminal(self) -> None:
        state = self.app["state"]
        policy = self.cfg.settings.browser.close_tab_on
        if state in TERMINAL and self.e.bridge.connected and self.app["id"] in self.e.bridge.owned and (
                policy == "terminal" or (policy == "submitted" and state == S.SUBMITTED)):
            try:
                await self.e.bridge.call(self.app["id"], "close", {}, timeout=10)
            except (BrowserError, BridgeUnavailable):
                pass
        self.db.release(self.app["id"], self.worker, self.token)

    # ------------------------------------------------------------------ browser helpers
    async def cmd(self, action: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None,
                  audit: bool = True) -> Any:
        started = time.monotonic()
        err: str | None = None
        try:
            return await self.e.bridge.call(self.app["id"], action, payload or {},
                                            timeout=timeout or self.cfg.settings.browser.command_timeout)
        except (BrowserError, BridgeUnavailable) as exc:
            err = f"{getattr(exc, 'code', 'BRIDGE')}: {exc}"
            raise
        finally:
            ms = int((time.monotonic() - started) * 1000)
            if action in ("open", "navigate", "wait_ready"):
                self.metric("page_wait_ms", ms)
            self.metric("browser_commands")
            verbose = self.cfg.settings.logging.verbose_browser
            if (audit or err or verbose) and (action not in ("scan", "tab_info", "wait_ready") or err or verbose):
                self.event("browser", f"browser_{action}", f"{action} {'failed' if err else 'ok'} ({ms} ms)",
                           {"action": action, "ms": ms, "payload": _audit_payload(action, payload), "error": err},
                           level="WARNING" if err else "INFO")

    async def scan(self, *, include_text: bool = False, record: bool = True) -> PageSnapshot:
        raw = await self.cmd("scan", {"include_text": include_text, "max_text": 4000}, audit=False)
        snap = PageSnapshot.model_validate(raw or {})
        if any(snap.url.startswith(u.split("?")[0]) for u in self.secret_urls):
            snap = snap.model_copy(update={"url": _origin(snap.url)})
        self.snap = snap
        pending = dict(self.data.get("captcha_resume_pending") or {})
        if pending and not (snap.captcha.present and snap.captcha.interactive) and snap.page_kind != "captcha":
            continued_at = time.time()
            completed = {
                **pending, "continued_at": continued_at, "continued_state": self.app["state"],
                "continued_url": redact_url(snap.url), "continued_page_kind": snap.page_kind,
                "successfully_continued": True,
            }
            history = (list(self.data.get("captcha_history") or []) + [completed])[-20:]
            self.save(data={"captcha_resume_pending": None, "captcha_wait": None, "captcha_history": history})
            self.event("captcha", "captcha_application_continued", "Application continued after CAPTCHA", completed)
        if record:
            pages = list(self.data.get("pages") or [])
            if not pages or pages[-1].get("fingerprint") != snap.fingerprint:
                entry = {"url": redact_url(snap.url), "kind": snap.page_kind, "fingerprint": snap.fingerprint,
                         "heading": truncate(snap.heading, 120), "fields": len(snap.fields), "ts": time.time(),
                         "step": snap.step.model_dump() if snap.step else None}
                pages = (pages + [entry])[-60:]
                self.save(data={"pages": pages}, current_url=redact_url(snap.url)[:2000], ats=snap.ats or self.app.get("ats"))
                self.event("browser", "page_visited", f"{snap.page_kind}: {truncate(snap.heading or snap.title, 100)}",
                           {"url": redact_url(snap.url), "page_kind": snap.page_kind, "fields": len(snap.fields),
                            "buttons": [b.text for b in snap.buttons][:12], "errors": [e.text for e in snap.errors][:5],
                            "step": entry["step"]})
        return snap

    async def wait_ready(self, timeout: float | None = None) -> None:
        await self.cmd("wait_ready", {"timeout_ms": int((timeout or self.cfg.settings.browser.page_load_timeout) * 1000),
                                      "quiet_ms": self.cfg.settings.browser.ready_quiet_ms},
                       timeout=(timeout or self.cfg.settings.browser.page_load_timeout) + 5, audit=False)

    async def click_and_wait(self, button: Button | str, *, expect_change: bool = True, settle: float = 15) -> tuple[PageSnapshot, bool]:
        before = self.snap.fingerprint if self.snap else ""
        before_url = self.snap.url if self.snap else ""
        element_id = button.id if isinstance(button, Button) else button
        text = button.text if isinstance(button, Button) else element_id
        res = await self.cmd("click", {"element_id": element_id})
        href = (res or {}).get("href") if isinstance(res, dict) else None
        self.event("browser", "clicked", f"Clicked '{truncate(text, 60)}'", {"element_id": element_id, "href": redact_url(href)})
        deadline = time.monotonic() + settle
        snap = self.snap
        while True:
            try:
                await self.wait_ready(timeout=min(self.cfg.settings.browser.page_load_timeout, max(2, deadline - time.monotonic())))
            except BrowserError as exc:
                if exc.code not in ("TIMEOUT", "CONTENT_SCRIPT_UNAVAILABLE"):
                    raise
            try:
                snap = await self.scan()
            except BrowserError as exc:
                if exc.code != "CONTENT_SCRIPT_UNAVAILABLE":
                    raise
                await asyncio.sleep(0.5)
                continue
            changed = snap.fingerprint != before or snap.url != before_url
            if changed or not expect_change or snap.errors or snap.captcha.present or time.monotonic() > deadline:
                return snap, changed
            await asyncio.sleep(0.6)

    async def screenshot(self, label: str) -> str | None:
        if not self.cfg.settings.browser.screenshot_on_failure or not self.e.bridge.connected:
            return None
        try:
            res = await self.e.bridge.call(self.app["id"], "screenshot", {"format": "jpeg", "quality": 70}, timeout=15)
            data_url = (res or {}).get("data_url") or ""
            if "," not in data_url:
                return None
            folder = self.cfg.paths.artifacts_dir / self.app["id"]
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{int(time.time())}-{label}.jpg"
            path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            self.event("diagnostics", "screenshot", f"Screenshot saved ({label})", {"path": str(path)})
            return str(path)
        except Exception as exc:
            log.debug("screenshot failed: %s", exc)
            return None

    # ------------------------------------------------------------------ prep pool
    async def h_queued(self) -> str:
        return S.CHECKING_ELIGIBILITY

    async def h_checking_eligibility(self) -> str | Stop:
        if self.data.get("eligibility_review"):
            return self._finish_review()
        # 1. Duplicate check before spending effort.
        dup = [a for a in self.db.find_equivalent_applications(self.job, exclude_app=self.app["id"])
               if a["state"] not in ("INELIGIBLE", "FAILED", "SKIPPED", "DISCOVERED")
               # Equivalent listings still in prep: the lowest application id wins, so they never skip each other.
               and not (a["state"] in ("QUEUED", "CHECKING_ELIGIBILITY") and a["id"] > self.app["id"])]
        if dup:
            self.event("dedupe", "duplicate_suspected", "Equivalent posting already applied/in progress", {"conflicts": dup[:5]},
                       level="WARNING")
            return self.go(S.SKIPPED, f"possible duplicate of {summarize_duplicate(dup)}", data={"duplicate_of": dup[:5]})
        # 2. Simplify enrichment over HTTP (description, requirements, ATS link).
        try:
            await self._enrich()
        except _Skip as skip:
            return self.go(S.SKIPPED, f"duplicate: {skip}")
        # 3. Eligibility.
        result = elig.evaluate(self.job, self.cfg)
        self.save(data={"eligibility": result.to_dict()})
        self.event("eligibility", "eligibility_checked", f"Eligibility: {result.decision}", result.to_dict())
        if result.decision == "ineligible":
            return self.go(S.INELIGIBLE, "; ".join(result.reasons)[:500])
        if result.decision == "skip_preference":
            return self.go(S.SKIPPED, "; ".join(result.reasons)[:500])
        if result.decision == "needs_user":
            raise NeedsUser("missing_fact", "; ".join(result.reasons), "Fill in the named candidate facts in "
                            "config/candidate.yaml, then press Resume.", resume_state=S.CHECKING_ELIGIBILITY)
        if result.decision == "review":
            payload = {"rule_reviews": result.review, "company": self.job["company"], "title": self.job["title"],
                       "question": "Is the candidate eligible under these explicit requirements? Treat preferences as "
                                   "non-blocking; answer eligible=false only for a clear hard incompatibility.",
                       "dedupe_key": f"elig:{self.job['id']}:{self.cfg.profile_hash[:10]}"}
            self.e.reasoning.enqueue(app_id=self.app["id"], kind="eligibility", payload=payload, field_key="__eligibility__")
            self.save(data={"eligibility_review": True})
            self.event("eligibility", "eligibility_review", "Graduation/structural requirement sent to Astra for review",
                       {"reviews": result.review})
            return Stop(blocked_on="reasoning", reason="eligibility review by Astra")
        return self._choose_resume()

    def _finish_review(self) -> str | Stop:
        result = self.e.reasoning.results_for(self.app["id"]).get("__eligibility__")
        if result is None:
            if self.e.reasoning.open_items_for(self.app["id"]):
                return Stop(blocked_on="reasoning", reason="waiting for eligibility review")
            self.save(data={"eligibility_review": False})
            return self._choose_resume()
        decision = dict(self.data.get("eligibility") or {})
        decision["astra_review"] = {k: v for k, v in result.items() if not k.startswith("_")}
        self.save(data={"eligibility": decision, "eligibility_review": False})
        if result.get("needs_user"):
            raise NeedsUser("missing_fact", str(result["needs_user"]), "Add the missing fact to config/candidate.yaml and press Resume.",
                            resume_state=S.CHECKING_ELIGIBILITY)
        if result.get("eligible") is False:
            decision["decision"] = "ineligible"
            self.save(data={"eligibility": decision})
            return self.go(S.INELIGIBLE, f"Astra review: {result.get('reason')}")
        decision["decision"] = "eligible"
        self.save(data={"eligibility": decision})
        return self._choose_resume()

    def _choose_resume(self) -> str:
        choice = select_resume(self.job, self.cfg)
        self.event("resume", "resume_selected", f"Resume: {choice.filename or 'none'} ({choice.method})", choice.to_dict())
        if not choice.filename:
            raise NeedsUser("missing_resume", "No resume file available", "Put your resume PDF(s) in resumes/ and press Resume.",
                            resume_state=S.CHECKING_ELIGIBILITY)
        url = self.job.get("apply_url") or self.job.get("simplify_url") or self.job.get("direct_url")
        self.save(data={"resume": choice.to_dict()}, resume=choice.filename, domain=domain_key(url),
                  ats=detect_ats(url))
        return S.READY

    async def _enrich(self) -> None:
        if self.job.get("details") or not self.job.get("simplify_url") or not self.cfg.settings.ingestion.simplify_fetch:
            return
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                details = await fetch_simplify(self.job["simplify_url"], timeout=20)
                break
            except SimplifyUnavailable as exc:
                last_exc = exc
                await asyncio.sleep(1.5 * (2 ** attempt))
        else:
            self.event("ingest", "simplify_unavailable", f"Simplify page unavailable: {last_exc}; will open it in Firefox",
                       level="WARNING")
            return
        apply_url = details.get("apply_url")
        updates: dict[str, Any] = {"details": details, "details_fetched_at": time.time()}
        if apply_url:
            updates["apply_url"] = apply_url
            key = ats_job_key(apply_url, details.get("tracked_obj"))
            if key:
                updates["ats_job_key"] = key
            updates["ats"] = detect_ats(apply_url)
        self.db.update_job(self.job["id"], **updates)
        self.job = self.db.get_job(self.job["id"]) or self.job
        aliases = []
        if updates.get("ats_job_key"):
            aliases.append(("ats", updates["ats_job_key"]))
        if apply_url and normalize_url(apply_url):
            aliases.append(("url", normalize_url(apply_url)))
        others = self.db.add_job_aliases(self.job["id"], aliases)
        self.event("ingest", "simplify_enriched", "Parsed Simplify job page",
                   {"apply_url": redact_url(apply_url), "requirements": len(details.get("requirements") or []),
                    "tracked_obj": details.get("tracked_obj"), "alias_conflicts": others})
        if others:
            conflicts = [a for o in others if (a := self.db.application_for_job(o)) and a["state"] not in ("INELIGIBLE", "SKIPPED", "FAILED")]
            if conflicts:
                raise _Skip(f"same ATS posting as job(s) {', '.join(others)} (already queued/applied)")

    # ------------------------------------------------------------------ browser pool
    async def h_ready(self) -> str:
        if not self.e.bridge.connected:
            return Stop(delay=10, reason="waiting for Firefox extension")  # type: ignore[return-value]
        return S.OPENING

    async def h_opening(self) -> str:
        url = self.job.get("apply_url") or self.job.get("simplify_url") or self.job.get("direct_url")
        if not url:
            return self.go(S.FAILED, "job has no usable application link")
        if self.app["id"] in self.e.bridge.owned and self.data.get("pages"):
            # Recovered after a crash/restart: the owned tab still exists; continue from the page.
            return S.APPLYING
        res = await self.cmd("open", {"url": url, "strategy": self.cfg.settings.browser.tab_strategy,
                                      "active": self.cfg.settings.browser.focus_new_windows},
                             timeout=self.cfg.settings.browser.page_load_timeout + 10)
        self.save(tab_id=(res or {}).get("tab_id") or 0, window_id=(res or {}).get("window_id"),
                  current_url=redact_url((res or {}).get("url") or url)[:2000])
        try:
            await self.wait_ready()
        except BrowserError as exc:
            if exc.code != "TIMEOUT":
                raise
        self.event("browser", "opened", f"Opened {'Simplify page' if 'simplify.jobs' in url else 'application'}",
                   {"url": redact_url(url), "via": "simplify_click" if self.job.get("apply_url") else "link"})
        return S.APPLYING

    async def h_applying(self) -> str | Stop:
        snap = await self.scan()
        kind = snap.page_kind
        if snap.captcha.present and snap.captcha.interactive or kind == "captcha":
            return await self.wait_for_captcha(
                snap, reason=f"CAPTCHA ({snap.captcha.kind or 'challenge'}) on {truncate(snap.title, 60)}",
                resume_state=S.APPLYING, phase="application")
        if kind == "confirmation" or snap.confirmation.detected:
            sub = self.db.submission(self.app["id"])
            if sub and sub["status"] in ("SUBMITTING", "SUBMISSION_UNCONFIRMED"):
                return S.SUBMITTING
            raise NeedsUser("unexpected", "a confirmation page appeared before ApplyPilot submitted anything",
                            "Check whether you already applied. Use Skip if so, otherwise Retry.")
        if kind == "already_applied":
            return self.go(S.SKIPPED, "the ATS reports an existing application for this posting",
                           data={"already_applied_evidence": truncate(snap.heading or snap.title, 200)})
        if kind == "closed":
            return self.go(S.SKIPPED, "posting is closed / no longer accepting applications")
        if kind in ("login", "signup"):
            return S.ACCOUNT_REQUIRED
        if kind == "verification":
            return S.VERIFYING_EMAIL
        if kind == "job_description" or (not snap.fields and snap.buttons_of("apply")):
            return await self._click_apply(snap)
        if snap.fields:
            return S.FILLING
        if snap.buttons_of("submit") or snap.buttons_of("next"):
            return S.VALIDATING  # review/summary or intro page: just navigate (gate still applies)
        return await self._page_help(snap, "The page has no recognizable form or next step.")

    async def _click_apply(self, snap: PageSnapshot) -> str | Stop:
        clicks = int(self.data.get("apply_clicks") or 0)
        buttons = snap.buttons_of("apply")
        # Prefer "Apply manually" (Workday) over autofill/LinkedIn variants.
        buttons.sort(key=lambda b: (0 if "manual" in b.text.lower() else 1 if "apply" in b.text.lower() else 2,
                                    1 if any(w in b.text.lower() for w in ("linkedin", "indeed", "resume", "autofill")) else 0))
        if not buttons or clicks >= 4:
            return await self._page_help(snap, "Could not find an Apply button on the job page.")
        self.save(data={"apply_clicks": clicks + 1})
        await self.click_and_wait(buttons[0])
        return S.APPLYING

    async def _page_help(self, snap: PageSnapshot, problem: str) -> str | Stop:
        """Visual/semantic fallback: ask Astra what to do on an unrecognized page (bounded)."""
        helps = int(self.data.get("page_help") or 0)
        result = self.e.reasoning.results_for(self.app["id"]).get(f"__page__{snap.fingerprint}")
        used = list(self.data.get("page_items_used") or [])
        if result and result.get("_item_id") not in used:
            self.save(data={"page_items_used": (used + [result.get("_item_id")])[-50:]})  # each answer applies once
            return await self._apply_page_action(result, snap)
        if helps >= 3:
            raise NeedsUser("unexpected", problem, "Open the application window, move it to the next step manually, "
                            "then press Resume (or Skip).")
        full = await self.scan(include_text=True, record=False)
        payload = {"problem": problem, "url": redact_url(full.url), "title": full.title, "page": full.compact(),
                   "text_excerpt": truncate(full.text or "", 3000), "screenshot_tool": "get_page_screenshot",
                   "dedupe_key": f"page:{self.app['id']}:{snap.fingerprint}"}
        self.e.reasoning.enqueue(app_id=self.app["id"], kind="page", payload=payload, field_key=f"__page__{snap.fingerprint}")
        self.save(data={"page_help": helps + 1})
        self.event("reasoning", "page_help_requested", problem, {"fingerprint": snap.fingerprint})
        return Stop(blocked_on="reasoning", reason="page understanding by Astra")

    async def _apply_page_action(self, result: dict[str, Any], snap: PageSnapshot) -> str | Stop:
        if result.get("needs_user"):
            raise NeedsUser("unexpected", str(result["needs_user"]), "Follow the note, then press Resume.")
        action = result.get("action") or {}
        kind = action.get("type")
        self.event("reasoning", "page_action", f"Astra page action: {kind}", {"action": action, "reason": result.get("reason")})
        if snap.has_filled_fields() and _final_click(action, snap):
            # Nothing that could send the application may bypass the submission gate.
            raise NeedsUser("review", "the suggested action would submit the application outside the validation gate",
                            "Check the page: if it is the final step, submit it yourself and use 'Mark submitted'; "
                            "otherwise move it forward manually and press Resume.", resume_state=S.APPLYING)
        if kind == "click_button" and action.get("button_id"):
            await self.click_and_wait(str(action["button_id"]))
        elif kind == "click_text" and action.get("text"):
            await self.cmd("click_text", {"text": action["text"], "exact": bool(action.get("exact"))})
            await self.wait_ready()
        elif kind == "click_point" and action.get("x") is not None:
            await self.cmd("click_point", {"x": float(action["x"]), "y": float(action["y"])})
            await self.wait_ready()
        elif kind == "skip_job":
            return self.go(S.SKIPPED, f"Astra: {action.get('reason') or result.get('reason') or 'not applicable'}")
        elif kind == "wait":
            waits = int(self.data.get("page_waits") or 0)
            if waits >= 3:
                raise NeedsUser("unexpected", "the page did not change after waiting", "Check the window, then Resume or Skip.")
            self.save(data={"page_waits": waits + 1})
            return Stop(delay=min(300.0, float(action.get("seconds") or 20)), reason="Astra asked to wait")
        else:
            raise NeedsUser("unexpected", f"unsupported page action {kind}", "Handle the page manually, then Resume.")
        return S.APPLYING

    # ------------------------------------------------------------------ filling
    def _ctx(self) -> ResolveContext:
        return ResolveContext(job=self.job, application_id=self.app["id"], page_index=self.app["page_index"],
                              resume=self.app.get("resume"), alternates=dict(self.data.get("alternates") or {}))

    async def h_filling(self) -> str | Stop:
        snap = await self.scan()
        if snap.page_kind in REDIRECT_KINDS or (snap.captcha.present and snap.captcha.interactive):
            return S.APPLYING
        return await self._fill_page(snap)

    async def h_answering(self) -> str | Stop:
        if self.e.reasoning.open_items_for(self.app["id"]):
            return Stop(blocked_on="reasoning", reason="still waiting for Astra")
        snap = await self.scan()
        if snap.page_kind in REDIRECT_KINDS:
            return S.APPLYING
        return await self._fill_page(snap)

    async def _fill_page(self, snap: PageSnapshot) -> str | Stop:
        ctx = self._ctx()
        existing = {a["field_key"]: a for a in self.db.answers(self.app["id"])}
        results = self.e.reasoning.results_for(self.app["id"])
        items: list[dict[str, Any]] = []
        plans: dict[str, tuple[PageField, Resolution]] = {}
        uploads: list[tuple[PageField, Resolution]] = []
        missing: list[dict[str, Any]] = []
        waiting: list[str] = []
        research_needed = False
        for f in snap.fields:
            if f.disabled:
                continue
            res = self._resolve_with_results(f, ctx, existing.get(f.key), results.get(f.key))
            if res.status == "file":
                uploads.append((f, res))
                continue
            audit = res.audit(f, ctx.page_index)
            audit["source"] = {**audit["source"], "options_sig": options_signature(f),
                               "company_specific": res.company_specific}
            if res.status == "resolved":
                if not _already(f, res.value):
                    items.append({"field_id": f.id, "value": res.value})
                    plans[f.id] = (f, res)
                    audit["status"] = "planned"
                else:
                    audit["status"] = "verified"
                self.metric("deterministic_fields" if res.method not in ("generated", "reused", "user") else f"{res.method}_answers")
                self.db.upsert_answer(self.app["id"], audit)
            elif res.status == "skip":
                self.db.upsert_answer(self.app["id"], audit)
            elif res.status == "needs_user":
                self.db.upsert_answer(self.app["id"], audit)
                missing.append({"field_key": f.key, "question": f.question, "type": f.type, "options": real_options(f)[:30],
                                "required": f.required, "missing": res.missing, "note": res.note})
            elif res.status == "reasoning":
                research_needed |= self._queue_reasoning(f, res, audit)
                waiting.append(f.key)
        # Fill everything we know first (even if other questions are pending) — fewer page visits later.
        if items:
            for f2 in await self._fill(items, plans):
                # The widget's real options did not contain the planned wording: resolve again with them.
                res = self.e.resolver.resolve(f2, ctx)
                audit = {**res.audit(f2, ctx.page_index), "source": {**res.sources, "options_sig": options_signature(f2)}}
                if res.status == "reasoning":
                    research_needed |= self._queue_reasoning(f2, res, audit)
                    waiting.append(f2.key)
                elif res.status == "resolved":
                    await self._fill([{"field_id": f2.id, "value": res.value}], {f2.id: (f2, res)})
                elif res.status == "needs_user":
                    self.db.upsert_answer(self.app["id"], audit)
                    missing.append({"field_key": f2.key, "question": f2.question, "type": f2.type,
                                    "options": real_options(f2)[:30], "required": f2.required, "missing": res.missing,
                                    "note": res.note})
        for f, res in uploads:
            await self._upload(f, res)
        if research_needed:
            self.e.research.schedule(self.job["company"], self.job)
        passes = dict(self.data.get("fill_passes") or {})
        passes[snap.fingerprint] = passes.get(snap.fingerprint, 0) + 1
        self.save(data={"fill_passes": passes})
        if missing:
            raise NeedsUser("missing_fact", f"{len(missing)} required question(s) need facts that are not configured",
                            "Answer them in the dashboard (optionally 'remember'), or add the facts to config, then Resume.",
                            fields=missing, resume_state=S.FILLING)
        if waiting:
            return self.go(S.ANSWERING, f"waiting for Astra ({len(waiting)} question(s))")  # parks below
        # Conditional questions may have appeared after answering (e.g. "If yes, explain").
        if items and passes[snap.fingerprint] <= 3:
            after = await self.scan(record=False)
            known = {f.key for f in snap.fields}
            if any(f.key not in known and not f.disabled for f in after.fields):
                self.event("filling", "conditional_fields", "New fields appeared after filling; resolving them")
                return await self._fill_page(after)
        return S.VALIDATING

    def _queue_reasoning(self, f: PageField, res: Resolution, audit: dict[str, Any]) -> bool:
        assert res.reasoning is not None
        open_for_field = [r for r in self.e.reasoning.open_items_for(self.app["id"]) if r["waiter_field"] == f.key]
        if not open_for_field:
            out = self.e.reasoning.enqueue(app_id=self.app["id"], kind="question", payload=res.reasoning, field_key=f.key)
            self.event("reasoning", "question_queued", f"Needs Astra: {truncate(f.question, 100)}",
                       {"field_key": f.key, "qclass": res.qclass, "template": res.reasoning.get("template"),
                        "shared_with_existing": out["joined"], "item_id": out["id"]})
            self.metric("reasoning_items")
        self.db.upsert_answer(self.app["id"], {**audit, "status": "pending"})
        return bool(res.reasoning.get("research_recommended"))

    def _resolve_with_results(self, f: PageField, ctx: ResolveContext, existing: dict[str, Any] | None,
                              result: dict[str, Any] | None) -> Resolution:
        if result is not None and (existing is None or existing.get("status") in ("pending", "filled", "verified", "failed")
                                   and existing.get("method") in ("generated", None)):
            if result.get("needs_user"):
                return Resolution(f.key, f.question, normalize_question(f.question), existing["qclass"] if existing else "UNKNOWN",
                                  existing.get("catalog_id") if existing else None, "needs_user",
                                  missing=[str(result["needs_user"])], note="Astra could not establish this answer")
            mapped = to_field_value(f, [result.get("value")])
            if mapped.ok:
                sources = {"reasoning_item": result.get("_item_id"), "fact_paths": result.get("fact_paths"),
                           "template": result.get("template"), "research_ids": result.get("research_ids"),
                           "provider": "astra"}
                return Resolution(f.key, f.question, normalize_question(f.question),
                                  existing["qclass"] if existing else "UNKNOWN",
                                  existing.get("catalog_id") if existing else None, "resolved", value=mapped.value,
                                  method="generated", sources=sources, confidence=float(result.get("confidence") or 0.8))
        if existing and existing.get("method") == "user" and existing.get("value") not in (None, ""):
            mapped = to_field_value(f, [existing["value"]])
            if mapped.ok:
                return Resolution(f.key, f.question, existing["normalized"], existing["qclass"], existing.get("catalog_id"),
                                  "resolved", value=mapped.value, method="user", sources=existing.get("source") or {})
        return self.e.resolver.resolve(f, ctx)

    async def _fill(self, items: list[dict[str, Any]], plans: dict[str, tuple[PageField, Resolution]]) -> list[PageField]:
        """Fill and audit; returns fields whose real options did not contain the planned value."""
        unmatched: list[PageField] = []
        secret_ids = {fid for fid, (f, _) in plans.items() if f.type == "password"}
        res = await self.cmd("fill", {"items": items}, timeout=max(30, 4 * len(items)))
        results = (res or {}).get("results") or []
        retry: list[dict[str, Any]] = []
        for r in results:
            fid = r.get("field_id")
            if fid not in plans:
                continue
            f, resolution = plans[fid]
            status = "filled" if r.get("ok") else "failed"
            if not r.get("ok"):
                err = (r.get("error") or {}).get("code")
                if err == "NO_OPTION_MATCH" and r.get("options"):
                    # Options loaded asynchronously: map the same truthful value onto the real options.
                    f2 = f.model_copy(update={"options": [Option.model_validate(o) for o in r["options"] if isinstance(o, dict)]})
                    alt = to_field_value(f2, [resolution.value])
                    if alt.ok and alt.value != resolution.value:
                        retry.append({"field_id": fid, "value": alt.value})
                        continue
                    if resolution.method != "generated":
                        unmatched.append(f2.model_copy(update={"options_dynamic": False}))
                        continue
                elif err in ("VERIFY_FAILED", "TIMEOUT"):
                    retry.append({"field_id": fid, "value": resolution.value})
                    continue
            record = {**resolution.audit(f, self.app["page_index"]), "status": status,
                      "source": {**resolution.sources, "fill_error": r.get("error"), "planned_value": resolution.value}}
            if r.get("ok") and f.type != "password" and r.get("actual") not in (None, ""):
                record["value"] = r["actual"]  # audit the exact value the page accepted
            self.db.upsert_answer(self.app["id"], record)
        if retry:
            self.metric("retries", len(retry))
            res2 = await self.cmd("fill", {"items": retry}, timeout=max(30, 5 * len(retry)))
            for r in (res2 or {}).get("results") or []:
                fid = r.get("field_id")
                if fid in plans:
                    f, resolution = plans[fid]
                    value = next((x["value"] for x in retry if x["field_id"] == fid), resolution.value)
                    if r.get("ok") and f.type != "password" and r.get("actual") not in (None, ""):
                        value = r["actual"]
                    self.db.upsert_answer(self.app["id"], {**resolution.audit(f, self.app["page_index"]), "value": value,
                                                           "status": "filled" if r.get("ok") else "failed",
                                                           "source": {**resolution.sources, "retried": True,
                                                                      "fill_error": r.get("error")}})
        failed = [r for r in results if not r.get("ok") and r.get("field_id") not in {x["field_id"] for x in retry}]
        self.event("filling", "fields_filled", f"Filled {len(items) - len(failed) - len(unmatched)}/{len(items)} field(s)",
                   {"failed": [{"field_id": r.get("field_id"), "error": r.get("error")} for r in failed][:10],
                    "option_mismatch": [f.label for f in unmatched], "secret_fields": len(secret_ids)})
        return unmatched

    async def _upload(self, f: PageField, res: Resolution) -> None:
        kind = res.value
        audit = res.audit(f, self.app["page_index"])
        if kind == "resume":
            name = self.app.get("resume")
            path = self.cfg.paths.resumes_dir / name if name else None
            if path is None or not path.exists():
                raise NeedsUser("missing_resume", f"selected resume {name} not found", "Put the resume in resumes/ and Resume.")
            if f.files and name and any(name.lower() == x.lower() for x in f.files):
                self.db.upsert_answer(self.app["id"], {**audit, "value": name, "status": "verified", "method": "file",
                                                       "source": {"resume": self.data.get("resume")}})
                return
            await self._send_file(f, path.name, path.read_bytes())
            self.db.upsert_answer(self.app["id"], {**audit, "value": name, "status": "filled", "method": "file",
                                                   "source": {"resume": self.data.get("resume")}})
            return
        if kind == "cover_letter":
            if not f.required and not self.cfg.settings.answers.cover_letter_when_optional:
                self.db.upsert_answer(self.app["id"], {**audit, "status": "skipped", "value": None})
                return
            text = self._cover_letter_text()
            if text is None:
                payload = {"question": "Write a cover letter for this application", "field_type": "textarea", "options": [],
                           "required": True, "limits": {"max_words": 400}, "qclass": "WRITTEN", "template": "cover_letter",
                           "company_specific": True, "research_recommended": True, "company": self.job["company"],
                           "title": self.job["title"], "job_id": self.job["id"], "hints": {},
                           "dedupe_key": f"cover:{self.job['id']}:{self.cfg.profile_hash[:10]}"}
                self.e.reasoning.enqueue(app_id=self.app["id"], kind="question", payload=payload, field_key="__cover_letter__")
                self.e.research.schedule(self.job["company"], self.job)
                self.db.upsert_answer(self.app["id"], {**audit, "status": "pending", "method": "generated", "qclass": "WRITTEN"})
                return
            pdf = text_to_pdf(text)
            await self._send_file(f, "Cover_Letter.pdf", pdf, mime="application/pdf")
            self.db.upsert_answer(self.app["id"], {**audit, "value": text, "status": "filled", "method": "generated",
                                                   "qclass": "WRITTEN", "source": {"template": "cover_letter", "as_file": True}})
            return
        # Other documents (transcript...) only if the user supplied one.
        doc = self._find_document(str(kind))
        if doc:
            await self._send_file(f, doc.name, doc.read_bytes())
            self.db.upsert_answer(self.app["id"], {**audit, "value": doc.name, "status": "filled", "method": "file"})
        elif f.required:
            raise NeedsUser("missing_document", f"required upload '{f.label}' has no configured document",
                            f"Put the document in resumes/ (filename containing '{kind}') and press Resume.",
                            fields=[{"field_key": f.key, "question": f.question, "type": "file"}])
        else:
            self.db.upsert_answer(self.app["id"], {**audit, "status": "skipped", "value": None})

    def _cover_letter_text(self) -> str | None:
        result = self.e.reasoning.results_for(self.app["id"]).get("__cover_letter__")
        return str(result["value"]) if result and result.get("value") else None

    def _find_document(self, kind: str) -> Path | None:
        for p in sorted(self.cfg.paths.resumes_dir.glob("*")):
            if p.is_file() and kind.lower() in p.name.lower():
                return p
        return None

    async def _send_file(self, f: PageField, name: str, content: bytes, mime: str | None = None) -> None:
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        res = await self.cmd("upload", {"field_id": f.id, "file": {"name": name, "mime": mime,
                                                                   "base64": base64.b64encode(content).decode()}},
                             timeout=self.cfg.settings.browser.upload_timeout)
        if not (res or {}).get("ok"):
            raise BrowserError("VERIFY_FAILED", f"upload of {name} did not attach")
        self.event("filling", "file_uploaded", f"Uploaded {name} to '{truncate(f.label, 60)}'",
                   {"file": name, "bytes": len(content), "attached": (res or {}).get("files")})

    # ------------------------------------------------------------------ validation / navigation
    def _bump_page_attempt(self, fingerprint: str) -> int:
        attempts = dict(self.data.get("page_attempts") or {})
        attempts[fingerprint] = attempts.get(fingerprint, 0) + 1
        self.save(data={"page_attempts": attempts})
        return attempts[fingerprint]

    async def h_validating(self) -> str | Stop:
        snap = await self.scan()
        if snap.page_kind in REDIRECT_KINDS or (snap.captcha.present and snap.captcha.interactive):
            return S.APPLYING
        problems = [f for f in snap.fields if f.error and not f.disabled]
        empty_required = [f for f in snap.fields if f.required and not f.disabled and f.is_empty()]
        if problems or snap.errors or empty_required:
            n = self._bump_page_attempt(snap.fingerprint)
            detail = {"field_errors": [{"label": f.label, "error": f.error, "value": f.value if f.type != "password" else None}
                                       for f in problems][:10],
                      "page_errors": [e.text for e in snap.errors][:5],
                      "empty_required": [f.label for f in empty_required][:10], "attempt": n}
            self.event("validation", "validation_failed", f"Page validation failed (attempt {n})", detail, level="WARNING")
            if n > self.cfg.settings.automation.page_retry_limit:
                await self.screenshot("validation")
                raise NeedsUser("validation", "the page keeps reporting validation errors",
                                "Fix the highlighted fields in the application window, then press Resume.",
                                fields=[{"field_key": f.key, "question": f.question, "error": f.error} for f in problems + empty_required][:10],
                                resume_state=S.VALIDATING)
            alternates = dict(self.data.get("alternates") or {})
            for f in problems:
                alternates[f.key] = alternates.get(f.key, 0) + 1
            self.save(data={"alternates": alternates})
            return S.FILLING
        submits, nexts = snap.final_buttons(), snap.buttons_of("next")
        if nexts:
            return await self._advance(snap, nexts[0])
        if submits:
            return await self._prepare_submit(snap)  # "Apply now" on a filled form is final: gated like Submit
        if snap.buttons_of("apply") and not snap.has_filled_fields():
            return await self._click_apply(snap)
        return await self._page_help(snap, "Form looks complete but there is no Next/Submit button.")

    async def _advance(self, snap: PageSnapshot, button: Button) -> str | Stop:
        if self.app["page_index"] >= self.cfg.settings.automation.max_pages_per_application:
            raise NeedsUser("unexpected", "too many pages", "Check the application window, then Resume or Skip.")
        new, changed = await self.click_and_wait(button)
        if changed and not (new.errors or any(f.error for f in new.fields)):
            self.save(page_index=self.app["page_index"] + 1, data={"alternates": {}})
            self.event("navigation", "page_advanced", f"Advanced past '{truncate(snap.heading or snap.title, 60)}'",
                       {"button": button.text, "page_index": self.app["page_index"]})
            return S.APPLYING
        self._bump_page_attempt(snap.fingerprint)
        if self.data.get("page_attempts", {}).get(snap.fingerprint, 0) > self.cfg.settings.automation.page_retry_limit + 1:
            raise NeedsUser("validation", "clicking Next does not advance the page",
                            "Check the page for problems, fix them and press Resume.", resume_state=S.VALIDATING)
        return S.FILLING if (new.errors or any(f.error for f in new.fields)) else S.VALIDATING

    async def _prepare_submit(self, snap: PageSnapshot) -> str | Stop:
        gate = submission_gate(snap=snap, app=self.app, job=self.job, cfg=self.cfg, db=self.db, resolver=self.e.resolver,
                               resume_name=self.app.get("resume"))
        self.save(data={"gate": gate.to_dict()})
        self.event("validation", "submission_gate", "Submission gate passed" if gate.ok else "Submission gate FAILED",
                   gate.to_dict(), level="INFO" if gate.ok else "WARNING")
        if not gate.ok:
            if not gate.checks.get("not_duplicate", True) or not gate.checks.get("no_prior_submission", True):
                return self.go(S.SKIPPED, "duplicate: " + "; ".join(gate.failures))
            if not gate.checks.get("required_filled", True) or not gate.checks.get("no_errors", True):
                n = self._bump_page_attempt(snap.fingerprint)
                if n <= self.cfg.settings.automation.page_retry_limit:
                    return S.FILLING
            await self.screenshot("gate")
            raise NeedsUser("review", "submission gate failed: " + "; ".join(gate.failures)[:400],
                            "Review the application window and the failed checks; fix and press Resume, or Skip.",
                            resume_state=S.VALIDATING)
        approved = bool(self.data.get("submit_approved")) and self.data.get("submit_approved_fp") == snap.fingerprint
        if not self.cfg.settings.automation.auto_submit and not approved:
            self.go(S.READY_TO_SUBMIT, "auto_submit is off: waiting for approval",
                    data={"needs_user": {"kind": "review", "reason": "Ready to submit (auto-submit disabled)",
                                         "instructions": "Review the filled application in Firefox, then click Submit in "
                                                         "the dashboard (or ask Astra to submit_application).",
                                         "since": time.time()}})
            self.e.notify_attention(self.app, self.data.get("needs_user") or {})
            return Stop(reason="ready to submit (review mode)")
        if self.e.should_yield():
            return Stop(reason="paused before submission")
        try:
            self.app = self.db.reserve_submission(self.app["id"], worker_id=self.worker, token=self.token)
        except DuplicateSubmission as exc:
            self.event("dedupe", "duplicate_blocked", str(exc), {"conflicts": exc.conflicts}, level="WARNING")
            return self.go(S.SKIPPED, f"duplicate blocked at submission: {summarize_duplicate(exc.conflicts)}")
        self.save(data={"submit_approved": False})
        return S.SUBMITTING

    # ------------------------------------------------------------------ submission
    async def h_submitting(self) -> str | Stop:
        sub = self.db.submission(self.app["id"])
        if sub is None or sub["status"] == "ABORTED":
            return S.VALIDATING
        if sub["status"] == "SUBMITTED":
            return S.SUBMITTED
        clicked = self.data.get("submit_clicked_at")
        if clicked:
            # Never click twice blindly: inspect what happened.
            snap = await self.scan()
            return await self._submission_outcome(snap, changed=True, resumed=True)
        snap = await self.scan()
        submits = snap.final_buttons()
        if not submits:
            if snap.page_kind == "confirmation" or snap.confirmation.detected:
                return await self._submission_outcome(snap, changed=True, resumed=True)
            self.db.finish_submission(self.app["id"], "ABORTED", {"reason": "submit button vanished before click"},
                                      worker_id=self.worker, token=self.token)
            return S.APPLYING
        self.save(data={"submit_clicked_at": time.time(), "submit_clicks": int(self.data.get("submit_clicks") or 0) + 1,
                        "submit_intent": {"url": redact_url(snap.url), "fingerprint": snap.fingerprint,
                                          "button_id": submits[0].id, "button_text": submits[0].text}})
        self.event("submission", "submit_clicked", f"Clicked '{submits[0].text}'", {"button": submits[0].text})
        before_step = snap.step.current if snap.step and snap.step.current else None
        new, changed = await self.click_and_wait(submits[0], settle=25)
        return await self._submission_outcome(new, changed=changed, resumed=False, before_step=before_step)

    async def _submission_outcome(self, snap: PageSnapshot, *, changed: bool, resumed: bool,
                                  before_step: int | None = None) -> str | Stop:
        evidence = {"url": redact_url(snap.url), "title": snap.title, "heading": snap.heading, "page_kind": snap.page_kind,
                    "confirmation": snap.confirmation.text, "ts": time.time()}
        if snap.page_kind == "confirmation" or snap.confirmation.detected:
            evidence["screenshot"] = await self.screenshot("confirmation")
            self.db.finish_submission(self.app["id"], "SUBMITTED", evidence, worker_id=self.worker, token=self.token)
            self._remember_answers(submitted=True)
            self.e.metrics["submitted"] += 1
            self.e.record_domain(self.app, ok=True)
            return self.go(S.SUBMITTED, "confirmation detected", data={"submission_evidence": evidence})
        if snap.captcha.present and snap.captcha.interactive:
            intent = self.data.get("submit_intent") or {}
            safe_retry = bool(
                snap.final_buttons()
                and redact_url(snap.url) == intent.get("url")
                and snap.fingerprint == intent.get("fingerprint")
                and not snap.confirmation.detected
            )
            resume_state = S.SUBMITTING
            if safe_retry:
                # The same final form and button are still present: the site proves the
                # click was intercepted before delivery. Abort this reserved attempt so
                # validation and the submission gate run again after the human challenge.
                self.db.finish_submission(
                    self.app["id"], "ABORTED",
                    {**evidence, "reason": "submit click blocked by CAPTCHA before delivery"},
                    worker_id=self.worker, token=self.token)
                self.save(data={"submit_clicked_at": None, "submit_blocked_by": "captcha"})
                resume_state = S.VALIDATING
            return await self.wait_for_captcha(
                snap, reason="CAPTCHA appeared at final submission", resume_state=resume_state,
                phase="submission", safe_to_retry_submit=safe_retry)
        still_form = bool(snap.final_buttons()) or bool(snap.buttons_of("submit"))
        if still_form and (snap.errors or any(f.error for f in snap.fields)):
            # The site rejected the submission → nothing was sent; safe to fix and retry (bounded).
            self.db.finish_submission(self.app["id"], "ABORTED", {**evidence, "errors": [e.text for e in snap.errors][:5]},
                                      worker_id=self.worker, token=self.token)
            self.save(data={"submit_clicked_at": None})
            self.event("submission", "submission_rejected", "Site reported validation errors after Submit",
                       {"errors": [e.text for e in snap.errors][:5]}, level="WARNING")
            if int(self.data.get("submit_clicks") or 0) >= 3:
                raise NeedsUser("validation", "submission rejected repeatedly", "Fix the form and press Resume.",
                                resume_state=S.VALIDATING)
            return S.FILLING
        step_advanced = bool(before_step and snap.step and snap.step.current and snap.step.current > before_step
                             and snap.step.total and snap.step.current <= snap.step.total)
        if changed and snap.fields and step_advanced and not resumed:
            # The step indicator proves the button only advanced to another step (e.g. a review page).
            self.db.finish_submission(self.app["id"], "ABORTED", {**evidence, "reason": "button led to another step"},
                                      worker_id=self.worker, token=self.token)
            self.save(data={"submit_clicked_at": None}, page_index=self.app["page_index"] + 1)
            return S.APPLYING
        # Give slow confirmations a final chance.
        for _ in range(3):
            await asyncio.sleep(2)
            again = await self.scan()
            if again.page_kind == "confirmation" or again.confirmation.detected:
                return await self._submission_outcome(again, changed=True, resumed=resumed)
        evidence["screenshot"] = await self.screenshot("unconfirmed")
        self.db.finish_submission(self.app["id"], "SUBMISSION_UNCONFIRMED", evidence, worker_id=self.worker, token=self.token)
        self._remember_answers(submitted=False)
        return self.go(S.SUBMISSION_UNCONFIRMED, "submit was clicked but no confirmation was detected",
                       data={"submission_evidence": evidence,
                             "needs_user": {"kind": "verify_submission", "reason": "Submission not confirmed",
                                            "instructions": "Check the window/email; mark Submitted in the dashboard if it went through.",
                                            "since": time.time()}})

    def _remember_answers(self, *, submitted: bool) -> None:
        for ans in self.db.answers(self.app["id"]):
            if ans["status"] in ("filled", "verified") and ans["method"] not in ("file", None):
                try:
                    self.e.reasoning.remember(app=self.app, job=self.job, answer=ans, submitted=submitted)
                except Exception:  # memory is an optimization; never block the outcome on it
                    log.exception("failed to store answer memory")
        if submitted:
            self.db.mark_memory_submitted(self.app["id"])

    # ------------------------------------------------------------------ accounts & verification
    async def h_account_required(self) -> str | Stop:
        snap = await self.scan()
        if snap.page_kind not in ("login", "signup"):
            return S.APPLYING
        attempts = int(self.data.get("account_attempts") or 0)
        if attempts >= 4:
            raise NeedsUser("login", "could not sign in or create an account automatically",
                            "Sign in / create the account in the application window, then press Resume.",
                            resume_state=S.APPLYING)
        self.save(data={"account_attempts": attempts + 1})
        url = snap.url
        creds = credentials_for(self.cfg, url)
        if creds is None:
            raise NeedsUser("login", "this site needs an account and no credentials are configured",
                            "Set APPLYPILOT_ACCOUNT_PASSWORD (and optionally APPLYPILOT_ACCOUNT_EMAIL) in .env and restart, "
                            "or sign in manually in the application window and press Resume.", resume_state=S.APPLYING)
        key = account_key(url)
        account = self.db.get_account(key)
        email_fields = [f for f in snap.fields if f.type == "email" or (f.type == "text" and "mail" in f.label.lower())]
        pw_fields = [f for f in snap.fields if f.type == "password"]
        if snap.page_kind == "signup":
            if account and account["status"] in ("active", "verification_pending") and snap.buttons_of("login"):
                await self.click_and_wait(snap.buttons_of("login")[0])
                return S.ACCOUNT_REQUIRED
            if not self.cfg.preferences.accounts.create_accounts:
                raise NeedsUser("login", "account creation is disabled (preferences.accounts.create_accounts)",
                                "Create the account manually, then press Resume.", resume_state=S.APPLYING)
            items: list[dict[str, Any]] = [{"field_id": f.id, "value": creds.username} for f in email_fields]
            items += [{"field_id": f.id, "value": creds.password} for f in pw_fields]
            accept = bool(self.cfg.preferences.application.model_dump().get("accept_terms")
                          or self.cfg.preferences.application.accept_privacy_policy)
            items += [{"field_id": f.id, "value": True} for f in snap.fields
                      if accept and f.type == "checkbox" and f.required and not f.value]
            await self._fill_secret(items)
            btn = (snap.buttons_of("signup") or snap.buttons_of("submit") or snap.buttons_of("next"))
            if not btn:
                return await self._page_help(snap, "Sign-up form has no recognizable create-account button.")
            self.db.upsert_account(key, domain=key, username=creds.username, secret_ref=creds.secret_ref, status="creating",
                                   application_id=self.app["id"])
            new, _ = await self.click_and_wait(btn[0])
            status = "verification_pending" if new.page_kind == "verification" else "active"
            if new.errors and new.page_kind == "signup":
                text = " ".join(e.text for e in new.errors).lower()
                if "exist" in text or "already" in text:
                    self.db.upsert_account(key, domain=key, username=creds.username, secret_ref=creds.secret_ref, status="active")
                    if new.buttons_of("login"):
                        await self.click_and_wait(new.buttons_of("login")[0])
                    return S.ACCOUNT_REQUIRED
                status = "failed"
            self.db.upsert_account(key, domain=key, username=creds.username, secret_ref=creds.secret_ref, status=status,
                                   application_id=self.app["id"])
            self.save(data={"account_created_at": time.time()})
            self.event("account", "account_created" if status != "failed" else "account_creation_failed",
                       f"Account {'created' if status != 'failed' else 'creation failed'} for {key}",
                       {"account_key": key, "username": creds.username, "status": status,
                        "errors": [e.text for e in new.errors][:3]}, level="INFO" if status != "failed" else "WARNING")
            return S.APPLYING
        # login page
        if not account and snap.buttons_of("signup") and self.cfg.preferences.accounts.create_accounts and attempts >= 1:
            await self.click_and_wait(snap.buttons_of("signup")[0])
            return S.ACCOUNT_REQUIRED
        items = [{"field_id": f.id, "value": creds.username} for f in email_fields]
        items += [{"field_id": f.id, "value": creds.password} for f in pw_fields[:1]]
        await self._fill_secret(items)
        btn = snap.buttons_of("login") or snap.buttons_of("submit") or snap.buttons_of("next")
        if not btn:
            return await self._page_help(snap, "Sign-in form has no recognizable sign-in button.")
        new, _ = await self.click_and_wait(btn[0])
        if new.page_kind == "login" and (new.errors or not _) and not account and new.buttons_of("signup") \
                and self.cfg.preferences.accounts.create_accounts:
            self.event("account", "login_failed_no_account", "Sign-in failed; creating an account", {"account_key": key})
            await self.click_and_wait(new.buttons_of("signup")[0])
            return S.ACCOUNT_REQUIRED
        if new.page_kind == "login" and new.errors:
            self.event("account", "login_failed", "Sign-in failed", {"account_key": key, "errors": [e.text for e in new.errors][:3]},
                       level="WARNING")
            return S.ACCOUNT_REQUIRED
        if account:
            self.db.upsert_account(key, domain=key, username=creds.username, secret_ref=creds.secret_ref, status="active")
        self.event("account", "signed_in", f"Signed in to {key}", {"account_key": key})
        return S.APPLYING

    async def _fill_secret(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        # Values never enter the audit log (cmd() redacts fill values; event omits them entirely).
        res = await self.e.bridge.call(self.app["id"], "fill", {"items": items}, timeout=30)
        failed = [r.get("field_id") for r in (res or {}).get("results") or [] if not r.get("ok")]
        self.event("account", "credentials_filled", f"Filled {len(items) - len(failed)}/{len(items)} credential field(s)",
                   {"failed": failed})

    async def h_verifying_email(self) -> str | Stop:
        snap = await self.scan()
        if snap.page_kind != "verification":
            return S.APPLYING
        provider = self.e.email
        if not provider.configured():
            raise NeedsUser("verification", "email verification required and no email provider is configured",
                            "Open your email, follow the verification link or copy the code into the application window, "
                            "then press Resume. (Configure APPLYPILOT_IMAP_* in .env to automate this.)",
                            resume_state=S.APPLYING)
        # Only emails that arrived after the account step (or after verification became necessary), and only
        # from the ATS / employer, are accepted — never another service's one-time code.
        started = self.data.get("verification_started_at")
        if not started:
            started = time.time()
            self.save(data={"verification_started_at": started})
        since = float(self.data.get("account_created_at") or started) - 120
        domains = sender_domains_for(snap.url, self.job.get("apply_url"), self.job.get("company"))
        self.event("account", "verification_waiting", "Waiting for verification email",
                   {"provider": provider.name, "accepted_senders": domains})
        found = await provider.find_verification(since=since, sender_domains=domains, recipient=None, timeout=150)
        if not found:
            raise NeedsUser("verification", "verification email did not arrive in time",
                            "Complete the email verification manually, then press Resume.", resume_state=S.APPLYING)
        self.event("account", "verification_found", f"Verification {found.kind} received",
                   {"subject": scrub_codes(found.subject), "kind": found.kind, "sender": found.sender})
        if found.kind == "link":
            await self.cmd("navigate", {"url": found.value}, timeout=self.cfg.settings.browser.page_load_timeout + 5, audit=False)
            await self.wait_ready()
            self.secret_urls.append(found.value)
        else:
            code_fields = [f for f in snap.fields if f.type in ("text", "number", "tel") and not f.value]
            if not code_fields:
                raise NeedsUser("verification", "verification code received but no code field found",
                                f"Enter the code from the email titled '{scrub_codes(found.subject)}', then Resume.")
            await self._fill_secret([{"field_id": code_fields[0].id, "value": found.value}])
            btn = snap.buttons_of("verify") or snap.buttons_of("submit") or snap.buttons_of("next")
            if btn:
                await self.click_and_wait(btn[0])
        key = account_key(snap.url)
        if self.db.get_account(key):
            acct = self.db.get_account(key)
            assert acct is not None
            self.db.upsert_account(key, domain=acct["domain"], username=acct["username"], secret_ref=acct["secret_ref"], status="active")
        return S.APPLYING


class _Skip(Exception):
    pass


def _origin(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""


def scrub_codes(text: str | None) -> str:
    """Remove one-time codes (4-8 digit runs) from email subjects before logging."""
    return re.sub(r"\b\d{4,8}\b", "[code]", text or "")


def sender_domains_for(*urls_and_company: str | None) -> list[str]:
    """Acceptable verification senders: the ATS host's registrable domain, the ATS vendor, the company."""
    from urllib.parse import urlsplit

    out: list[str] = []
    for value in urls_and_company[:-1]:
        host = (urlsplit(value or "").hostname or "").lower()
        if host and host not in ("127.0.0.1", "localhost"):
            parts = host.split(".")
            out.append(".".join(parts[-2:]))
            ats = detect_ats(value)
            if ats not in ("generic", "custom"):
                out.append({"workday": "myworkday", "greenhouse": "greenhouse", "lever": "lever", "ashby": "ashbyhq",
                            "icims": "icims", "smartrecruiters": "smartrecruiters"}.get(ats, ats))
    company = re.sub(r"[^a-z0-9]", "", (urls_and_company[-1] or "").lower())
    if len(company) >= 3:
        out.append(company)
    return sorted(set(out))


_FINAL_WORDS = re.compile(r"\b(submit|send|finish|complete|apply|done|confirm)\b", re.I)


def _final_click(action: dict[str, Any], snap: PageSnapshot) -> bool:
    kind = action.get("type")
    if kind == "click_point":
        return True  # unknown target on a filled form: never risk it
    if kind == "click_text":
        return bool(_FINAL_WORDS.search(str(action.get("text") or "")))
    if kind == "click_button":
        btn = next((b for b in snap.buttons if b.id == str(action.get("button_id"))), None)
        return btn is None or btn.kind in ("submit", "apply") or bool(_FINAL_WORDS.search(btn.text))
    return False


def _already(f: PageField, value: Any) -> bool:
    if f.type in ("password", "file"):
        return False
    if f.type == "checkbox":
        return bool(f.value) == bool(value)
    if f.value in (None, "", []):
        return False
    if isinstance(value, list):
        return sorted(map(str, f.value if isinstance(f.value, list) else [f.value])) == sorted(map(str, value))
    return str(f.value).strip().casefold() == str(value).strip().casefold()


def _audit_payload(action: str, payload: dict[str, Any] | None) -> Any:
    if not payload:
        return None
    if action == "fill":
        return {"items": [{"field_id": i.get("field_id"), "value": "[filled]"} for i in payload.get("items", [])]}
    if action == "upload":
        f = payload.get("file") or {}
        return {"field_id": payload.get("field_id"), "file": f.get("name")}
    if action in ("open", "navigate"):
        return {"url": redact_url(payload.get("url"))}
    return payload


_ACTIVITY = {
    "QUEUED": "Queued", "CHECKING_ELIGIBILITY": "Checking eligibility", "READY": "Ready for browser",
    "OPENING": "Opening application…", "APPLYING": "Reading page…", "ACCOUNT_REQUIRED": "Signing in / creating account…",
    "VERIFYING_EMAIL": "Waiting for verification email…", "FILLING": "Filling application…",
    "ANSWERING": "Waiting for Astra answers…", "VALIDATING": "Validating page…", "SUBMITTING": "Submitting…",
}
_LIMIT_HINT = limits_for  # re-exported for tests
