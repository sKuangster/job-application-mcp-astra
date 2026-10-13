"""Persistent worker pool, pipeline scheduling and autopilot control.

Two pools share one persistent queue (the ``applications`` table):

* **prep** workers (HTTP only, no browser): Simplify enrichment, eligibility, resume choice;
* **browser** workers: own one Firefox window/tab each while actively driving it.

Applications waiting on Astra or the user hold their tab but NOT a worker, so browser
workers always do useful work (pipeline parallelism). Claims are atomic leases with
heartbeats; a crashed worker's lease simply expires and another worker resumes the
application from its persisted state.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import sqlite3
import time
import traceback
import uuid
from typing import Any

from applypilot.accounts import EmailProvider, email_provider
from applypilot.bridge import TAB_GONE, BridgeUnavailable, BrowserBridge, BrowserError
from applypilot.config import AppConfig
from applypilot.db import Database, new_id
from applypilot.engine.runner import ApplicationRunner
from applypilot.identity import (
    ats_job_key,
    detect_ats,
    domain_key,
    job_aliases,
    normalize_url,
    simplify_id_from_url,
)
from applypilot.ingest.listings import fetch_jobs, simplify_url_for
from applypilot.logsetup import ctx_application, ctx_job, ctx_worker
from applypilot.models import PageSnapshot
from applypilot.questions.resolver import Resolver
from applypilot.reasoning import ReasoningService
from applypilot.research import ResearchService
from applypilot.security import redact_url
from applypilot.state import TERMINAL, InvalidTransition, LeaseLost, S
from applypilot.textutil import company_norm, stable_hash, title_norm

log = logging.getLogger("applypilot.engine")
RUNNING, PAUSED, STOPPED = "RUNNING", "PAUSED", "STOPPED"
MODES = ("backlog", "new_jobs", "continuous", "test_one")


class Engine:
    def __init__(self, cfg: AppConfig, db: Database, bridge: BrowserBridge, *, email: EmailProvider | None = None):
        self.cfg = cfg
        self.db = db
        self.bridge = bridge
        self.boot_id = uuid.uuid4().hex[:6]
        self.research = ResearchService(db, cfg)
        self.reasoning = ReasoningService(db, cfg, research=self.research)
        self.resolver = Resolver(cfg, db)
        self.email = email or email_provider()
        self.activity: dict[str, dict[str, Any]] = {}
        self.metrics: dict[str, Any] = {"submitted": 0, "failures": 0, "claims": 0, "started_at": time.time()}
        self.recent_errors: list[dict[str, Any]] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self.closing = False
        self._wake = asyncio.Event()
        self._captcha_checks: set[str] = set()
        self.attention_listeners: list[Any] = []
        bridge.handlers.append(self._on_bridge_event)

    # ------------------------------------------------------------------ control
    def control(self) -> str:
        return str(self.db.meta_get("control", STOPPED))

    def mode(self) -> str:
        return str(self.db.meta_get("mode", "backlog"))

    def test_application_id(self) -> str | None:
        scope = self.db.execution_scope()
        if scope and scope.get("kind") == "test_one":
            return str(scope.get("application_id") or "") or None
        return None

    def application_browser_options(self, app_id: str) -> dict[str, Any]:
        scope = self.db.execution_scope()
        if scope and scope.get("kind") == "test_one" and scope.get("application_id") == app_id:
            return {
                "mode": scope.get("browser_mode", "headed"),
                "watch": bool(scope.get("watch")),
                "slow_mo_ms": int(scope.get("slow_mo_ms") or 0),
            }
        return {"mode": "headed", "watch": False,
                "slow_mo_ms": self.cfg.settings.browser.debug_action_delay_ms}

    def should_yield(self) -> bool:
        return self.closing or self.control() != RUNNING

    def wake(self) -> None:
        self._wake.set()

    async def set_control(self, action: str, mode: str | None = None) -> str:
        action = action.lower()
        if mode and mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if action in ("start", "resume"):
            scope = self.db.execution_scope()
            if scope and scope.get("kind") == "test_one" and mode != "test_one":
                if action == "resume":
                    mode = "test_one"
                else:
                    # A normal explicit start is the deliberate escape hatch from a
                    # completed/stopped test scope. Never clear it implicitly at boot.
                    if self.control() != STOPPED:
                        raise ValueError("test-one is active; stop it before starting a normal automation mode")
                    self.db.meta_delete("execution_scope")
            if self.cfg.settings.automation.stop_on_config_incomplete:
                ready = self.cfg.readiness()
                if not ready["ready"]:
                    raise ValueError("configuration incomplete: " + ", ".join(ready["missing"]))
            if mode:
                self.db.meta_set("mode", mode)
            self.db.meta_set("control", RUNNING)
            if action == "start":
                self.db.meta_set("backlog_complete", False)
        elif action == "pause":
            self.db.meta_set("control", PAUSED)
        elif action == "stop":
            self.db.meta_set("control", STOPPED)
        else:
            raise ValueError(f"unknown control action {action!r}")
        self.db.event(None, "control", f"autopilot_{action}", f"Autopilot {action}" + (f" ({mode})" if mode else ""),
                      {"mode": mode or self.mode()})
        self.wake()
        return self.control()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        owner = f"backend-{self.boot_id}"
        if not self.db.acquire_lock("backend", owner, ttl=60):
            raise RuntimeError(f"another ApplyPilot backend is running ({self.db.lock_owner('backend')}); stop it first")
        # RUNNING is never trusted across process death. This prevents a restart
        # after a crash from silently draining a previously queued backlog.
        if self.control() == RUNNING:
            self.db.meta_set("control", STOPPED)
            self.db.event(None, "control", "startup_stopped", "Autopilot held stopped after backend restart", {})
        released = self.db.release_all_leases(except_prefix=None)
        if released:
            log.info("released %d stale lease(s) from a previous run", released)
        auto = self.cfg.settings.automation
        for i in range(auto.max_prep_workers):
            self.tasks.append(asyncio.create_task(self._worker(f"prep-{i + 1}", "prep"), name=f"prep-{i + 1}"))
        for i in range(auto.max_concurrent_applications):
            self.tasks.append(asyncio.create_task(self._worker(f"browser-{i + 1}", "browser"), name=f"browser-{i + 1}"))
        self.tasks.append(asyncio.create_task(self._housekeeping(owner), name="housekeeping"))
        self.tasks.append(asyncio.create_task(self._discovery(), name="discovery"))
        if auto.captcha_auto_resume:
            self.tasks.append(asyncio.create_task(self._captcha_watch(), name="captcha-watch"))
        if self.cfg.settings.reasoning.provider == "hybrid":
            from applypilot.hybrid import HybridRouter

            for i in range(auto.max_concurrent_model_requests):
                router = HybridRouter(self.cfg, self.reasoning, self.db)
                self.tasks.append(asyncio.create_task(router.run(lambda: self.closing), name=f"hybrid-{i}"))
        if self.cfg.settings.reasoning.provider == "openai":
            from applypilot.providers import OpenAIProvider

            provider = OpenAIProvider(self.cfg, self.reasoning, self.db)
            if provider.available():
                self.tasks.append(asyncio.create_task(provider.run(lambda: self.closing), name="openai"))
            else:
                log.warning("reasoning.provider=openai but %s is not set; Astra (MCP) must answer instead",
                            self.cfg.settings.reasoning.openai.api_key_env)
        for name, value in (("browser workers", auto.max_concurrent_applications), ("prep workers", auto.max_prep_workers)):
            log.info("%s: %s", name, value)

    async def shutdown(self, timeout: float = 20) -> None:
        """Stop at safe points: workers finish their current atomic step, then exit."""
        self.closing = True
        await self.research.shutdown()
        self.wake()
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        self.db.release_lock("backend", f"backend-{self.boot_id}")

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)

    # ------------------------------------------------------------------ workers
    def set_activity(self, worker_id: str, app: dict[str, Any] | None, text: str) -> None:
        worker_id = worker_id.split("@", 1)[0]  # lease ids are "<worker>@<boot>"
        if app is None:
            self.activity[worker_id] = {"worker_id": worker_id, "status": "idle", "since": time.time()}
            return
        prev = self.activity.get(worker_id) or {}
        job = prev.get("_job") if prev.get("application_id") == app["id"] else None
        if job is None:
            job = self.db.get_job(app["job_id"]) or {}
        self.activity[worker_id] = {
            "worker_id": worker_id, "pool": "prep" if worker_id.startswith("prep") else "browser", "status": "busy",
            "application_id": app["id"], "company": job.get("company"), "title": job.get("title"), "state": app["state"],
            "activity": text, "since": prev.get("since") if prev.get("application_id") == app["id"] else time.time(),
            "_job": {"company": job.get("company"), "title": job.get("title")},
        }

    async def _worker(self, worker_id: str, pool: str) -> None:
        auto = self.cfg.settings.automation
        self.activity[worker_id] = {"worker_id": worker_id, "pool": pool, "status": "idle", "since": time.time()}
        while not self.closing:
            try:
                if self.control() != RUNNING:
                    await self._sleep(1.0)
                    continue
                if pool == "browser" and not self.bridge.connected:
                    await self._sleep(2.0)
                    continue
                app = self.db.claim(f"{worker_id}@{self.boot_id}", pool, lease_seconds=auto.worker_lease_timeout,
                                    per_domain=auto.per_domain_concurrency, max_tabs=auto.max_open_application_tabs,
                                    app_id=self.test_application_id())
                if app is None:
                    await self._sleep(auto.idle_poll_seconds)
                    continue
                self.metrics["claims"] += 1
                await self._process(f"{worker_id}@{self.boot_id}", worker_id, app)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one bad application kill a worker
                log.exception("worker %s loop error", worker_id)
                await self._sleep(2.0)
            finally:
                self.activity[worker_id] = {"worker_id": worker_id, "pool": pool, "status": "idle", "since": time.time()}

    async def _process(self, lease_worker: str, worker_id: str, app: dict[str, Any]) -> None:
        tokens = (ctx_application.set(app["id"]), ctx_job.set(app["job_id"]), ctx_worker.set(worker_id))
        hb = asyncio.create_task(self._heartbeat(app["id"], lease_worker, app["lease_token"]))
        runner: ApplicationRunner | None = None
        try:
            runner = ApplicationRunner(self, app, lease_worker)
            self.set_activity(worker_id, app, "Starting…")
            await runner.run()
        except LeaseLost as exc:
            log.warning("lease lost: %s", exc)
        except BridgeUnavailable as exc:
            self._safe_release(app, lease_worker, delay=5, reason=f"bridge unavailable: {exc}")
        except Exception as exc:
            await self._failure(runner, app, lease_worker, exc)
        finally:
            hb.cancel()
            for var, token in zip((ctx_application, ctx_job, ctx_worker), tokens, strict=True):
                var.reset(token)

    async def _heartbeat(self, app_id: str, worker: str, token: str) -> None:
        auto = self.cfg.settings.automation
        while True:
            await asyncio.sleep(auto.heartbeat_interval)
            if not self.db.heartbeat(app_id, worker, token, auto.worker_lease_timeout):
                log.warning("heartbeat failed for %s (lease lost)", app_id)
                return

    def _safe_release(self, app: dict[str, Any], worker: str, *, delay: float, reason: str) -> None:
        with contextlib.suppress(Exception):
            self.db.release(app["id"], worker, app["lease_token"], delay=delay)
            self.db.event(app["id"], "queue", "released", f"Released: {reason}", {"delay": delay}, worker_id=worker)

    async def _failure(self, runner: ApplicationRunner | None, app: dict[str, Any], worker: str, exc: Exception) -> None:
        current = runner.app if runner else app
        try:
            current = {**self.db.get_application(app["id"]), "lease_token": current.get("lease_token")}
        except KeyError:
            pass
        token = current.get("lease_token") or app["lease_token"]
        self.metrics["failures"] += 1
        code = getattr(exc, "code", type(exc).__name__)
        message = f"{code}: {exc}"[:500]
        self.recent_errors = ([{"ts": time.time(), "application_id": app["id"], "message": message}] + self.recent_errors)[:50]
        attempts = self.db.increment_attempts(app["id"])
        shot = None
        if runner is not None and not isinstance(exc, BridgeUnavailable):
            with contextlib.suppress(Exception):
                shot = await runner.screenshot("error")
        self.db.event(app["id"], "error", "error", message,
                      {"code": code, "attempt": attempts, "state": current.get("state"), "screenshot": shot,
                       "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]},
                      worker_id=worker, level="ERROR")
        self.record_domain(current, ok=False, error=message)
        auto = self.cfg.settings.automation
        state = current.get("state")
        try:
            sub = self.db.submission(app["id"])
            clicked = bool(current["data"].get("submit_clicked_at"))
            if sub and sub["status"] == "SUBMITTING" and clicked:
                # A submit click may have gone through: never FAIL or retry it — let the next claim (or the user)
                # confirm. After repeated errors, park as SUBMISSION_UNCONFIRMED with the record intact.
                if attempts > auto.retry_limit or (isinstance(exc, BrowserError) and exc.code in TAB_GONE):
                    self.db.finish_submission(app["id"], "SUBMISSION_UNCONFIRMED", {"reason": message},
                                              worker_id=worker, token=token)
                    self.db.transition(app["id"], S.SUBMISSION_UNCONFIRMED, worker_id=worker, token=token,
                                       reason=f"error after the submit click; verify manually ({message[:200]})")
                    self.db.release(app["id"], worker, token)
                else:
                    self.db.release(app["id"], worker, token, delay=min(auto.backoff_max_seconds, auto.backoff_base_seconds))
                return
            if sub and sub["status"] == "SUBMITTING" and not clicked:
                self.db.finish_submission(app["id"], "ABORTED", {"reason": f"error before the submit click: {message[:200]}"},
                                          worker_id=worker, token=token)
            if (isinstance(exc, BrowserError) and exc.code in TAB_GONE and state not in TERMINAL
                    and state not in (S.READY, S.OPENING)):
                self.db.transition(app["id"], S.OPENING, worker_id=worker, token=token, reason="tab was closed; reopening",
                                   data={"pages": []}, fields={"tab_id": None, "window_id": None})
                state = S.OPENING
            if attempts > auto.retry_limit:
                self.db.transition(app["id"], S.FAILED, worker_id=worker, token=token,
                                   reason=f"retry limit exceeded: {message}"[:500])
                self.db.release(app["id"], worker, token)
                return
            delay = min(auto.backoff_max_seconds, auto.backoff_base_seconds * (2 ** (attempts - 1)))
            self.db.release(app["id"], worker, token, delay=delay)
            self.db.event(app["id"], "queue", "retry_scheduled", f"Retry {attempts}/{auto.retry_limit} in ~{int(delay)}s",
                          {"delay": delay}, worker_id=worker, level="WARNING")
        except (LeaseLost, InvalidTransition) as inner:
            log.warning("could not persist failure for %s: %s", app["id"], inner)

    def record_domain(self, app: dict[str, Any], *, ok: bool, error: str | None = None) -> None:
        auto = self.cfg.settings.automation
        health = self.db.record_domain_result(app.get("domain"), ok, threshold=auto.domain_failure_threshold,
                                              cooldown=auto.domain_cooldown_seconds, error=error)
        if health and health["cooldown_until"] > time.time() and not ok:
            self.db.event(None, "scheduler", "domain_cooldown",
                          f"{app.get('domain')} cooling down after {health['consecutive_failures']} failures",
                          health, level="WARNING")

    def notify_attention(self, app: dict[str, Any], info: dict[str, Any]) -> None:
        log.warning("needs attention: %s — %s", app["id"], info.get("reason"))
        for listener in self.attention_listeners:
            with contextlib.suppress(Exception):
                listener(app, info)

    async def _on_bridge_event(self, event: str, app_id: str | None, data: dict[str, Any]) -> None:
        if event == "tab_closed" and app_id:
            self.db.event(app_id, "browser", "tab_closed", "Application tab was closed", data)
            with contextlib.suppress(Exception):
                app = self.db.get_application(app_id)
                self.db.update_application(app_id, data={"tab_reopen_required": app["state"] not in TERMINAL},
                                           tab_id=None, window_id=None)
        if event == "tab_navigated" and app_id:
            url = redact_url(str(data.get("url") or ""))
            with contextlib.suppress(Exception):
                self.db.update_application(app_id, current_url=url[:2000])
                self.db.event(app_id, "browser", "tab_navigated", "Application tab navigated",
                              {"url": url, "tab_id": data.get("tab_id"), "window_id": data.get("window_id")})
        if event == "hello":
            self.wake()

    # ------------------------------------------------------------------ background loops
    async def _housekeeping(self, owner: str) -> None:
        park_timeout = self.cfg.settings.reasoning.park_timeout_seconds
        while not self.closing:
            with contextlib.suppress(Exception):
                self.db.acquire_lock("backend", owner, ttl=60)
                # Unblock applications whose reasoning finished but were never woken.
                scoped = self.test_application_id()
                rows = self.db.query("SELECT id FROM applications WHERE blocked_on='reasoning'" +
                                     (" AND id=?" if scoped else ""), (scoped,) if scoped else ())
                ready = [r["id"] for r in rows if not self.reasoning.open_items_for(r["id"])]
                if ready:
                    self.db.unblock(ready)
                # Reasoning that waited too long → let the application ask the user instead.
                stale_sql = "SELECT DISTINCT r.id FROM reasoning_items r"
                stale_params: list[Any] = []
                if scoped:
                    stale_sql += " JOIN reasoning_waiters w ON w.item_id=r.id"
                stale_sql += " WHERE r.status IN ('PENDING','LEASED') AND r.created_at < ?"
                stale_params.append(time.time() - park_timeout)
                if scoped:
                    stale_sql += " AND w.application_id=?"
                    stale_params.append(scoped)
                stale = self.db.query(stale_sql, stale_params)
                for row in stale:
                    waiters = self.db.complete_reasoning(row["id"], {"needs_user": "No answer from Astra in time"}, status="FAILED")
                    self.db.unblock(w["application_id"] for w in waiters)
                # Backlog completion detection.
                if self.control() == RUNNING and self.mode() in ("backlog", "continuous", "test_one"):
                    counts = self.db.claimable_counts()
                    active = self.db.query(
                        "SELECT COUNT(*) AS n FROM applications WHERE state NOT IN "
                        "('INELIGIBLE','SUBMITTED','SUBMISSION_UNCONFIRMED','FAILED','SKIPPED','WAITING_FOR_CAPTCHA',"
                        "'NEEDS_USER','READY_TO_SUBMIT','DISCOVERED')")[0]["n"]
                    if active == 0 and counts["blocked"] == 0 and not self.db.meta_get("backlog_complete", False):
                        self.db.meta_set("backlog_complete", True)
                        self.db.event(None, "queue", "backlog_complete", "Backlog exhausted", {"mode": self.mode()})
                        if self.mode() == "continuous":
                            self.db.meta_set("mode", "new_jobs")
            await self._sleep(10)

    async def _discovery(self) -> None:
        """NEW JOBS mode: poll the repository and queue only never-seen jobs."""
        while not self.closing:
            interval = self.cfg.settings.automation.poll_interval_seconds
            if self.control() == RUNNING and self.mode() == "new_jobs":
                last = float(self.db.meta_get("last_sync_at", 0) or 0)
                if time.time() - last >= interval:
                    try:
                        await self.sync("new_jobs")
                    except Exception as exc:
                        log.warning("new-jobs sync failed: %s", exc)
                        self.db.meta_set("last_sync_at", time.time())
            await self._sleep(min(60.0, interval))

    async def _captcha_watch(self) -> None:
        interval = self.cfg.settings.automation.captcha_check_interval
        while not self.closing:
            await self._sleep(interval)
            if not self.bridge.connected:
                continue
            scoped = self.test_application_id()
            for app in self.db.needs_user():
                if scoped and app["id"] != scoped:
                    continue
                info = app["data"].get("needs_user") or {}
                with contextlib.suppress(Exception):
                    if app["state"] == S.WAITING_FOR_CAPTCHA and info.get("kind") == "captcha":
                        await self.retry_captcha_detection(app["id"])
                    elif app["state"] == S.NEEDS_USER and info.get("kind") == "login":
                        await self.retry_login_detection(app["id"])

    async def retry_login_detection(self, app_id: str) -> dict[str, Any]:
        """Read the owned tab and resume after a manual sign-in has visibly completed."""
        app = self.db.get_application(app_id)
        info = app["data"].get("needs_user") or {}
        if app["state"] != S.NEEDS_USER or info.get("kind") != "login":
            raise InvalidTransition("application is not waiting for manual login")
        if not self.bridge.connected or app_id not in self.bridge.owned:
            return {"waiting": True, "browser_connected": self.bridge.connected,
                    "window_owned": app_id in self.bridge.owned}
        raw = await self.bridge.call(app_id, "scan", {}, timeout=15)
        snap = PageSnapshot.model_validate(raw or {})
        self.db.update_application(app_id, current_url=redact_url(snap.url)[:2000])
        if snap.page_kind in {"login", "signup", "unknown", "error", "captcha"}:
            return {"waiting": True, "page_kind": snap.page_kind}
        out = self.resume_application(app_id, reason="manual login completed in Firefox")
        self.db.event(app_id, "account", "login_completed",
                      "Manual login completed; worker will resume automatically",
                      {"url": redact_url(snap.url), "page_kind": snap.page_kind})
        return {"waiting": False, "state": out["state"], "page_kind": snap.page_kind}

    @staticmethod
    def _valid_after_captcha(snap: PageSnapshot) -> bool:
        """Require a recognizable live page before handing it back to a worker."""
        if snap.captcha.interactive or snap.page_kind == "captcha":
            return False
        if snap.page_kind in {"unknown", "error"}:
            return bool(snap.fields or snap.buttons or snap.confirmation.detected)
        return True

    async def retry_captcha_detection(self, app_id: str, *, focus: bool = False) -> dict[str, Any]:
        """Perform one read-only challenge check and atomically resume when it is gone."""
        app = self.db.get_application(app_id)
        if app["state"] != S.WAITING_FOR_CAPTCHA:
            raise InvalidTransition(f"application is {app['state']}, not WAITING_FOR_CAPTCHA")
        if app_id in self._captcha_checks:
            return {"waiting": True, "check_in_progress": True}
        self._captcha_checks.add(app_id)
        try:
            if not self.bridge.connected or app_id not in self.bridge.owned:
                if focus and self.bridge.connected:
                    await self.open_application(app_id)
                return {"waiting": True, "browser_connected": self.bridge.connected,
                        "window_owned": app_id in self.bridge.owned}
            if focus:
                with contextlib.suppress(BrowserError, BridgeUnavailable):
                    await self.bridge.call(app_id, "focus", {}, timeout=10)
            raw = await self.bridge.call(app_id, "scan", {}, timeout=15)
            snap = PageSnapshot.model_validate(raw or {})
            current = self.db.get_application(app_id)
            if current["state"] != S.WAITING_FOR_CAPTCHA:
                return {"waiting": False, "state": current["state"]}
            now = time.time()
            wait = dict(current["data"].get("captcha_wait") or {})
            detected_at = float(wait.get("detected_at") or current["updated_at"])
            old_url = wait.get("page_url")
            current_url = redact_url(snap.url)
            navigation = bool(wait.get("navigation_observed") or (old_url and current_url != old_url))
            wait.update({"last_checked_at": now, "check_count": int(wait.get("check_count") or 0) + 1,
                         "last_page_url": current_url, "navigation_observed": navigation})
            present = bool(snap.captcha.interactive or snap.page_kind == "captcha")
            if present:
                wait["challenge_type"] = snap.captcha.kind or wait.get("challenge_type") or "unknown"
                timeout = self.cfg.settings.automation.captcha_wait_timeout
                timed_out = now - detected_at >= timeout
                if timed_out and not wait.get("timed_out_at"):
                    wait["timed_out_at"] = now
                    info = dict(current["data"].get("needs_user") or {})
                    info["attention_revision"] = int(info.get("attention_revision") or 0) + 1
                    self.db.update_application(app_id, data={"captcha_wait": wait, "needs_user": info},
                                               current_url=current_url[:2000])
                    self.db.event(app_id, "captcha", "captcha_wait_timeout",
                                  "CAPTCHA is still waiting; application remains recoverable",
                                  {"waiting_seconds": now - detected_at, "timeout_seconds": timeout,
                                   "challenge_type": wait.get("challenge_type")}, level="WARNING")
                    self.notify_attention(self.db.get_application(app_id), info)
                else:
                    self.db.update_application(app_id, data={"captcha_wait": wait}, current_url=current_url[:2000])
                if focus:
                    self.db.event(app_id, "captcha", "captcha_still_present",
                                  "CAPTCHA is still present after manual detection retry",
                                  {"waiting_seconds": now - detected_at, "check_count": wait["check_count"]})
                return {"waiting": True, "present": True, "challenge_type": wait.get("challenge_type"),
                        "waiting_seconds": now - detected_at, "timed_out": timed_out}

            if not self._valid_after_captcha(snap):
                wait["clear_but_unrecognized_at"] = now
                self.db.update_application(app_id, data={"captcha_wait": wait}, current_url=current_url[:2000])
                if focus:
                    self.db.event(app_id, "captcha", "captcha_clear_unverified",
                                  "Challenge disappeared but the page is not yet recognizable",
                                  {"url": current_url, "page_kind": snap.page_kind}, level="WARNING")
                return {"waiting": True, "present": False, "valid_page": False}

            duration = max(0.0, now - detected_at)
            possible_false_positive = not navigation and duration < max(5.0, self.cfg.settings.automation.captcha_check_interval)
            resume = {
                **wait, "resume_at": now, "time_waiting_seconds": duration,
                "resume_url": current_url, "resume_page_kind": snap.page_kind,
                "navigation_observed": navigation, "possible_false_positive": possible_false_positive,
            }
            authentication_pending = snap.page_kind in {"login", "signup"}
            target = S.ACCOUNT_REQUIRED if authentication_pending else self.resume_target(current)
            out = self.db.user_action(
                app_id, target, reason="CAPTCHA cleared; automatic resume",
                data={"captcha_resume_pending": resume, "captcha_wait": wait,
                      "page_attempts": {}, "account_attempts": 0, "page_help": 0, "apply_clicks": 0})
            self.db.event(app_id, "captcha", "captcha_cleared",
                          "CAPTCHA cleared; worker will resume automatically",
                          {"detected_at": detected_at, "resume_at": now, "time_waiting_seconds": duration,
                           "challenge_type": wait.get("challenge_type"), "navigation_observed": navigation,
                           "possible_false_positive": possible_false_positive, "resume_state": target,
                           "url": current_url, "authentication_pending": authentication_pending})
            self.wake()
            return {"waiting": False, "present": False, "valid_page": True, "state": out["state"],
                    "navigation_observed": navigation}
        except (BrowserError, BridgeUnavailable) as exc:
            return {"waiting": True, "browser_error": str(exc)}
        finally:
            self._captcha_checks.discard(app_id)

    # ------------------------------------------------------------------ user actions
    def resume_target(self, app: dict[str, Any]) -> str:
        sub = self.db.submission(app["id"])
        if sub and sub["status"] == "SUBMITTING":
            return S.SUBMITTING
        info = app["data"].get("needs_user") or {}
        wait = app["data"].get("captcha_wait") or {}
        target = wait.get("resume_state") or info.get("resume_state")
        if target == S.CHECKING_ELIGIBILITY:
            return S.CHECKING_ELIGIBILITY
        if app["id"] not in self.bridge.owned and app["state"] in (
                S.WAITING_FOR_CAPTCHA, S.NEEDS_USER, S.READY_TO_SUBMIT):
            job_open = bool(app["data"].get("pages"))
            return S.OPENING if job_open else S.READY
        if target == S.VALIDATING:
            return S.VALIDATING
        return S.APPLYING

    def resume_application(self, app_id: str, *, reason: str = "resumed by user") -> dict[str, Any]:
        app = self.db.get_application(app_id)
        if app["state"] == S.WAITING_FOR_CAPTCHA:
            raise InvalidTransition("CAPTCHA completion is detected automatically; use Retry detection for an immediate check")
        if app["state"] not in (S.NEEDS_USER, S.READY_TO_SUBMIT):
            raise InvalidTransition(f"application is {app['state']}, not waiting for the user")
        target = self.resume_target(app)
        out = self.db.user_action(app_id, target, reason=reason, clear_attempts=True,
                                  data={"page_attempts": {}, "account_attempts": 0, "page_help": 0, "apply_clicks": 0})
        if self.cfg.settings.reasoning.writing.enabled:
            # Explicit human Resume permits a new bounded writing/research attempt.
            # Detach only this application's unresolved waiters; shared items and
            # successful answers remain intact. Paid cost reservations are never reset.
            answers = {answer["field_key"]: answer for answer in self.db.answers(app_id)}
            for item in self.db.reasoning_for_application(app_id):
                if item["kind"] != "question" or not (item.get("result") or {}).get("needs_user"):
                    continue
                field_key = item["waiter_field"]
                old_id = item["id"]
                def detach_waiter(c: sqlite3.Connection, old_id: str = old_id,
                                  field_key: str = field_key) -> None:
                    c.execute("DELETE FROM reasoning_waiters WHERE item_id=? AND application_id=? AND field_key=?",
                              (old_id, app_id, field_key))

                self.db.write(detach_waiter)
                if answers.get(field_key, {}).get("method") != "user":
                    payload = {**item["payload"], "dedupe_key": f"resume:{app_id}:{field_key}:{uuid.uuid4().hex}"}
                    self.reasoning.enqueue(app_id=app_id, kind="question", payload=payload, field_key=field_key)
            job = self.db.get_job(app["job_id"]) or {}
            keys = [f"research:{company_norm(job.get('company', ''))}:{self.research.scope_key(job.get('company', ''), job, scope)}"
                    for scope in ("company", "role", "technology")]
            self.db.write(lambda c: c.executemany("UPDATE research_tasks SET state='PENDING',attempts=0,available_at=0 "
                                                "WHERE task_key=? AND state='FAILED'", [(key,) for key in keys]))
        self.wake()
        return out

    def approve_submit(self, app_id: str) -> dict[str, Any]:
        app = self.db.get_application(app_id)
        if app["state"] != S.READY_TO_SUBMIT:
            raise InvalidTransition(f"application is {app['state']}, not READY_TO_SUBMIT")
        if app_id not in self.bridge.owned:
            # The reviewed page is gone: reopen and refill, then ask for approval again.
            out = self.db.user_action(app_id, S.OPENING, reason="window closed; reopening for a fresh review",
                                      data={"submit_approved": False, "pages": []})
            self.wake()
            return out
        pages = app["data"].get("pages") or []
        out = self.db.user_action(app_id, S.VALIDATING, reason="submission approved",
                                  data={"submit_approved": True, "submit_approved_fp": pages[-1]["fingerprint"] if pages else None})
        self.wake()
        return out

    def retry_application(self, app_id: str) -> dict[str, Any]:
        app = self.db.get_application(app_id)
        if app["state"] in (S.SUBMITTED, S.SUBMITTING):
            raise InvalidTransition("already submitted/submitting; retry refused")
        if app["state"] in (S.NEEDS_USER, S.READY_TO_SUBMIT):
            return self.resume_application(app_id, reason="retry requested")
        self.db.cancel_reasoning_for(app_id)
        out = self.db.user_action(app_id, S.QUEUED, reason="retry requested", clear_attempts=True,
                                  data={"pages": [], "page_attempts": {}, "fill_passes": {}, "eligibility_review": False,
                                        "apply_clicks": 0, "account_attempts": 0, "page_help": 0, "alternates": {}})
        self.wake()
        return out

    def skip_application(self, app_id: str, note: str | None = None) -> dict[str, Any]:
        app = self.db.get_application(app_id)
        sub = self.db.submission(app_id)
        if app["state"] in (S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNCONFIRMED) or (sub and sub["status"] == "SUBMITTING"):
            raise InvalidTransition("cannot skip an application that was (or may have been) submitted; "
                                    "verify it and use confirm_submitted, or resume it")
        self.db.cancel_reasoning_for(app_id)
        return self.db.user_action(app_id, S.SKIPPED, reason=f"skipped by user{': ' + note if note else ''}")

    def confirm_submitted(self, app_id: str, note: str | None = None) -> dict[str, Any]:
        app = self.db.get_application(app_id)
        if app["state"] != S.SUBMISSION_UNCONFIRMED:
            raise InvalidTransition("only SUBMISSION_UNCONFIRMED applications can be confirmed manually")
        self.db.write(lambda c: c.execute("UPDATE submissions SET status='SUBMITTED' WHERE application_id=?", (app_id,)))
        self.db.mark_memory_submitted(app_id)
        return self.db.user_action(app_id, S.SUBMITTED, reason=f"confirmed by user{': ' + note if note else ''}")

    async def open_application(self, app_id: str) -> dict[str, Any]:
        if app_id in self.bridge.owned:
            focus = await self.bridge.call(app_id, "focus", {}, timeout=10)
            tab = await self.bridge.call(app_id, "tab_info", {}, timeout=10)
            return {"focused": True, "focus": focus, "tab": tab}
        app = self.db.get_application(app_id)
        job = self.db.get_job(app["job_id"]) or {}
        # A closed authentication/redirect tab can contain an expired one-time URL.
        # Restart this same application from its job URL when the close was observed;
        # the bridge still allocates a fresh, exclusively-owned tab for ``app_id``.
        reopen_required = bool((app.get("data") or {}).get("tab_reopen_required"))
        job_url = job.get("apply_url") or job.get("simplify_url") or job.get("direct_url")
        url = job_url if reopen_required else (app.get("current_url") or job_url)
        res = await self.bridge.call(app_id, "open", {"url": url, "strategy": "window", "active": True}, timeout=60)
        focus = await self.bridge.call(app_id, "focus", {}, timeout=10)
        tab = await self.bridge.call(app_id, "tab_info", {}, timeout=10)
        self.db.update_application(app_id, tab_id=(res or {}).get("tab_id"), window_id=(res or {}).get("window_id"),
                                   current_url=redact_url((res or {}).get("url") or url)[:2000])
        return {"opened": True, "focus": focus, "tab": tab or res}

    # ------------------------------------------------------------------ ingestion
    async def sync(self, mode: str = "backlog") -> dict[str, Any]:
        if self.db.execution_scope():
            raise ValueError("repository synchronization is disabled while a durable test-one scope exists; "
                             "explicitly start a normal automation mode first")
        cfg = self.cfg
        src, ing = cfg.preferences.source, cfg.settings.ingestion
        run_id = self.db.start_ingest(mode, src.repository)
        try:
            result = await fetch_jobs(repository=src.repository, branch=src.branch, listings_path=ing.listings_path,
                                      readme_path=src.path or ing.readme_path, terms=ing.terms, categories=ing.categories,
                                      etag=self.db.meta_get("listings_etag"), timeout=ing.fetch_timeout)
        except Exception as exc:
            self.db.finish_ingest(run_id, error=str(exc)[:500])
            raise
        stats: dict[str, Any] = {"fetched": len(result.records), "new": 0, "updated": 0, "closed": 0, "queued": 0,
                                 "not_modified": result.not_modified, "source": result.source}
        if not result.not_modified:
            baseline_done = bool(self.db.meta_get("baseline_done", False))
            batch = "incremental" if baseline_done else "baseline"
            up = self.db.upsert_jobs([r.to_db() for r in result.records], batch=batch)
            stats.update({k: up[k] for k in ("new", "updated", "closed")})
            stats["duplicate_listings"] = len(up.get("duplicate_listings", []))
            self.db.requeue_changed_ineligible(up.get("changed_ids", []) + up.get("reopened_ids", []))
            if not baseline_done:
                self.db.meta_set("baseline_done", True)
                self.db.meta_set("baseline_at", time.time())
            self.db.meta_set("listings_etag", result.etag)
        stats["queued"] = self.db.queue_jobs(mode="new_jobs" if mode == "new_jobs" else "backlog")
        self.db.meta_set("last_sync_at", time.time())
        self.db.finish_ingest(run_id, fetched=stats["fetched"], new=stats["new"], updated=stats["updated"],
                              closed=stats["closed"], queued=stats["queued"], etag=result.etag)
        self.db.event(None, "ingest", "sync_complete",
                      f"Sync ({mode}): {stats['fetched']} listings, {stats['new']} new, {stats['queued']} queued", stats)
        self.wake()
        return stats

    def select_test_job(self, job_id: str | None = None) -> dict[str, Any]:
        """Choose one locally stored, deterministically eligible listing without writes."""
        from applypilot.eligibility import evaluate

        # Default selection is deliberately stricter than explicit --job-id:
        # never fall back to one of the user's existing queued/in-progress apps.
        candidates = self.db.test_job_candidates(job_id, new_only=job_id is None)
        for job in candidates:
            decision = evaluate(job, self.cfg)
            if decision.decision in ("eligible", "review"):
                url = job.get("apply_url") or job.get("direct_url")
                existing = bool(job.get("application_id"))
                return {"job_id": job["id"], "company": job["company"], "title": job["title"],
                        "source_url": url, "eligibility": decision.decision,
                        "application_id": job.get("application_id"),
                        "application_state": job.get("application_state"),
                        "selection_status": "existing" if existing else "new",
                        "has_existing_application": existing}
        if job_id and self.db.get_job(job_id) is None:
            raise KeyError(f"unknown job {job_id}")
        if job_id:
            raise ValueError("the requested job is not eligible for a safe test (it may be inactive, already "
                             "submitted, duplicated, or lack a stored employer URL)")
        raise ValueError("no eligible previously unprocessed imported job is available; test-one will not fall "
                         "back to any existing queued or in-progress application")

    async def start_test_one(self, job_id: str | None = None, *, dry_run: bool = False,
                             browser_mode: str | None = None, watch: bool = False,
                             slow_mo_ms: int | None = None) -> dict[str, Any]:
        """Select and optionally run one existing job under a durable claim scope."""
        browser_mode = browser_mode or self.cfg.settings.browser.test_one_mode
        if browser_mode not in ("headed", "headless"):
            raise ValueError("browser_mode must be headed or headless")
        if browser_mode == "headless":
            raise ValueError("headless mode is unsupported by the Firefox-extension browser backend; "
                             "use --headed with a connected Firefox profile")
        slow_mo_ms = self.cfg.settings.browser.debug_action_delay_ms if slow_mo_ms is None else int(slow_mo_ms)
        if slow_mo_ms < 0 or slow_mo_ms > 10000:
            raise ValueError("slow_mo_ms must be between 0 and 10000")
        selected = self.select_test_job(job_id)
        if dry_run:
            return {**selected, "dry_run": True, "changed": False, "browser_mode": browser_mode,
                    "watch": watch, "slow_mo_ms": slow_mo_ms}
        if self.cfg.settings.automation.auto_submit:
            raise ValueError("test-one requires automation.auto_submit: false")
        if self.control() != STOPPED:
            raise ValueError("stop the autopilot before starting test-one")
        active = self.db.query("SELECT id FROM applications WHERE lease_until>=?", (time.time(),))
        if active:
            raise ValueError("workers still hold active leases; wait for them to stop before test-one")
        if not self.bridge.connected:
            raise ValueError("Firefox extension is not connected; open Firefox and connect the ApplyPilot "
                             "extension before running test-one")
        app_id = selected.get("application_id")
        if app_id:
            app = self.db.get_application(app_id)
            if app["state"] in (S.SUBMITTED, S.SUBMITTING, S.SUBMISSION_UNCONFIRMED, S.INELIGIBLE, S.SKIPPED, S.FAILED):
                raise ValueError(f"existing application is {app['state']}; test-one will not reset it")
        else:
            app, _ = self.db.create_application(selected["job_id"], mode="test_one")
            app_id = app["id"]
        scope = {"kind": "test_one", "application_id": app_id, "job_id": selected["job_id"],
                 "browser_mode": browser_mode, "watch": watch, "slow_mo_ms": slow_mo_ms,
                 "created_at": time.time()}
        self.db.meta_set("execution_scope", scope)
        browser: dict[str, Any] | None = None
        if app_id in self.bridge.owned or selected.get("application_id"):
            try:
                browser = await self.open_application(app_id)
                tab = (browser or {}).get("tab") or {}
                self.db.event(app_id, "browser", "headed_visibility",
                              "Focused the application tab for headed test mode",
                              {"tab_id": tab.get("tab_id"), "window_id": tab.get("window_id"),
                               "url": redact_url(tab.get("url")), "focus": (browser or {}).get("focus")})
            except (BrowserError, BridgeUnavailable) as exc:
                self.db.event(app_id, "browser", "focus_failed",
                              "Could not confirm application-tab focus",
                              {"error": str(exc)}, level="WARNING")
                browser = {"focused": False, "error": str(exc)}
        await self.set_control("start", "test_one")
        self.wake()
        return {**selected, "application_id": app_id, "application_state": self.db.get_application(app_id)["state"],
                "dry_run": False, "changed": True, "scope": scope, "browser_mode": browser_mode,
                "watch": watch, "slow_mo_ms": slow_mo_ms, "browser": browser}

    def add_manual_job(self, url: str, *, company: str | None = None, title: str | None = None,
                       location: str | None = None) -> dict[str, Any]:
        """Queue one specific posting (e.g. a safe test application) at top priority."""
        sid = simplify_id_from_url(url)
        simplify_url = simplify_url_for(sid) if sid else None
        direct = None if sid else url
        company = company or (urlhost(url) if not sid else "Unknown company")
        title = title or "Manual application"
        ats_key = ats_job_key(direct)
        record: dict[str, Any] = {
            "id": "job_" + stable_hash(sid or normalize_url(url), 20), "simplify_id": sid, "company": company,
            "company_norm": company_norm(company), "title": title, "title_norm": title_norm(title),
            "locations": [location] if location else [], "terms": [], "degrees": [], "active": True,
            "simplify_url": simplify_url, "direct_url": direct, "apply_url": direct, "ats": detect_ats(url),
            "ats_job_key": ats_key, "content_hash": stable_hash(url), "source": "manual",
            "_aliases": job_aliases(simplify_id=sid, ats_key=ats_key, urls=[direct], company=company, title=title,
                                    locations=[location] if location else []),
        }
        up = self.db.upsert_jobs([record], batch="manual")
        job_id = (up["new_ids"] or [None])[0] or self.db.find_job_by_aliases(record.get("_aliases", []))
        if job_id is None:
            job_id = record["id"]
        app, created = self.db.create_application(job_id, mode="manual", priority=0)
        if created:
            self.db.update_application(app["id"], domain=domain_key(url), ats=detect_ats(url))
        self.wake()
        return {"application_id": app["id"], "job_id": job_id, "created": created, "state": app["state"]}

    # ------------------------------------------------------------------ status
    def today_start(self) -> float:
        return dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def workers(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in w.items() if not k.startswith("_")} for _, w in sorted(self.activity.items())]


def urlhost(url: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "unknown").removeprefix("www.")
    return host.split(".")[-2].title() if host.count(".") >= 1 else host


__all__ = ["Engine", "RUNNING", "PAUSED", "STOPPED", "MODES", "new_id"]
