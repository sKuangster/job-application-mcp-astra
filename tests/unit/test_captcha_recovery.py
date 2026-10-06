from __future__ import annotations

import time
from typing import Any

import pytest
from conftest import make_job

from applypilot.engine.runner import ApplicationRunner, Stop
from applypilot.engine.scheduler import Engine
from applypilot.state import S


def page(*, captcha: bool, confirmation: bool = False, fingerprint: str = "final",
         url: str = "https://jobs.example/apply/1") -> dict[str, Any]:
    return {
        "url": url,
        "title": "Example application",
        "page_kind": "confirmation" if confirmation else ("captcha" if captcha else "form"),
        "heading": "Thank you" if confirmation else "Apply",
        "fingerprint": fingerprint,
        "fields": [] if confirmation else [
            {"id": "0:1", "key": "name", "type": "text", "label": "Name", "required": True, "value": "Testy"}
        ],
        "buttons": [] if confirmation else [{"id": "0:2", "text": "Submit", "kind": "submit"}],
        "captcha": {"present": captcha, "interactive": captcha, "kind": "recaptcha" if captcha else None,
                    "detail": "test challenge" if captcha else None},
        "confirmation": {"detected": confirmation, "text": "Application received" if confirmation else None},
    }


class FakeBridge:
    def __init__(self, scans: list[dict[str, Any]] | None = None) -> None:
        self.connected = True
        self.owned: dict[str, dict[str, Any]] = {}
        self.handlers: list[Any] = []
        self.scans = list(scans or [])
        self.calls: list[str] = []
        self.metrics: dict[str, int] = {}

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "owned_tabs": len(self.owned)}

    async def call(self, app_id: str, action: str, payload: dict[str, Any] | None = None,
                   *, timeout: float | None = None) -> Any:
        self.calls.append(action)
        if action == "scan":
            return self.scans.pop(0) if len(self.scans) > 1 else self.scans[0]
        if action == "screenshot":
            return {"data_url": ""}
        if action == "focus":
            return {"focused": True}
        raise AssertionError(f"unexpected browser action {action}")


def waiting_app(db: Any, bridge: FakeBridge, *, resume_state: str = S.APPLYING,
                detected_at: float | None = None) -> dict[str, Any]:
    job_id = make_job(db, company="Captcha Co", url="https://jobs.example/apply/1")
    app, _ = db.create_application(job_id, mode="backlog")
    since = detected_at or time.time()
    db.update_application(
        app["id"], state=S.WAITING_FOR_CAPTCHA, tab_id=7, window_id=8,
        data={
            "needs_user": {"kind": "captcha", "reason": "CAPTCHA required", "since": since,
                           "resume_state": resume_state},
            "captcha_wait": {"detected_at": since, "page_url": "https://jobs.example/apply/1",
                             "page_fingerprint": "final", "challenge_type": "recaptcha",
                             "resume_state": resume_state, "check_count": 0},
        })
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 7, "window_id": 8}
    return db.get_application(app["id"])


@pytest.mark.asyncio
async def test_waiting_state_releases_capacity_and_auto_resumes_from_read_only_scan(cfg: Any, db: Any) -> None:
    bridge = FakeBridge([page(captcha=False)])
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    waiting = waiting_app(db, bridge)
    other_job = make_job(db, company="Other Co", url="https://other.example/apply")
    other, _ = db.create_application(other_job, mode="backlog")
    db.update_application(other["id"], state=S.READY, domain="other.example")

    claimed = db.claim("browser-2", "browser", lease_seconds=60)
    assert claimed and claimed["id"] == other["id"]
    assert db.get_application(waiting["id"])["lease_token"] is None

    result = await engine.retry_captcha_detection(waiting["id"])
    resumed = db.get_application(waiting["id"])
    assert result["waiting"] is False and resumed["state"] == S.APPLYING
    assert bridge.calls == ["scan"]  # polling never clicks, fills, or enters the widget
    events = db.events(waiting["id"])
    assert any(e["event_type"] == "captcha_cleared" for e in events)
    assert resumed["data"]["captcha_resume_pending"]["time_waiting_seconds"] >= 0
    assert resumed["data"]["captcha_resume_pending"]["possible_false_positive"] is True


@pytest.mark.asyncio
async def test_navigation_after_challenge_and_later_reappearance_are_recorded(cfg: Any, db: Any) -> None:
    bridge = FakeBridge([
        page(captcha=False, fingerprint="step-2", url="https://jobs.example/apply/2"),
        page(captcha=True, fingerprint="step-2", url="https://jobs.example/apply/2"),
    ])
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    app = waiting_app(db, bridge)

    result = await engine.retry_captcha_detection(app["id"])
    assert result["navigation_observed"] is True
    leased = db.claim("browser-1", "browser", lease_seconds=60, app_id=app["id"])
    runner = ApplicationRunner(engine, leased, "browser-1")
    stop = await runner.h_applying()
    assert isinstance(stop, Stop)
    assert db.get_application(app["id"])["state"] == S.WAITING_FOR_CAPTCHA
    assert "click" not in bridge.calls and "fill" not in bridge.calls
    assert any(e["event_type"] == "captcha_detected" for e in db.events(app["id"]))


