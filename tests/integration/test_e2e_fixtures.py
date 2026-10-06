"""Full system against mock ATS sites: backend + workers + bridge + real extension JS (jsdom) + simulated Astra."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from harness import Backend, FakeBrowser, FixtureServer, SimulatedAstra, free_port, node_bin

from applypilot.accounts import Verification
from applypilot.api import create_app
from applypilot.bridge import BrowserBridge
from applypilot.config import load_config
from applypilot.db import Database
from applypilot.engine.scheduler import Engine

pytestmark = pytest.mark.skipif(node_bin() is None, reason="Node.js is required for the fake browser")

FIXTURE_JOBS = [
    ("greenhouse/index.html", "Fixture Robotics", "Software Engineering Intern"),
    ("lever/index.html", "Lever Fixture Co", "Backend Engineer Intern"),
    ("ashby/index.html", "Ashby Fixture Labs", "Robotics Software Intern"),
    ("workday/index.html", "Fixture Corp", "Platform Engineer Intern"),
    ("icims/index.html", "Icims Fixture Inc", "Data Engineering Intern"),
    ("smartrecruiters/index.html", "SmartFixture", "Mobile Developer Intern"),
    ("custom/index.html", "Custom Fixture", "Full Stack Intern"),
    ("validation/index.html", "Validation Fixture", "QA Engineer Intern"),
    ("verification/index.html", "Verify Fixture", "Security Engineer Intern"),
    ("captcha/index.html", "Captcha Fixture", "Frontend Engineer Intern"),
    ("closed/index.html", "Closed Fixture", "Firmware Intern"),
]
TERMINAL = {"SUBMITTED", "SUBMISSION_UNCONFIRMED", "FAILED", "SKIPPED", "INELIGIBLE"}


class FakeEmail:
    name = "fake"

    def configured(self) -> bool:
        return True

    async def find_verification(self, **_: Any) -> Verification:
        return Verification("code", "123456", "Your verification code", "noreply@fixture", time.time())


class Client:
    def __init__(self, port: int, token: str):
        self.http = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"X-ApplyPilot-Token": token}, timeout=90)

    def get(self, path: str, **params: Any) -> Any:
        r = self.http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        r = self.http.post(path, json=body or {})
        assert r.status_code < 400, r.text
        return r.json()

    def rpc(self, op: str, **args: Any) -> Any:
        return self.post(f"/api/rpc/{op}", args)


def dump(client: Client, apps: dict[str, str]) -> str:
    lines = []
    for name, app_id in apps.items():
        d = client.get(f"/api/applications/{app_id}")
        lines.append(f"== {name}: {d['summary']['state']} ({d['summary']['state_reason']})")
        for e in d["events"][-14:]:
            lines.append(f"   [{e['component']}] {e['event_type']}: {e['message']} {json.dumps(e['data'])[:300]}")
    return "\n".join(lines)


@pytest.fixture
def system(project, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_ALLOW_NO_ORIGIN", "1")
    monkeypatch.setenv("APPLYPILOT_ACCOUNT_PASSWORD", "Fixture#Pass123")
    monkeypatch.setenv("APPLYPILOT_ACCOUNT_EMAIL", "testy.fixture@example.com")
    cfg = load_config(project)
    port = free_port()
    cfg.port = port
    db = Database(cfg.paths.db_path)
    db.initialize()
    bridge = BrowserBridge(default_timeout=cfg.settings.browser.command_timeout)
    engine = Engine(cfg, db, bridge, email=FakeEmail())
    app = create_app(cfg, db=db, bridge=bridge, engine=engine)
    with FixtureServer() as fx, Backend(app, port), FakeBrowser(port, cfg.token):
        client = Client(port, cfg.token)
        deadline = time.time() + 20
        while not client.get("/api/status")["bridge"]["connected"]:
            assert time.time() < deadline, "fake browser never connected"
            time.sleep(0.1)
        yield {"cfg": cfg, "fx": fx, "client": client, "engine": engine, "db": db}


def run_until_settled(sys_: dict[str, Any], apps: dict[str, str], astra: SimulatedAstra, *, timeout: float = 240,
                      on_needs_user: Any = None) -> dict[str, str]:
    client: Client = sys_["client"]
    deadline = time.time() + timeout
    states: dict[str, str] = {}
    while time.time() < deadline:
        batch = client.rpc("get_reasoning_batch", wait_seconds=0.5)
        if batch["items"]:
            res = client.rpc("submit_reasoning", results=astra.answer(batch))
            assert not res["rejected"], res
        states = {name: client.get(f"/api/applications/{app_id}")["summary"]["state"] for name, app_id in apps.items()}
        if on_needs_user:
            for name, state in states.items():
                if state == "NEEDS_USER":
                    on_needs_user(name, apps[name])
        if all(s in TERMINAL for s in states.values()):
            return states
        time.sleep(0.3)
    report = sys_["cfg"].paths.data_dir / "e2e_report.json"
    report.write_text(json.dumps({n: client.get(f"/api/applications/{a}") for n, a in apps.items()}, indent=1, default=str))
    raise AssertionError(f"did not settle: {states}\nfull report: {report}\n{dump(client, apps)}")


def solve_captcha(sys_: dict[str, Any], app_id: str) -> None:
    engine: Engine = sys_["engine"]
    loop = engine.tasks[0].get_loop()
    fut = asyncio.run_coroutine_threadsafe(
        engine.bridge.call(app_id, "__test_eval", {"code": (
            "window.FixtureKit.solveCaptcha();"
            "Array.from(document.querySelectorAll('button')).find(b => /submit/i.test(b.textContent)).click(); true")},
            timeout=10), loop)
    fut.result(15)


def test_full_pipeline_against_mock_ats(system):
    client: Client = system["client"]
    fx: FixtureServer = system["fx"]
    apps = {}
    for path, company, title in FIXTURE_JOBS:
        res = client.post("/api/manual", {"url": fx.url(path), "company": company, "title": title})
        apps[path.split("/")[0]] = res["application_id"]
    client.post("/api/control", {"action": "start", "mode": "backlog"})
    astra = SimulatedAstra()
    solved: set[str] = set()
    captcha_events: list[str] = []

    def on_needs_user(name: str, app_id: str) -> None:
        detail = client.get(f"/api/applications/{app_id}")
        info = detail["summary"]["needs_user"] or {}
        if info.get("kind") == "captcha" and app_id not in solved:
            captcha_events.append(name)
            # Other workers must keep going while this one waits for the human, who solves the CAPTCHA,
            # clicks Submit themselves (ApplyPilot never re-clicks after a CAPTCHA) and presses Resume.
            solve_captcha(system, app_id)
            solved.add(app_id)
            time.sleep(0.5)
            client.post(f"/api/applications/{app_id}/action", {"action": "resume"})

    states = run_until_settled(system, apps, astra, on_needs_user=on_needs_user)
    report = dump(client, apps)
    expected_submitted = {"greenhouse", "lever", "ashby", "workday", "icims", "smartrecruiters", "custom", "validation",
                          "verification", "captcha"}
    assert {k for k, v in states.items() if v == "SUBMITTED"} == expected_submitted, f"{states}\n{report}"
    assert states["closed"] == "SKIPPED"
    assert captcha_events == ["captcha"]

    # Exactly one real submission per site — never a double submit.
    for name in expected_submitted:
        assert len(fx.by_fixture(name)) == 1, (name, fx.submissions)

    gh = fx.by_fixture("greenhouse")[0]["data"]
    assert gh["first_name"] == "Testy" and gh["last_name"] == "McFixture" and gh["email"] == "testy.fixture@example.com"
    assert gh["work_authorization"] == "Yes" and gh["sponsorship"] == "No"
    assert gh["resume"] == ["resume_swe.pdf"]
    assert "RoboSort" in gh["why_fixture_robotics"]
    assert "decline" in str(gh["gender"]).lower() or "wish" in str(gh["gender"]).lower()
    wd = fx.by_fixture("workday")[0]["data"]
    assert wd["firstName"] == "Testy" and wd["previousWorker"].lower().startswith("no")
    assert fx.by_fixture("validation")[0]["data"]["phone"].startswith("+")

    # Audit trail: every answer has provenance.
    detail = client.get(f"/api/applications/{apps['greenhouse']}")
    by_q = {a["question"]: a for a in detail["answers"]}
    first = next(a for q, a in by_q.items() if q.lower().startswith("first name"))
    assert first["method"] == "fact" and first["source"]["facts"][0]["path"] == "candidate.identity.first_name"
    why = next(a for q, a in by_q.items() if q.lower().startswith("why do you want"))
    assert why["method"] == "generated" and why["status"] in ("filled", "verified")
    assert detail["submission"]["status"] == "SUBMITTED"
    assert any(e["event_type"] == "submission_gate" for e in detail["events"])
    assert all(e["application_id"] == apps["greenhouse"] for e in detail["events"])
    ids = [e["id"] for e in detail["events"]]
    assert ids == sorted(ids)

    # Account creation was recorded without the password.
    wd_detail = client.get(f"/api/applications/{apps['workday']}")
    assert any(e["event_type"] == "account_created" for e in wd_detail["events"])
    assert "Fixture#Pass123" not in json.dumps(wd_detail)
    assert "Fixture#Pass123" not in (system["cfg"].paths.logs_dir / "applypilot.jsonl").read_text(errors="ignore") \
        if (system["cfg"].paths.logs_dir / "applypilot.jsonl").exists() else True

    # Model usage stayed low: most fields were deterministic.
    methods = client.get("/api/metrics")["answers_by_method"]
    deterministic = sum(v for k, v in methods.items() if k in ("fact", "preference", "eeo", "file", "deterministic"))
    assert deterministic > methods.get("generated", 0)


def test_duplicate_jobs_submit_once(system):
    client: Client = system["client"]
    fx: FixtureServer = system["fx"]
    a = client.post("/api/manual", {"url": fx.url("greenhouse/index.html?loc=nyc"), "company": "Fixture Robotics",
                                    "title": "Software Engineering Intern", "location": "New York, NY"})
    # Same company/role posted for another office: a distinct job, but an equivalent application.
    b = client.post("/api/manual", {"url": fx.url("greenhouse/index.html?loc=sf"), "company": "Fixture Robotics, Inc.",
                                    "title": "Software Engineering Intern (Summer 2027)", "location": "San Francisco, CA"})
    assert a["application_id"] != b["application_id"]
    # Re-adding the identical posting is idempotent (same application).
    again = client.post("/api/manual", {"url": fx.url("greenhouse/index.html?loc=nyc&utm_source=x"), "company": "Fixture Robotics",
                                        "title": "Software Engineering Intern", "location": "New York, NY"})
    assert again["application_id"] == a["application_id"]
    client.post("/api/control", {"action": "start", "mode": "backlog"})
    states = run_until_settled(system, {"a": a["application_id"], "b": b["application_id"]}, SimulatedAstra())
    assert sorted(states.values()) == ["SKIPPED", "SUBMITTED"]
    assert len(fx.by_fixture("greenhouse")) == 1


def test_pause_stops_claiming_and_resume_continues(system):
    client: Client = system["client"]
    fx: FixtureServer = system["fx"]
    ids = {}
    for path, company, title in FIXTURE_JOBS[:3]:
        ids[path.split("/")[0]] = client.post("/api/manual", {"url": fx.url(path), "company": company, "title": title})["application_id"]
    client.post("/api/control", {"action": "pause"})
    time.sleep(1.5)
    assert all(client.get(f"/api/applications/{i}")["summary"]["state"] == "QUEUED" for i in ids.values())
    client.post("/api/control", {"action": "resume"})
    states = run_until_settled(system, ids, SimulatedAstra())
    assert set(states.values()) == {"SUBMITTED"}


def test_one_failure_does_not_stop_other_workers(system):
    client: Client = system["client"]
    fx: FixtureServer = system["fx"]
    dead_port = free_port()  # nothing listens here → navigation fails every time
    ids = {
        "broken": client.post("/api/manual", {"url": f"http://127.0.0.1:{dead_port}/x/apply", "company": "Broken Co",
                                              "title": "Intern"})["application_id"],
        "greenhouse": client.post("/api/manual", {"url": fx.url("greenhouse/index.html"), "company": "Fixture Robotics",
                                                  "title": "Software Engineering Intern"})["application_id"],
        "lever": client.post("/api/manual", {"url": fx.url("lever/index.html"), "company": "Lever Fixture Co",
                                             "title": "Backend Engineer Intern"})["application_id"],
    }
    client.post("/api/control", {"action": "start", "mode": "backlog"})
    states = run_until_settled(system, ids, SimulatedAstra(), timeout=180)
    assert states["greenhouse"] == "SUBMITTED" and states["lever"] == "SUBMITTED"
    assert states["broken"] in ("FAILED", "SKIPPED", "NEEDS_USER") or states["broken"] == "FAILED"
    detail = client.get(f"/api/applications/{ids['broken']}")
    assert detail["summary"]["state"] in ("FAILED", "SKIPPED")
    assert any(e["event_type"] in ("retry_scheduled", "error") for e in detail["events"]) or \
        detail["summary"]["state"] == "SKIPPED"


def test_restart_resumes_persisted_work(project, monkeypatch):
    """Kill the backend while applications wait on Astra; a new backend + browser finishes them."""
    monkeypatch.setenv("APPLYPILOT_ALLOW_NO_ORIGIN", "1")
    cfg = load_config(project)
    with FixtureServer() as fx:
        ids: dict[str, str] = {}
        for round_ in (1, 2):
            port = free_port()
            cfg.port = port
            db = Database(cfg.paths.db_path)
            db.initialize()
            bridge = BrowserBridge(default_timeout=cfg.settings.browser.command_timeout)
            engine = Engine(cfg, db, bridge, email=FakeEmail())
            app = create_app(cfg, db=db, bridge=bridge, engine=engine)
            with Backend(app, port), FakeBrowser(port, cfg.token):
                client = Client(port, cfg.token)
                deadline = time.time() + 20
                while not client.get("/api/status")["bridge"]["connected"]:
                    assert time.time() < deadline
                    time.sleep(0.1)
                if round_ == 1:
                    for path, company, title in FIXTURE_JOBS[:2]:
                        ids[path.split("/")[0]] = client.post("/api/manual", {"url": fx.url(path), "company": company,
                                                                               "title": title})["application_id"]
                    client.post("/api/control", {"action": "start", "mode": "backlog"})
                    deadline = time.time() + 90
                    while time.time() < deadline:  # wait until both park on Astra
                        st = {k: client.get(f"/api/applications/{v}")["summary"]["state"] for k, v in ids.items()}
                        if all(s == "ANSWERING" for s in st.values()) or client.get("/api/status")["pending_reasoning"] >= 2:
                            break
                        time.sleep(0.3)
                    assert client.get("/api/status")["pending_reasoning"] >= 1
                else:
                    assert client.get("/api/status")["control"] == "RUNNING"  # persisted
                    states = run_until_settled({"client": client, "cfg": cfg}, ids, SimulatedAstra(), timeout=180)
                    assert set(states.values()) == {"SUBMITTED"}
                    for name in ids:
                        assert len(fx.by_fixture(name)) == 1
