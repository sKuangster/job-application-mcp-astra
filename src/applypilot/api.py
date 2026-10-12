"""HTTP API + bridge WebSocket + MCP (streamable HTTP) in one local server."""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from applypilot import __version__
from applypilot.bridge import BridgeUnavailable, BrowserBridge, BrowserError
from applypilot.config import AppConfig
from applypilot.db import Database
from applypilot.engine.scheduler import Engine
from applypilot.security import token_matches
from applypilot.service import Service
from applypilot.state import InvalidTransition

log = logging.getLogger("applypilot.api")
UI_DIR = Path(__file__).resolve().parents[2] / "firefox" / "ui"


def _date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        try:
            return float(value)
        except ValueError:
            raise HTTPException(400, f"bad date {value!r}") from None


class MCPAuth:
    """Guards the MCP endpoint: bearer token header, or the capability path /mcp/<mcp_token>."""

    def __init__(self, app: ASGIApp, token: str, mcp_token: str):
        self.app, self.token, self.mcp_token = app, token, mcp_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        ok = auth.lower().startswith("bearer ") and (token_matches(auth[7:], self.token) or token_matches(auth[7:], self.mcp_token))
        rest = path.removeprefix("/mcp").strip("/")
        if not ok and self.mcp_token and rest and token_matches(rest.split("/")[0], self.mcp_token):
            ok = True
        if not ok:
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        scope = dict(scope)
        scope["path"] = "/mcp"
        scope["raw_path"] = b"/mcp"
        scope["root_path"] = ""
        await self.app(scope, receive, send)


class Dispatcher:
    """Top-level ASGI app: /mcp* → authenticated MCP transport, everything else → FastAPI."""

    def __init__(self, api: FastAPI, mcp: ASGIApp | None):
        self.api, self.mcp = api, mcp
        self.state = api.state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] in ("http", "websocket") and _proxied(scope) and not path.startswith("/mcp"):
            # Tunnelled (remote) traffic may only reach the token-protected MCP endpoint — never the
            # dashboard API, the browser bridge or extension pairing.
            if scope["type"] == "http":
                await JSONResponse({"detail": "only /mcp is reachable through a tunnel"}, status_code=403)(scope, receive, send)
            else:
                await send({"type": "websocket.close", "code": 4403})
            return
        if self.mcp is not None and scope["type"] == "http" and path.startswith("/mcp"):
            await self.mcp(scope, receive, send)
            return
        await self.api(scope, receive, send)


_PROXY_HEADERS = {b"x-forwarded-for", b"forwarded", b"x-real-ip", b"cf-connecting-ip", b"x-forwarded-host",
                  b"true-client-ip", b"ngrok-trace-id"}


def _proxied(scope: Scope) -> bool:
    return any(k.lower() in _PROXY_HEADERS for k, _ in scope.get("headers", []))


