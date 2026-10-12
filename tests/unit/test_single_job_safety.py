from __future__ import annotations

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import make_job
from fastapi.testclient import TestClient

from applypilot import cli
from applypilot.api import create_app
from applypilot.bridge import BrowserBridge, BrowserError
from applypilot.cli import TwoStageServer
from applypilot.engine.runner import ApplicationRunner, Stop
from applypilot.engine.scheduler import STOPPED, Engine

H = {"X-ApplyPilot-Token": "test-token-123"}


class RecordingBridge:
    def __init__(self, *, fail_focus: bool = False):
        self.connected = True
        self.owned: dict[str, dict[str, object]] = {}
        self.handlers: list[object] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_focus = fail_focus

    async def call(self, app_id, action, payload=None, *, timeout=None):
        self.calls.append((app_id, action))
        if action == "focus":
            if self.fail_focus:
                raise BrowserError("FOCUS_FAILED", "window manager refused focus")
            return {"confirmed": True, "tab_active": True, "window_focused": True}
        if action == "tab_info":
            owned = self.owned[app_id]
            return {**owned, "url": "https://jobs.example/apply", "title": "Application"}
        if action in {"highlight", "click"}:
            return {"ok": True}
        raise AssertionError(f"unexpected action {action}")

    def status(self):
        return {"connected": True, "owned_tabs": len(self.owned)}


def _bridge(*, connected: bool = False):
    bridge = BrowserBridge()
    if connected:
        bridge._ws = object()  # type: ignore[assignment]
    return bridge


def _snapshot(db):
    return {
        "applications": db.query("SELECT * FROM applications ORDER BY id"),
        "events": db.query("SELECT * FROM application_events ORDER BY id"),
        "meta": db.query("SELECT * FROM meta ORDER BY key"),
    }


def test_one_dry_run_is_read_only(cfg, db):
    job_id = make_job(db, sid="offline-1")
    engine = Engine(cfg, db, BrowserBridge())
    before = _snapshot(db)

    result = asyncio.run(engine.start_test_one(job_id, dry_run=True))

    assert result["job_id"] == job_id and result["changed"] is False
    assert result["selection_status"] == "new"
    assert result["has_existing_application"] is False
    assert _snapshot(db) == before


def test_one_dry_run_reports_headed_defaults(cfg, db):
    job_id = make_job(db, sid="headed-default")
    result = asyncio.run(Engine(cfg, db, BrowserBridge()).start_test_one(job_id, dry_run=True))

    assert result["browser_mode"] == "headed"
    assert result["watch"] is False
    assert result["slow_mo_ms"] == cfg.settings.browser.debug_action_delay_ms


@pytest.mark.asyncio
async def test_headless_is_explicitly_rejected_without_writes(cfg, db):
    job_id = make_job(db, sid="headless-rejected")
    before = _snapshot(db)

    with pytest.raises(ValueError, match="headless mode is unsupported"):
        await Engine(cfg, db, BrowserBridge()).start_test_one(job_id, browser_mode="headless")

    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_headed_watch_and_slow_mo_propagate_and_focus_existing_tab(cfg, db):
    cfg.settings.automation.auto_submit = False
    job_id = make_job(db, sid="headed-existing")
    app, _ = db.create_application(job_id, mode="backlog")
    bridge = RecordingBridge()
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 41, "window_id": 9}
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]

    result = await engine.start_test_one(job_id, browser_mode="headed", watch=True, slow_mo_ms=500)

    assert result["application_id"] == app["id"]
    assert result["browser"]["focus"]["confirmed"] is True
    assert result["browser"]["tab"]["tab_id"] == 41
    assert bridge.calls == [(app["id"], "focus"), (app["id"], "tab_info")]
    assert db.execution_scope() | {"created_at": None} == {
        "kind": "test_one", "application_id": app["id"], "job_id": job_id,
        "browser_mode": "headed", "watch": True, "slow_mo_ms": 500, "created_at": None,
    }


