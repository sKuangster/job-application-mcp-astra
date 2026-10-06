"""Authenticated command broker; the extension is the only browser executor."""
from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect


class BridgeUnavailable(RuntimeError):
    pass


class BrowserActionError(RuntimeError):
    pass


class BrowserBridge:
    def __init__(self, token: str, timeout: float = 45):
        self.token = token
        self.timeout = timeout
        self.socket: WebSocket | None = None
        self.client_id: str | None = None
        self.pending: dict[str, tuple[asyncio.Future, dict]] = {}
        self.ownership: dict[str, dict] = {}
        self.connected_at: float | None = None
        self.commands = 0
        self.elapsed = 0.0
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.socket is not None

    async def connect(self, socket: WebSocket) -> None:
        origin = socket.headers.get("origin", "")
        if origin and not origin.startswith("moz-extension://"):
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            hello = await asyncio.wait_for(socket.receive_json(), 5)
            if hello.get("type") != "hello" or not secrets.compare_digest(
                str(hello.get("token", "")), self.token
            ):
                await socket.close(code=1008)
                return
            if self.socket is not None:
                await socket.close(code=1013, reason="A Firefox bridge is already connected")
                return
            self.socket = socket
            self.client_id = str(hello.get("client_id", "firefox"))
            self.ownership = hello.get("ownership", {})
            self.connected_at = time.time()
            await socket.send_json({"type": "welcome", "protocol": 1})
            while True:
                message = await socket.receive_json()
                if message.get("type") == "ping":
                    await socket.send_json({"type": "pong"})
                    continue
                pair = self.pending.get(message.get("id"))
                if pair is None:
                    continue
                future, command = pair
                if any(message.get(k) != command[k] for k in
                       ("application_id", "worker_id", "ownership_token")):
                    if not future.done():
                        future.set_exception(BrowserActionError("Browser response ownership mismatch"))
                    continue
                if not future.done():
                    if message.get("ok"):
                        future.set_result(message.get("result") or {})
                    else:
                        # Never forward raw exception payloads (which could include passwords).
                        error = str(message.get("error", "Browser operation failed"))[:500]
                        future.set_exception(BrowserActionError(error))
        except (WebSocketDisconnect, TimeoutError, ValueError):
            pass
        finally:
            if self.socket is socket:
                self.socket = None
                self.client_id = None
                for future, _ in self.pending.values():
                    if not future.done():
                        future.set_exception(BridgeUnavailable("Firefox disconnected; inspect before retry"))

    async def call(self, application: dict, action: str, payload: dict | None = None,
                   *, command_id: str | None = None) -> dict[str, Any]:
        if self.socket is None:
            raise BridgeUnavailable("Firefox extension is not connected")
        command = {
            "id": command_id or str(uuid4()),
            "application_id": application["id"],
            "worker_id": application["worker_id"],
            "ownership_token": application["lease_token"],
            "action": action,
            "payload": payload or {},
            "deadline": int((time.time() + self.timeout) * 1000),
        }
        future = asyncio.get_running_loop().create_future()
        self.pending[command["id"]] = (future, command)
        started = time.monotonic()
        try:
            async with self._send_lock:
                socket = self.socket
                if socket is None:
                    raise BridgeUnavailable("Firefox disconnected")
                await socket.send_json(command)
            self.commands += 1
            return await asyncio.wait_for(future, self.timeout)
        except TimeoutError as exc:
            raise BridgeUnavailable("Firefox command timed out; action outcome may be unknown") from exc
        finally:
            self.pending.pop(command["id"], None)
            self.elapsed += time.monotonic() - started
