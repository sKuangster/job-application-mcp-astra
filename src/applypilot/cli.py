"""Command line: setup | run | mcp | doctor | sync | status | start | pause | resume | stop | apply-url | show | backup."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from applypilot import __version__


def _cfg(strict: bool = True) -> Any:
    from applypilot.config import ConfigError, load_config

    try:
        return load_config(strict=strict)
    except ConfigError as exc:
        print(f"\n{exc}\n\nFix the files above (see config/examples/ for documented templates).", file=sys.stderr)
        raise SystemExit(2) from exc


def _base_url(cfg: Any) -> str:
    return f"http://{cfg.host}:{cfg.port}"


def _api(cfg: Any, method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 90) -> Any:
    import httpx

    if not cfg.token:
        raise SystemExit("APPLYPILOT_TOKEN is not set. Run `applypilot setup` first.")
    try:
        resp = httpx.request(method, _base_url(cfg) + path, json=body, timeout=timeout,
                             headers={"X-ApplyPilot-Token": cfg.token})
    except httpx.ConnectError as exc:
        raise SystemExit(f"Backend not reachable at {_base_url(cfg)} — start it with `applypilot run`.") from exc
    if resp.status_code >= 400:
        raise SystemExit(f"Error {resp.status_code}: {resp.text[:800]}")
    return resp.json()


def cmd_setup(args: argparse.Namespace) -> int:
    from dotenv import dotenv_values

    from applypilot.config import make_paths
    from applypilot.security import generate_token, upsert_env_file

    paths = make_paths()
    env = paths.env_file
    if not env.exists():
        example = paths.root / ".env.example"
        env.write_text(example.read_text(encoding="utf-8") if example.exists() else "", encoding="utf-8")
    existing = dotenv_values(env)
    values = {}
    if not existing.get("APPLYPILOT_TOKEN") or args.rotate:
        values["APPLYPILOT_TOKEN"] = generate_token()
    if not existing.get("APPLYPILOT_MCP_TOKEN") or args.rotate:
        values["APPLYPILOT_MCP_TOKEN"] = generate_token(24)
    if values:
        upsert_env_file(env, values)
        print(f"Wrote new token(s) to {env}")
    settings = paths.config_dir / "settings.yaml"
    if not settings.exists():
        shutil.copyfile(paths.config_dir / "examples" / "settings.example.yaml", settings)
        print(f"Created {settings}")
    for d in ("resumes", "templates", "profile"):
        (paths.root / d).mkdir(exist_ok=True)
    for key, value in values.items():
        os.environ[key] = value
    cfg = _cfg(strict=False)
    cfg.paths.ensure()
    from applypilot.db import Database

    db = Database(cfg.paths.db_path)
    db.initialize()
    print(f"Database ready: {cfg.paths.db_path} (schema v{db.schema_version()})")
    _print_connection(cfg)
    ready = cfg.readiness()
    if not ready["ready"]:
        print("\nStill needed before autopilot can run:")
        for item in ready["missing"]:
            print(f"  - {item}")
    for w in ready["warnings"]:
        print(f"  ! {w}")
    return 0


def _print_connection(cfg: Any) -> None:
    print("\nFirefox extension settings (ApplyPilot → Options):")
    print(f"  Backend URL: {_base_url(cfg)}")
    print(f"  Token:       {cfg.token}")
    print("\nMCP endpoints:")
    print(f"  Local (header auth):  {_base_url(cfg)}/mcp   (Authorization: Bearer <APPLYPILOT_TOKEN>)")
    print(f"  Capability URL:       {_base_url(cfg)}/mcp/{cfg.mcp_token}")
    print("  For ChatGPT, expose the backend through an HTTPS tunnel and use https://<tunnel-host>/mcp/<MCP token>.")


def cmd_token(args: argparse.Namespace) -> int:
    _print_connection(_cfg(strict=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    from applypilot.api import create_app
    from applypilot.logsetup import setup_logging

    cfg = _cfg(strict=True)
    if not cfg.token:
        raise SystemExit("APPLYPILOT_TOKEN missing — run `applypilot setup` first.")
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
        raise SystemExit(f"Refusing to bind {cfg.host}: use 127.0.0.1 and a tunnel, or pass --allow-remote.")
    cfg.paths.ensure()
    setup_logging(cfg.paths.logs_dir, args.log_level or cfg.settings.logging.level,
                  max_bytes=cfg.settings.logging.file_max_bytes, backups=cfg.settings.logging.file_backups)
    if args.pair:
        os.environ["APPLYPILOT_PAIRING_SECONDS"] = "300"
        print("Pairing open for 5 minutes: open Firefox — the ApplyPilot extension connects automatically.")
    app = create_app(cfg)
    print(f"ApplyPilot {__version__} on {_base_url(cfg)}  (dashboard: {_base_url(cfg)}/ui/dashboard.html#token=…)")
    readiness = cfg.readiness()
    if not readiness["ready"]:
        print("Configuration incomplete (autopilot will refuse to start): " + ", ".join(readiness["missing"]))
    server = TwoStageServer(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="warning",
                                           ws_ping_interval=20, ws_ping_timeout=30), app.state.engine)
    app.state.shutdown_callback = server.request_shutdown
    asyncio.run(server.serve())
    return 0


class TwoStageServer:
    """Small Uvicorn wrapper implementing persistent stop, then process shutdown."""

    def __init__(self, config: Any, engine: Any):
        import uvicorn
        self.server = uvicorn.Server(config)
        self.engine = engine
        self._interrupts = 0

    def request_shutdown(self) -> None:
        self.engine.db.meta_set("control", "STOPPED")
        self.engine.wake()
        self.server.should_exit = True

    def handle_signal(self, sig: int, _frame: Any = None) -> None:
        if sig == signal.SIGTERM:
            self.request_shutdown()
            return
        self._interrupts += 1
        if self._interrupts == 1:
            self.engine.db.meta_set("control", "STOPPED")
            self.engine.wake()
            print("\nAutopilot stopped. Backend is still running. Press Ctrl+C again to shut down the backend.",
                  flush=True)
        else:
            self.server.should_exit = True

    async def serve(self) -> None:
        # Uvicorn's default first-SIGINT behavior exits the server. Replace only
        # its signal-capture context; Uvicorn still performs normal lifespan shutdown.
        @contextlib.contextmanager
        def capture_signals():
            old = {sig: signal.signal(sig, self.handle_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                yield
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
        self.server.capture_signals = capture_signals  # type: ignore[method-assign]
        await self.server.serve()


def cmd_pair(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    _api(cfg, "POST", "/api/pairing/open", {"seconds": 120})
    print("Pairing open for 2 minutes: in Firefox click the ApplyPilot toolbar button (or restart Firefox).")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from applypilot.mcp_server import build_stdio_proxy

    cfg = _cfg(strict=False)
    if not cfg.token:
        raise SystemExit("APPLYPILOT_TOKEN missing — run `applypilot setup` first.")
    build_stdio_proxy(_base_url(cfg), cfg.token).run("stdio")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import httpx

    cfg = _cfg(strict=False)
    ok = True
    print(f"ApplyPilot {__version__}   root: {cfg.paths.root}")
    ready = cfg.readiness()
    print(f"[{'ok' if ready['ready'] else '!!'}] configuration " + ("complete" if ready["ready"] else "incomplete"))
    for m in ready["missing"]:
        print(f"      missing: {m}")
    for w in ready["warnings"]:
        print(f"      warning: {w}")
    ok &= ready["ready"]
    print(f"[{'ok' if ready['resumes'] else '!!'}] resumes: {', '.join(ready['resumes']) or 'none'}")
    print(f"[{'ok' if cfg.token else '!!'}] APPLYPILOT_TOKEN {'set' if cfg.token else 'missing (run setup)'}")
    try:
        from applypilot.db import Database

        db = Database(cfg.paths.db_path)
        db.initialize()
        print(f"[ok] database {cfg.paths.db_path} (schema v{db.schema_version()}, {db.job_counts()['total']} jobs)")
    except Exception as exc:
        ok = False
        print(f"[!!] database: {exc}")
    try:
        r = httpx.get(_base_url(cfg) + "/api/health", timeout=3)
        print(f"[ok] backend running ({r.json().get('version')})")
        st = httpx.get(_base_url(cfg) + "/api/status", headers={"X-ApplyPilot-Token": cfg.token}, timeout=5).json()
        print(f"[{'ok' if st['bridge']['connected'] else '!!'}] Firefox extension "
              f"{'connected' if st['bridge']['connected'] else 'NOT connected'}")
    except httpx.HTTPError:
        print("[--] backend not running (start with `applypilot run`)")
    from applypilot.accounts import email_provider

    print(f"[{'ok' if email_provider().configured() else '--'}] email verification provider: {email_provider().name}")
    print(f"[--] auto_submit: {cfg.settings.automation.auto_submit}   browser workers: "
          f"{cfg.settings.automation.max_concurrent_applications}   reasoning: {cfg.settings.reasoning.provider}")
    return 0 if ok else 1


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    try:
        print(json.dumps(_api(cfg, "POST", "/api/sync", {"mode": args.mode}), indent=2))
        return 0
    except SystemExit as exc:
        if "not reachable" not in str(exc):
            raise
    # Backend not running: sync directly against the database.
    from applypilot.bridge import BrowserBridge
    from applypilot.db import Database
    from applypilot.engine.scheduler import Engine

    db = Database(cfg.paths.db_path)
    db.initialize()
    engine = Engine(cfg, db, BrowserBridge())
    print(json.dumps(asyncio.run(engine.sync(args.mode)), indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    st = _api(cfg, "GET", "/api/status")
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"AUTOPILOT: {st['control']} ({st['mode']})   queue: {st['queue_remaining']}   "
          f"pending Astra items: {st['pending_reasoning']}   Firefox: {'connected' if st['bridge']['connected'] else 'DISCONNECTED'}")
    t = st["today"]
    print(f"TODAY: {t['submitted']} submitted, {t['skipped']} skipped, {t['ineligible']} ineligible, "
          f"{t['needs_user']} need attention, {t['failed']} failed, {t['active']} active")
    for w in st["workers"]:
        if w.get("status") == "busy":
            print(f"  {w['worker_id']:<10} {w.get('company')} — {w.get('title')}: {w.get('activity')}")
    for n in st["needs_user"]:
        print(f"  !! {n['application_id']} {n['company']} — {n['title']}: [{n['kind']}] {n['reason']}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    if args.command == "start":
        res = _api(cfg, "POST", "/api/rpc/start_autopilot", {"mode": args.mode, "refresh_jobs": not args.no_sync}, timeout=180)
    else:
        res = _api(cfg, "POST", "/api/control", {"action": args.command})
    print(json.dumps(res.get("status", res) if isinstance(res, dict) else res, indent=2, default=str)[:4000])
    return 0


def cmd_apply_url(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    print(json.dumps(_api(cfg, "POST", "/api/manual", {"url": args.url, "company": args.company, "title": args.title}), indent=2))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    log = _api(cfg, "POST", "/api/rpc/get_application_log", {"application_id": args.application_id})
    s = log["summary"]
    print(f"{s['company']} — {s['title']}  [{s['state']}]  {s.get('state_reason') or ''}")
    print(f"Eligibility: {log['eligibility']}")
    print(f"Resume: {log['resume']}")
    for a in log["answers"]:
        print(f"\nQ: {a['question']}\nA: {a['value']}\n   method={a['method']} class={a['qclass']} source={json.dumps(a['source'])}")
    print("\nTimeline:")
    for line in log["timeline"]:
        print("  " + line)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    dest = Path(args.dest or cfg.paths.data_dir / "backups" / f"applypilot-{time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(cfg.paths.db_path)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    src.close()
    out.close()
    print(f"Backup written to {dest}")
    return 0


def cmd_test_one(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    browser_mode = "headless" if getattr(args, "headless", False) else (
        "headed" if getattr(args, "headed", False) else None)
    watch = bool(getattr(args, "watch", False))
    slow_mo = getattr(args, "slow_mo", None)
    result = _api(cfg, "POST", "/api/test-one", {
        "job_id": args.job_id, "dry_run": args.dry_run, "browser_mode": browser_mode,
        "watch": watch, "slow_mo_ms": slow_mo,
    })
    if "selection_status" not in result:
        raise SystemExit(
            "The running ApplyPilot backend is using an older test-one implementation. "
            "No dry-run changes were made. Shut it down with `applypilot shutdown` (or press Ctrl+C twice "
            "in its terminal), restart it with `applypilot run`, and retry."
        )
    print(f"Job:     {result['job_id']}")
    print(f"Company: {result['company']}")
    print(f"Title:   {result['title']}")
    print(f"URL:     {result['source_url']}")
    print(f"State:   {result.get('application_state') or 'not queued'}")
    print(f"Browser: {result.get('browser_mode', browser_mode or 'headed')}"
          f"{'  watch=' + str(bool(result.get('watch'))) if watch else ''}"
          f"  slow-mo={result.get('slow_mo_ms', slow_mo or 0)}ms")
    if result["selection_status"] == "new":
        print("Selection: NEW — no application record exists for this job.")
    else:
        print(f"Selection: EXISTING — application {result['application_id']} already exists.")
    if args.dry_run:
        print("DRY RUN: no database state was changed and no external network request was made.")
        return 0
    app_id = result["application_id"]
    browser = result.get("browser") or {}
    tab = browser.get("tab") or {}
    if tab:
        print(f"Controlled tab: window={tab.get('window_id')} tab={tab.get('tab_id')} "
              f"url={tab.get('url') or 'unknown'} focus_confirmed={((browser.get('focus') or {}).get('confirmed'))}")
    print(f"Application: {app_id}\nRunning only this application; Ctrl+C here does not stop the backend.")
    parked = {"INELIGIBLE", "SUBMITTED", "SUBMISSION_UNCONFIRMED", "FAILED", "SKIPPED",
              "WAITING_FOR_CAPTCHA", "NEEDS_USER", "READY_TO_SUBMIT"}
    try:
        event_cursor = 0
        last_watch_status: tuple[Any, ...] | None = None
        while True:
            detail = _api(cfg, "GET", f"/api/applications/{app_id}")
            summary = detail["summary"]
            state, blocked = summary["state"], summary.get("blocked_on")
            if watch:
                runtime = _api(cfg, "GET", "/api/status")
                bridge_connected = bool((runtime.get("bridge") or {}).get("connected"))
                worker: dict[str, Any] = next((w for w in runtime.get("workers") or []
                                              if w.get("application_id") == app_id), {})
                status_key = (state, summary.get("worker_id"), summary.get("window_id"), summary.get("tab_id"),
                              summary.get("current_url"), blocked, bridge_connected, worker.get("activity"))
                if status_key != last_watch_status:
                    print("WATCH state={} worker={} bridge={} window={} tab={} url={} action={} waiting={}".format(
                        state, summary.get("worker_id") or "idle", bridge_connected, summary.get("window_id"),
                        summary.get("tab_id"), summary.get("current_url") or "—", worker.get("activity") or "idle",
                        blocked or "no"), flush=True)
                    last_watch_status = status_key
                evs = _api(cfg, "GET", f"/api/applications/{app_id}/events?after_id={event_cursor}&limit=500")["events"]
                if event_cursor == 0 and len(evs) > 25:
                    evs = evs[-25:]
                for event in evs:
                    event_cursor = max(event_cursor, int(event["id"]))
                    data = event.get("data") or {}
                    context = []
                    # Keep watch diagnostics structural: enough to follow reasoning
                    # attempts and recovery, without printing model text or field values.
                    for key in ("page_kind", "title", "tab_id", "window_id", "url", "action", "fields",
                                "challenge_type", "items", "attempt", "max_attempts", "decision",
                                "response_shape"):
                        if data.get(key) not in (None, "", []):
                            context.append(f"{key}={data[key]}")
                    print(f"WATCH #{event['id']} [{event['component']}/{event['event_type']}] "
                          f"{event['message']}" + (" | " + " ".join(context) if context else ""), flush=True)
            if watch and (state in {"WAITING_FOR_CAPTCHA", "NEEDS_USER"} or blocked == "reasoning"):
                time.sleep(1)
                continue
            if state in parked or blocked:
                _api(cfg, "POST", "/api/control", {"action": "stop"})
                reason = summary.get("state_reason") or (f"waiting on {blocked}" if blocked else "pipeline stopped")
                print(f"Result: {state} — {reason}")
                if state in {"WAITING_FOR_CAPTCHA", "NEEDS_USER", "READY_TO_SUBMIT"} or blocked:
                    print("User action is required. The durable single-job scope remains active.")
                return 0 if state not in {"FAILED", "SUBMISSION_UNCONFIRMED"} else 1
            time.sleep(1)
    except KeyboardInterrupt:
        _api(cfg, "POST", "/api/control", {"action": "stop"})
        print("\nSingle-job run stopped; its durable scope remains active.")
        return 130


def cmd_shutdown(args: argparse.Namespace) -> int:
    cfg = _cfg(strict=False)
    print(json.dumps(_api(cfg, "POST", "/api/backend/shutdown"), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="applypilot", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="create .env tokens, settings.yaml, database")
    p.add_argument("--rotate", action="store_true", help="generate new tokens")
    p.set_defaults(func=cmd_setup)
    sub.add_parser("token", help="print extension/MCP connection details").set_defaults(func=cmd_token)
    p = sub.add_parser("run", help="start backend + workers + MCP endpoint")
    p.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--allow-remote", action="store_true")
    p.add_argument("--pair", action="store_true", help="let the Firefox extension fetch the token for 5 minutes")
    p.set_defaults(func=cmd_run)
    p = sub.add_parser("pair", help="open a 2-minute pairing window on the running backend")
    p.set_defaults(func=cmd_pair)
    sub.add_parser("mcp", help="stdio MCP server (proxies to the running backend)").set_defaults(func=cmd_mcp)
    sub.add_parser("doctor", help="diagnose configuration and connectivity").set_defaults(func=cmd_doctor)
    p = sub.add_parser("sync", help="ingest SimplifyJobs listings and queue jobs")
    p.add_argument("--mode", choices=["backlog", "new_jobs"], default="backlog")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("status", help="show autopilot status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("start", help="start autopilot")
    p.add_argument("--mode", choices=["backlog", "new_jobs", "continuous"], default="backlog")
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_control)
    for name in ("pause", "resume", "stop"):
        sub.add_parser(name, help=f"{name} autopilot").set_defaults(func=cmd_control)
    p = sub.add_parser("apply-url", help="queue one specific posting")
    p.add_argument("url")
    p.add_argument("--company")
    p.add_argument("--title")
    p.set_defaults(func=cmd_apply_url)
    p = sub.add_parser("show", help="print how one application was handled")
    p.add_argument("application_id")
    p.set_defaults(func=cmd_show)
    p = sub.add_parser("backup", help="consistent online backup of the database")
    p.add_argument("--dest")
    p.set_defaults(func=cmd_backup)
    p = sub.add_parser("test-one", help="run exactly one already-imported job without synchronization")
    p.add_argument("--job-id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-sync", action="store_true", help="accepted for clarity; test-one is always offline")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--headed", action="store_true", help="use and focus the connected visible Firefox session (default)")
    modes.add_argument("--headless", action="store_true", help="request headless mode (unsupported by extension backend)")
    p.add_argument("--watch", action="store_true", help="stream application states and audit events")
    p.add_argument("--slow-mo", type=int, metavar="MS", help="delay actual browser actions by 0-10000 milliseconds")
    p.set_defaults(func=cmd_test_one)
    sub.add_parser("shutdown", help="gracefully stop the backend process").set_defaults(func=cmd_shutdown)
    args = parser.parse_args(argv)
    os.environ.setdefault("PYTHONUTF8", "1")
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