@pytest.mark.asyncio
async def test_focus_failure_is_reported_without_duplicate_tab(cfg, db):
    cfg.settings.automation.auto_submit = False
    job_id = make_job(db, sid="focus-failure")
    app, _ = db.create_application(job_id, mode="backlog")
    bridge = RecordingBridge(fail_focus=True)
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 5, "window_id": 6}
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]

    result = await engine.start_test_one(job_id, browser_mode="headed")

    assert result["browser"]["focused"] is False
    assert "window manager refused focus" in result["browser"]["error"]
    assert bridge.calls == [(app["id"], "focus")]
    assert any(e["event_type"] == "focus_failed" for e in db.events(app["id"]))


@pytest.mark.asyncio
async def test_watch_slow_mo_delays_real_action_and_highlights_target(cfg, db, monkeypatch):
    job_id = make_job(db, sid="watch-action")
    app, _ = db.create_application(job_id, mode="test_one")
    db.update_application(app["id"], state="APPLYING", tab_id=4, window_id=8)
    bridge = RecordingBridge()
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 4, "window_id": 8}
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    db.meta_set("execution_scope", {"kind": "test_one", "application_id": app["id"],
                                    "browser_mode": "headed", "watch": True, "slow_mo_ms": 500})
    leased = db.claim("browser-test", "browser", lease_seconds=60, app_id=app["id"])
    assert leased is not None
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("applypilot.engine.runner.asyncio.sleep", record_sleep)
    runner = ApplicationRunner(engine, leased, "browser-test")
    await runner.cmd("click", {"element_id": "0:12"})

    assert sleeps == [0.5]
    assert bridge.calls == [(app["id"], "focus"), (app["id"], "highlight"), (app["id"], "click")]


@pytest.mark.asyncio
async def test_closed_owned_tab_reopens_same_application_before_any_page_action(cfg, db):
    job_id = make_job(db, sid="closed-tab")
    app, _ = db.create_application(job_id, mode="test_one")
    db.update_application(app["id"], state="APPLYING", tab_id=4, window_id=8)
    bridge = RecordingBridge()
    bridge.owned[app["id"]] = {"app_id": app["id"], "tab_id": 4, "window_id": 8}
    engine = Engine(cfg, db, bridge)  # type: ignore[arg-type]
    await engine._on_bridge_event("tab_closed", app["id"], {"tab_id": 4, "window_id": 8})
    bridge.owned.pop(app["id"], None)
    leased = db.claim("browser-test", "browser", lease_seconds=60, app_id=app["id"])
    assert leased is not None
    runner = ApplicationRunner(engine, leased, "browser-test")

    async def stop_after_reopen():
        return Stop(reason="test stop")

    runner.h_opening = stop_after_reopen  # type: ignore[method-assign]
    await runner.run()

    current = db.get_application(app["id"])
    assert current["state"] == "OPENING"
    assert current["tab_id"] is None and current["window_id"] is None
    assert bridge.calls == []
    assert any(e["event_type"] == "tab_closed" for e in db.events(app["id"]))


def test_cli_headed_and_headless_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        cli.main(["test-one", "--headed", "--headless", "--dry-run"])
    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_navigation_event_updates_owned_tab_diagnostics():
    bridge = BrowserBridge()
    bridge.owned["app_x"] = {"app_id": "app_x", "tab_id": 1, "window_id": 2,
                             "url": "https://old.example/"}

    await bridge._handle({"type": "event", "event": "tab_navigated", "app_id": "app_x",
                          "data": {"url": "https://auth.example/login", "tab_id": 1, "window_id": 2}})

    assert bridge.owned["app_x"]["url"] == "https://auth.example/login"


@pytest.mark.asyncio
async def test_one_creates_only_selected_application_and_never_syncs(cfg, db, monkeypatch):
    cfg.settings.automation.auto_submit = False
    first = make_job(db, company="First", url="https://first.example/jobs/1", sid="first")
    make_job(db, company="Second", url="https://second.example/jobs/2", sid="second")
    engine = Engine(cfg, db, _bridge(connected=True))

    async def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("test-one invoked repository synchronization")

    monkeypatch.setattr(engine, "sync", forbidden_sync)
    result = await engine.start_test_one(first)

    apps = db.query("SELECT id, job_id FROM applications")
    assert apps == [{"id": result["application_id"], "job_id": first}]
    assert db.execution_scope()["application_id"] == result["application_id"]


