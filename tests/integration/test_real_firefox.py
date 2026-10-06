"""Real headless Firefox + the real extension (web-ext) + backend + mock ATS sites.

Skipped unless a Firefox binary is available (APPLYPILOT_FIREFOX or the portable install
used in CI/dev: ~/.local/share/jp-firefox/firefox/firefox).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path

import pytest
from harness import ROOT, Backend, FixtureServer, SimulatedAstra, free_port, node_bin
from test_e2e_fixtures import Client, FakeEmail, run_until_settled

from applypilot.api import create_app
from applypilot.bridge import BrowserBridge
from applypilot.config import load_config
from applypilot.db import Database
from applypilot.engine.scheduler import Engine


def firefox_bin() -> str | None:
    for candidate in (os.environ.get("APPLYPILOT_FIREFOX"), str(Path.home() / ".local/share/jp-firefox/firefox/firefox")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


pytestmark = [pytest.mark.firefox,
              pytest.mark.skipif(firefox_bin() is None or node_bin() is None, reason="Firefox/Node not available")]


@contextlib.contextmanager
def real_firefox():
    env = dict(os.environ)
    libdirs = list((Path.home() / ".local/share/jp-firefox/root").rglob("libasound.so.2"))
    if libdirs:
        env["LD_LIBRARY_PATH"] = f"{libdirs[0].parent}:{env.get('LD_LIBRARY_PATH', '')}"
    env["PATH"] = f"{Path(node_bin()).parent}:{env['PATH']}"  # type: ignore[arg-type]
    env["MOZ_HEADLESS"] = "1"
    log_path = ROOT / "data" / "web-ext.log"
    log_path.parent.mkdir(exist_ok=True)
    with open(log_path, "w") as log:
        yield from _run_firefox(env, log)


def _run_firefox(env, log):
    proc = subprocess.Popen(
        [node_bin(), str(ROOT / "node_modules/web-ext/bin/web-ext.js"), "run", "--source-dir", str(ROOT / "firefox"),
         "--firefox", firefox_bin(), "--no-input", "--no-reload", "--arg=--headless",
         "--pref=dom.webnotifications.enabled=false"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        yield proc
    finally:
        # web-ext spawns Firefox as a child: stop the whole process group, or Firefox outlives the test.
        import signal

        for sig in (signal.SIGTERM, signal.SIGKILL):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, sig)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                continue
            time.sleep(1)


def test_real_firefox_end_to_end(project, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_ALLOW_NO_ORIGIN", raising=False)  # real moz-extension:// origin required
    monkeypatch.setenv("APPLYPILOT_PAIRING_SECONDS", "180")
    monkeypatch.setenv("APPLYPILOT_ACCOUNT_PASSWORD", "Fixture#Pass123")
    cfg = load_config(project)
    port = free_port()
    if port != 8765:
        # The extension defaults to 127.0.0.1:8765; the test must use that port.
        port = 8765
    cfg.port = port
    db = Database(cfg.paths.db_path)
    db.initialize()
    bridge = BrowserBridge(default_timeout=cfg.settings.browser.command_timeout)
    engine = Engine(cfg, db, bridge, email=FakeEmail())
    app = create_app(cfg, db=db, bridge=bridge, engine=engine)
    jobs = [("greenhouse/index.html", "Fixture Robotics", "Software Engineering Intern"),
            ("workday/index.html", "Fixture Corp", "Platform Engineer Intern"),
            ("icims/index.html", "Icims Fixture Inc", "Data Engineering Intern"),
            ("smartrecruiters/index.html", "SmartFixture", "Mobile Developer Intern"),
            ("ashby/index.html", "Ashby Fixture Labs", "Robotics Software Intern"),
            ("captcha/index.html", "Captcha Fixture", "Frontend Engineer Intern")]
    with FixtureServer() as fx, Backend(app, port), real_firefox():
        client = Client(port, cfg.token)
        deadline = time.time() + 90
        while not client.get("/api/status")["bridge"]["connected"]:
            assert time.time() < deadline, "real Firefox extension never paired/connected"
            time.sleep(0.5)
        status = client.get("/api/status")
        assert status["bridge"]["browser"] and "firefox" in str(status["bridge"]["browser"]).lower()
        ids = {p.split("/")[0]: client.post("/api/manual", {"url": fx.url(p), "company": c, "title": t})["application_id"]
               for p, c, t in jobs}
        captcha_id = ids["captcha"]
        client.post("/api/control", {"action": "start", "mode": "backlog"})
        seen_captcha: list[str] = []

        def on_needs_user(name: str, app_id: str) -> None:
            info = client.get(f"/api/applications/{app_id}")["summary"]["needs_user"] or {}
            if info.get("kind") == "captcha" and name not in seen_captcha:
                seen_captcha.append(name)
                # A human would solve it; here we only verify the durable hand-off.
                ids.pop(name, None)

        sys_ = {"client": client, "cfg": cfg}
        try:
            states = run_until_settled(sys_, ids, SimulatedAstra(), timeout=420, on_needs_user=on_needs_user)
        except AssertionError:
            # Captcha app ends SKIPPED only after the submit click; anything else is a failure.
            raise
        assert client.get(f"/api/applications/{captcha_id}")["summary"]["state"] == "WAITING_FOR_CAPTCHA"
        for name in ("greenhouse", "workday", "icims", "smartrecruiters", "ashby"):
            assert states[name] == "SUBMITTED", (name, states)
            assert len(fx.by_fixture(name)) == 1
        assert seen_captcha == ["captcha"]
        gh = fx.by_fixture("greenhouse")[0]["data"]
        assert gh["first_name"] == "Testy" and gh["resume"] == ["resume_swe.pdf"]