def create_app(cfg: AppConfig, *, db: Database | None = None, bridge: BrowserBridge | None = None,
               engine: Engine | None = None, start_engine: bool = True, mount_mcp: bool = True) -> Dispatcher:
    db = db or Database(cfg.paths.db_path)
    db.initialize()
    bridge = bridge or BrowserBridge(default_timeout=cfg.settings.browser.command_timeout)
    engine = engine or Engine(cfg, db, bridge)
    service = Service(engine)
    allow_no_origin = os.environ.get("APPLYPILOT_ALLOW_NO_ORIGIN") == "1"

    mcp_app = None
    mcp_server = None
    if mount_mcp:
        from applypilot.mcp_server import build_mcp

        mcp_server = build_mcp(service)
        mcp_app = mcp_server.streamable_http_app()

    if os.environ.get("APPLYPILOT_PAIRING_SECONDS"):
        db.meta_set("pairing_until", time.time() + float(os.environ["APPLYPILOT_PAIRING_SECONDS"]))

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            if mcp_server is not None:
                await stack.enter_async_context(mcp_server.session_manager.run())
            if start_engine:
                await engine.start()
            try:
                yield
            finally:
                if start_engine:
                    await engine.shutdown()

    app = FastAPI(title="ApplyPilot", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.engine = engine
    app.add_middleware(
        CORSMiddleware, allow_origin_regex=r"^(moz-extension://[0-9a-f-]+|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
        allow_methods=["GET", "POST"], allow_headers=["X-ApplyPilot-Token", "Authorization", "Content-Type"],
    )

    def auth(request: Request) -> None:
        token = request.headers.get("x-applypilot-token") or ""
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer "):
            token = token or bearer[7:]
        if not token_matches(token, cfg.token):
            raise HTTPException(401, "invalid or missing token")

    guarded = [Depends(auth)]

    @app.exception_handler(InvalidTransition)
    async def _invalid(_: Request, exc: InvalidTransition) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def _missing(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    @app.exception_handler(ValueError)
    async def _bad(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(BridgeUnavailable)
    async def _nobridge(_: Request, exc: BridgeUnavailable) -> JSONResponse:
        return JSONResponse({"detail": f"Firefox extension not connected: {exc}"}, status_code=503)

    @app.exception_handler(BrowserError)
    async def _browser(_: Request, exc: BrowserError) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=502)

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.post("/api/pair")
    async def pair(request: Request) -> JSONResponse:
        """One-click extension pairing. Returns the token only (a) while a pairing window is open
        (`applypilot run --pair` / `applypilot pair`) and (b) to an extension origin, which web pages
        cannot forge. Each window pairs one extension."""
        origin = request.headers.get("origin") or ""
        until = float(db.meta_get("pairing_until", 0) or 0)
        if not origin.startswith("moz-extension://") or time.time() > until:
            return JSONResponse({"detail": "pairing is not open"}, status_code=403)
        db.meta_set("pairing_until", 0)
        db.event(None, "security", "extension_paired", "Firefox extension paired", {"origin": origin})
        return JSONResponse({"token": cfg.token, "version": __version__})

    @app.post("/api/pairing/open", dependencies=guarded)
    async def pairing_open(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        seconds = min(600.0, float(body.get("seconds") or 120))
        db.meta_set("pairing_until", time.time() + seconds)
        return {"open_for_seconds": seconds}

    @app.get("/api/status", dependencies=guarded)
    async def status() -> dict[str, Any]:
        return service.status()

    @app.post("/api/control", dependencies=guarded)
    async def control(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return await service.control(str(body.get("action")), body.get("mode"))

    @app.post("/api/sync", dependencies=guarded)
    async def sync(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return await service.sync(body.get("mode") or "backlog")

    @app.post("/api/test-one", dependencies=guarded)
    async def test_one(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return await service.test_one(body.get("job_id"), dry_run=bool(body.get("dry_run")),
                                      browser_mode=body.get("browser_mode"), watch=bool(body.get("watch")),
                                      slow_mo_ms=body.get("slow_mo_ms"))

    @app.post("/api/backend/shutdown", dependencies=guarded)
    async def backend_shutdown() -> dict[str, Any]:
        callback = getattr(app.state, "shutdown_callback", None)
        if callback is None:
            raise HTTPException(503, "backend shutdown is not available in this server mode")
        import asyncio
        asyncio.get_running_loop().call_soon(callback)
        return {"shutting_down": True}

    @app.get("/api/applications", dependencies=guarded)
    async def applications(q: str | None = None, company: str | None = None, role: str | None = None,
                           state: str | None = None, ats: str | None = None, resume: str | None = None,
                           failure: bool = False, date_from: str | None = None, date_to: str | None = None,
                           limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        to = _date(date_to)
        if to and date_to and len(date_to) == 10:
            to += 86399
        return service.search(q=q, company=company, role=role, state=state, ats=ats, resume=resume, failure=failure,
                              date_from=_date(date_from), date_to=to, limit=limit, offset=offset)

    @app.get("/api/applications/{app_id}", dependencies=guarded)
    async def application(app_id: str) -> dict[str, Any]:
        return service.application(app_id)

    @app.get("/api/applications/{app_id}/events", dependencies=guarded)
    async def events(app_id: str, after_id: int = 0, limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
        return service.events(app_id, after_id, limit)

    @app.post("/api/applications/{app_id}/action", dependencies=guarded)
    async def app_action(app_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return await service.action(app_id, str(body.get("action")), body.get("note"))

    @app.post("/api/applications/{app_id}/answer", dependencies=guarded)
    async def app_answer(app_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return service.answer(app_id, str(body.get("field_key")), body.get("value"), bool(body.get("remember")))

    @app.get("/api/queue", dependencies=guarded)
    async def queue(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        return service.queue(limit)

    @app.get("/api/reasoning", dependencies=guarded)
    async def reasoning(status: str | None = None) -> dict[str, Any]:
        return service.reasoning_items(status)

    @app.get("/api/logs", dependencies=guarded)
    async def logs(level: str | None = None, component: str | None = None, application_id: str | None = None,
                   limit: int = Query(300, ge=1, le=5000)) -> dict[str, Any]:
        return service.logs(level=level, component=component, application_id=application_id, limit=limit)

    @app.get("/api/metrics", dependencies=guarded)
    async def metrics() -> dict[str, Any]:
        return service.metrics()

    @app.get("/api/config", dependencies=guarded)
    async def config() -> dict[str, Any]:
        return service.config_report()

    @app.post("/api/manual", dependencies=guarded)
    async def manual(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return engine.add_manual_job(str(body["url"]), company=body.get("company"), title=body.get("title"),
                                     location=body.get("location"))

    @app.post("/api/rpc/{operation}", dependencies=guarded)
    async def rpc(operation: str, body: dict[str, Any] = Body(default={})) -> Any:
        from applypilot.mcp_server import dispatch

        return await dispatch(service, operation, body)

    @app.websocket("/bridge")
    async def bridge_ws(ws: WebSocket) -> None:
        origin = ws.headers.get("origin") or ""
        if not token_matches(ws.query_params.get("token"), cfg.token):
            await ws.close(code=4401)
            return
        if not (origin.startswith("moz-extension://") or (not origin and allow_no_origin)):
            log.warning("rejected bridge connection from origin %r", origin)
            await ws.close(code=4403)
            return
        await ws.accept()
        await bridge.serve(ws)

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")

        @app.get("/")
        async def root() -> RedirectResponse:
            return RedirectResponse("/ui/dashboard.html")

    return Dispatcher(app, MCPAuth(mcp_app, cfg.token, cfg.mcp_token) if mcp_app is not None else None)