@pytest.mark.asyncio
async def test_one_refuses_auto_submit(cfg, db):
    job_id = make_job(db, sid="no-auto-submit")
    cfg.settings.automation.auto_submit = True
    engine = Engine(cfg, db, _bridge(connected=True))

    with pytest.raises(ValueError, match="auto_submit"):
        await engine.start_test_one(job_id)
    assert db.application_for_job(job_id) is None


@pytest.mark.asyncio
async def test_existing_queue_unchanged_and_claim_is_scoped(cfg, db):
    cfg.settings.automation.auto_submit = False
    old_job = make_job(db, company="Old", url="https://old.example/jobs/1", sid="old")
    selected_job = make_job(db, company="Selected", url="https://selected.example/jobs/2", sid="selected")
    old_app, _ = db.create_application(old_job, mode="backlog")
    old_before = db.get_application(old_app["id"])
    engine = Engine(cfg, db, _bridge(connected=True))

    result = await engine.start_test_one(selected_job)
    claimed = db.claim("prep-test", "prep", lease_seconds=60, app_id=engine.test_application_id())

    assert claimed and claimed["id"] == result["application_id"]
    assert db.get_application(old_app["id"]) == old_before


@pytest.mark.asyncio
async def test_restart_stops_control_and_preserves_scope(cfg, db):
    cfg.settings.automation.auto_submit = False
    job_id = make_job(db, sid="restart")
    engine = Engine(cfg, db, _bridge(connected=True))
    result = await engine.start_test_one(job_id)
    scope = db.execution_scope()
    restarted = Engine(cfg, db, BrowserBridge())

    await restarted.start()
    try:
        assert restarted.control() == STOPPED
        assert db.execution_scope() == scope
        assert db.get_application(result["application_id"])["state"] == "QUEUED"
    finally:
        await restarted.shutdown()


def test_submitted_job_cannot_be_selected(cfg, db):
    job_id = make_job(db, sid="submitted")
    app, _ = db.create_application(job_id, mode="backlog")
    db.transition(app["id"], "SUBMITTED", force=True)
    engine = Engine(cfg, db, BrowserBridge())

    with pytest.raises(ValueError, match="requested job is not eligible"):
        engine.select_test_job(job_id)


def test_test_one_api_does_not_call_sync(cfg, db, monkeypatch):
    job_id = make_job(db, sid="api-offline")
    engine = Engine(cfg, db, BrowserBridge())

    async def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("sync called")

    monkeypatch.setattr(engine, "sync", forbidden_sync)
    with TestClient(create_app(cfg, db=db, engine=engine, start_engine=False)) as client:
        response = client.post("/api/test-one", headers=H, json={"job_id": job_id, "dry_run": True})
    assert response.status_code == 200 and response.json()["dry_run"] is True


def test_cli_reports_stale_backend_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(cli, "_cfg", lambda strict=False: object())
    monkeypatch.setattr(cli, "_api", lambda *_args, **_kwargs: {
        "job_id": "job_old", "company": "Old", "title": "Old", "source_url": "https://old.example",
        "application_id": "app_old", "application_state": "OPENING", "dry_run": True,
    })
    args = SimpleNamespace(job_id=None, dry_run=True)

    with pytest.raises(SystemExit, match="older test-one implementation"):
        cli.cmd_test_one(args)