@pytest.mark.asyncio
async def test_restart_and_browser_reconnect_leave_captcha_recoverable(cfg: Any, db: Any) -> None:
    bridge = FakeBridge([page(captcha=False)])
    first_engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    app = waiting_app(db, bridge)
    del first_engine

    bridge.connected = False
    restarted = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    offline = await restarted.retry_captcha_detection(app["id"])
    assert offline["waiting"] is True
    assert db.get_application(app["id"])["state"] == S.WAITING_FOR_CAPTCHA

    bridge.connected = True
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 7, "window_id": 8}
    resumed = await restarted.retry_captcha_detection(app["id"])
    assert resumed["waiting"] is False
    assert db.get_application(app["id"])["state"] == S.APPLYING


@pytest.mark.asyncio
async def test_timeout_is_bounded_attention_not_failure(cfg: Any, db: Any) -> None:
    bridge = FakeBridge([page(captcha=True)])
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    app = waiting_app(db, bridge, detected_at=time.time() - 61)

    result = await engine.retry_captcha_detection(app["id"])
    current = db.get_application(app["id"])
    assert result["waiting"] is True and result["timed_out"] is True
    assert current["state"] == S.WAITING_FOR_CAPTCHA
    assert current["data"]["captcha_wait"]["timed_out_at"]
    assert any(e["event_type"] == "captcha_wait_timeout" for e in db.events(app["id"]))
    assert bridge.calls == ["scan"]


@pytest.mark.asyncio
async def test_submission_captcha_revalidates_and_does_not_authorize_or_duplicate_submit(cfg: Any, db: Any) -> None:
    bridge = FakeBridge([page(captcha=False)])
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    job_id = make_job(db, company="Submit Captcha", url="https://jobs.example/apply/1")
    app, _ = db.create_application(job_id, mode="backlog")
    db.update_application(app["id"], state=S.VALIDATING, tab_id=7, window_id=8)
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 7, "window_id": 8}
    leased = db.claim("browser-1", "browser", lease_seconds=60, app_id=app["id"])
    reserved = db.reserve_submission(app["id"], worker_id="browser-1", token=leased["lease_token"])
    runner = ApplicationRunner(engine, reserved, "browser-1")
    runner.save(data={"submit_clicked_at": time.time(), "submit_intent": {
        "url": "https://jobs.example/apply/1", "fingerprint": "final", "button_id": "0:2"}})

    from applypilot.models import PageSnapshot

    stop = await runner._submission_outcome(PageSnapshot.model_validate(page(captcha=True)), changed=False, resumed=False)
    assert isinstance(stop, Stop)
    assert db.get_application(app["id"])["state"] == S.WAITING_FOR_CAPTCHA
    assert db.submission(app["id"])["status"] == "ABORTED"
    runner.release(stop)

    recovered = await engine.retry_captcha_detection(app["id"])
    assert recovered["state"] == S.VALIDATING  # normal validation + gate must run again
    assert db.submission(app["id"])["status"] == "ABORTED"
    assert "click" not in bridge.calls and "fill" not in bridge.calls


@pytest.mark.asyncio
async def test_uncertain_post_click_captcha_only_inspects_confirmation(cfg: Any, db: Any) -> None:
    bridge = FakeBridge([page(captcha=False, confirmation=True)])
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    job_id = make_job(db, company="No Duplicate", url="https://jobs.example/apply/1")
    app, _ = db.create_application(job_id, mode="backlog")
    db.update_application(app["id"], state=S.VALIDATING, tab_id=7, window_id=8)
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 7, "window_id": 8}
    leased = db.claim("browser-1", "browser", lease_seconds=60, app_id=app["id"])
    db.reserve_submission(app["id"], worker_id="browser-1", token=leased["lease_token"])
    db.update_application(app["id"], worker_id="browser-1", token=leased["lease_token"],
                          data={"submit_clicked_at": time.time()})
    db.transition(app["id"], S.WAITING_FOR_CAPTCHA, worker_id="browser-1", token=leased["lease_token"],
                  data={"needs_user": {"kind": "captcha", "since": time.time(), "resume_state": S.SUBMITTING},
                        "captcha_wait": {"detected_at": time.time(), "page_url": "https://challenge.example/",
                                         "resume_state": S.SUBMITTING}})
    db.release(app["id"], "browser-1", leased["lease_token"])

    await engine.retry_captcha_detection(app["id"])
    resumed = db.claim("browser-2", "browser", lease_seconds=60, app_id=app["id"])
    runner = ApplicationRunner(engine, resumed, "browser-2")
    outcome = await runner.h_submitting()
    assert outcome == S.SUBMITTED
    assert db.submission(app["id"])["status"] == "SUBMITTED"
    assert bridge.calls.count("scan") == 2
    assert "click" not in bridge.calls
