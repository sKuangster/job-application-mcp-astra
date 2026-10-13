"""Backend side of the Firefox bridge (docs/PROTOCOL.md §2).

The extension's background page holds one authenticated WebSocket to the backend.
Commands carry the application id; the extension routes them only to the tab that
application owns, so a worker can never act on another application's tab. Every
command has a timeout; a disconnect fails in-flight commands with ``BridgeUnavailable``
(recoverable — workers back off and resume after reconnect).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger("applypilot.bridge")

RECOVERABLE = {"TIMEOUT", "ELEMENT_NOT_FOUND", "CONTENT_SCRIPT_UNAVAILABLE", "NAVIGATION_FAILED", "VERIFY_FAILED",
               "NO_OPTION_MATCH", "INTERNAL", "DISABLED"}
TAB_GONE = {"NO_OWNED_TAB", "TAB_CLOSED"}


class BridgeUnavailable(RuntimeError):
    """No extension connected (or it disconnected mid-command)."""


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = details

    @property
    def recoverable(self) -> bool:
        return self.code in RECOVERABLE


EventHandler = Callable[[str, str | None, dict[str, Any]], Awaitable[None] | None]


class BrowserBridge:
    def __init__(self, *, default_timeout: float = 30):
        self.default_timeout = default_timeout
        self._ws: WebSocket | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._connected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self.owned: dict[str, dict[str, Any]] = {}
        self.info: dict[str, Any] = {}
        self.last_seen = 0.0
        self.connected_at = 0.0
        self.handlers: list[EventHandler] = []
        self.stale_after = 40.0  # seconds without traffic after which a connection may be replaced
        self.metrics = {"commands": 0, "errors": 0, "timeouts": 0, "reconnects": 0, "total_ms": 0}

    @property
    def connected(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "last_seen": self.last_seen, "connected_at": self.connected_at,
                "owned_tabs": len(self.owned), "browser": self.info.get("browser"), "version": self.info.get("version")}

    async def wait_connected(self, timeout: float) -> bool:
        if self.connected:
            return True
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except TimeoutError:
            return False

    # ------------------------------------------------------------------ connection
    async def serve(self, ws: WebSocket) -> None:
        """Run one extension connection (called by the API's websocket route)."""
        previous = self._ws
        if previous is not None:
            if time.time() - self.last_seen < self.stale_after:
                # A healthy browser is already connected (e.g. the extension is paired in two Firefox
                # profiles). Refuse the newcomer instead of ping-ponging and aborting commands.
                log.warning("refused a second browser connection while one is active")
                with contextlib.suppress(Exception):
                    await ws.close(code=4409, reason="another browser is already connected")
                return
            with contextlib.suppress(Exception):
                await previous.close(code=4000, reason="replaced by a newer connection")
            self._fail_pending("bridge connection replaced")
        self._ws = ws
        self._connected.clear()
        self.info = {}
        self.metrics["reconnects"] += 1
        self.connected_at = self.last_seen = time.time()
        log.info("Firefox transport connected; waiting for ownership handshake")
        pinger = asyncio.create_task(self._ping_loop(ws))
        try:
            while True:
                raw = await ws.receive_text()
                self.last_seen = time.time()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self._handle(msg)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # pragma: no cover - transport errors
            log.warning("bridge connection error: %s", exc)
        finally:
            pinger.cancel()
            if self._ws is ws:
                self._ws = None
                self._connected.clear()
                self._fail_pending("Firefox extension disconnected")
                log.warning("Firefox extension disconnected")

    async def _ping_loop(self, ws: WebSocket) -> None:
        while True:
            await asyncio.sleep(15)
            with contextlib.suppress(Exception):
                await self._send({"type": "ping", "ts": time.time()})

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(BridgeUnavailable(reason))
        self._pending.clear()

    async def _send(self, msg: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise BridgeUnavailable("Firefox extension is not connected")
        async with self._send_lock:
            await ws.send_text(json.dumps(msg, default=str))

    async def _handle(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "result":
            fut = self._pending.pop(str(msg.get("id")), None)
            if fut and not fut.done():
                if msg.get("ok"):
                    fut.set_result(msg.get("result"))
                else:
                    err = msg.get("error") or {}
                    fut.set_exception(BrowserError(str(err.get("code") or "INTERNAL"), str(err.get("message") or "error"),
                                                   err.get("details")))
        elif mtype == "hello":
            if not isinstance(msg.get("browser"), str) or not msg["browser"] or not isinstance(msg.get("owned"), list):
                raise ValueError("invalid Firefox ownership handshake")
            self.info = {"version": msg.get("version"), "browser": msg.get("browser")}
            self.owned = {str(o["app_id"]): o for o in msg.get("owned") or [] if o.get("app_id")}
            self._connected.set()
            log.info("Firefox ownership handshake complete")
            await self._emit("hello", None, {"owned": list(self.owned)})
        elif mtype == "event":
            app_id = msg.get("app_id")
            event = str(msg.get("event") or "")
            if event == "tab_closed" and app_id:
                self.owned.pop(str(app_id), None)
            elif event == "tab_navigated" and app_id and str(app_id) in self.owned:
                self.owned[str(app_id)].update({k: v for k, v in (msg.get("data") or {}).items()
                                                if k in {"url", "tab_id", "window_id"} and v is not None})
            await self._emit(event, app_id, msg.get("data") or {})
        elif mtype == "ping":
            await self._send({"type": "pong", "ts": time.time()})

    async def _emit(self, event: str, app_id: str | None, data: dict[str, Any]) -> None:
        for handler in self.handlers:
            try:
                res = handler(event, app_id, data)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:  # pragma: no cover
                log.exception("bridge event handler failed")

    # ------------------------------------------------------------------ commands
    async def call(self, app_id: str | None, action: str, payload: dict[str, Any] | None = None, *,
                   timeout: float | None = None) -> Any:
        if not self.connected:
            raise BridgeUnavailable("Firefox extension is not connected")
        timeout = timeout or self.default_timeout
        cmd_id = uuid.uuid4().hex
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        started = time.monotonic()
        self.metrics["commands"] += 1
        try:
            await self._send({"type": "command", "id": cmd_id, "app_id": app_id, "action": action,
                              "payload": payload or {}, "timeout_ms": int(timeout * 1000)})
            # The extension enforces timeout_ms itself; allow a little slack for transport.
            result = await asyncio.wait_for(fut, timeout + 5)
            if action == "open" and app_id and isinstance(result, dict):
                self.owned[app_id] = {"app_id": app_id, **{k: result.get(k) for k in ("tab_id", "window_id", "url")}}
            if action == "close" and app_id:
                self.owned.pop(app_id, None)
            return result
        except TimeoutError as exc:
            self.metrics["timeouts"] += 1
            raise BrowserError("TIMEOUT", f"{action} timed out after {timeout:.0f}s") from exc
        except BrowserError as exc:
            self.metrics["errors"] += 1
            if exc.code in TAB_GONE and app_id:
                self.owned.pop(app_id, None)
            raise
        finally:
            self._pending.pop(cmd_id, None)
            self.metrics["total_ms"] += int((time.monotonic() - started) * 1000)