def test_watch_keeps_reasoning_wait_live_and_prints_only_safe_retry_metadata(monkeypatch, capsys):
    calls = []

    def fake_api(_cfg, method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/test-one":
            return {"job_id": "job_watch", "company": "Watch Co", "title": "Role",
                    "source_url": "https://jobs.example/1", "application_id": "app_watch",
                    "application_state": "APPLYING", "selection_status": "existing",
                    "browser_mode": "headed", "watch": True, "slow_mo_ms": 0, "browser": {}}
        if path == "/api/applications/app_watch":
            return {"summary": {"state": "APPLYING", "blocked_on": "reasoning", "worker_id": None,
                                "window_id": 2, "tab_id": 3, "current_url": "https://login.example/"}}
        if path == "/api/status":
            return {"bridge": {"connected": True}, "workers": []}
        if path.startswith("/api/applications/app_watch/events"):
            return {"events": [{"id": 9, "component": "reasoning",
                                "event_type": "provider_validation_failed",
                                "message": "OpenAI response failed validation (attempt 1/3)",
                                "data": {"attempt": 1, "max_attempts": 3, "decision": "repair",
                                         "errors": ["candidate-secret-value"],
                                         "response_shape": [{"action_type": "open_url"}]}}]}
        if path == "/api/control":
            return {"control": "STOPPED"}
        raise AssertionError(f"unexpected API call {method} {path}")

    slept = []

    def interrupt_sleep(_seconds):
        slept.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cfg", lambda strict=False: object())
    monkeypatch.setattr(cli, "_api", fake_api)
    monkeypatch.setattr(cli.time, "sleep", interrupt_sleep)
    args = SimpleNamespace(job_id="job_watch", dry_run=False, headed=True, headless=False,
                           watch=True, slow_mo=0)

    assert cli.cmd_test_one(args) == 130
    output = capsys.readouterr().out
    assert slept  # blocked_on=reasoning stayed in the watch loop instead of stopping immediately
    assert "attempt=1" in output and "decision=repair" in output and "action_type" in output
    assert "candidate-secret-value" not in output


def test_default_selection_requires_a_new_job(cfg, db):
    existing_job = make_job(db, company="Existing", url="https://existing.example/1", sid="existing")
    new_job = make_job(db, company="Clean", url="https://clean.example/1", sid="clean")
    existing_app, _ = db.create_application(existing_job, mode="backlog")
    engine = Engine(cfg, db, BrowserBridge())

    selected = engine.select_test_job()

    assert selected["job_id"] == new_job
    assert selected["selection_status"] == "new"
    assert db.get_application(existing_app["id"])["state"] == "QUEUED"


def test_default_selection_does_not_fall_back_to_existing(cfg, db):
    job_id = make_job(db, sid="only-existing")
    app, _ = db.create_application(job_id, mode="backlog")
    before = _snapshot(db)
    engine = Engine(cfg, db, BrowserBridge())

    with pytest.raises(ValueError, match="no eligible previously unprocessed"):
        engine.select_test_job()

    assert db.get_application(app["id"])["state"] == "QUEUED"
    assert _snapshot(db) == before


def test_dry_run_reports_explicit_existing_job(cfg, db):
    job_id = make_job(db, sid="dry-existing")
    app, _ = db.create_application(job_id, mode="backlog")
    engine = Engine(cfg, db, BrowserBridge())

    result = asyncio.run(engine.start_test_one(job_id, dry_run=True))

    assert result["selection_status"] == "existing"
    assert result["has_existing_application"] is True
    assert result["application_id"] == app["id"]


@pytest.mark.asyncio
async def test_disconnected_firefox_fails_before_queue_or_scope(cfg, db):
    cfg.settings.automation.auto_submit = False
    job_id = make_job(db, sid="needs-firefox")
    before = _snapshot(db)
    engine = Engine(cfg, db, BrowserBridge())

    with pytest.raises(ValueError, match="Firefox extension is not connected"):
        await engine.start_test_one(job_id)

    assert db.application_for_job(job_id) is None
    assert db.execution_scope() is None
    assert engine.control() == STOPPED
    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_dry_run_does_not_require_firefox(cfg, db):
    job_id = make_job(db, sid="dry-no-firefox")
    engine = Engine(cfg, db, BrowserBridge())

    result = await engine.start_test_one(job_id, dry_run=True)

    assert result["dry_run"] is True
    assert result["selection_status"] == "new"
    assert db.application_for_job(job_id) is None


def test_two_stage_sigint_and_sigterm():
    db = SimpleNamespace(meta_set=Mock())
    engine = SimpleNamespace(db=db, wake=Mock())
    wrapper = TwoStageServer.__new__(TwoStageServer)
    wrapper.engine = engine
    wrapper.server = SimpleNamespace(should_exit=False)
    wrapper._interrupts = 0

    wrapper.handle_signal(signal.SIGINT)
    assert wrapper.server.should_exit is False
    db.meta_set.assert_called_once_with("control", "STOPPED")
    wrapper.handle_signal(signal.SIGINT)
    assert wrapper.server.should_exit is True

    wrapper.server.should_exit = False
    wrapper.handle_signal(signal.SIGTERM)
    assert wrapper.server.should_exit is True
