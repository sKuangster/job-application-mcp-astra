"""Opt-in Windows UI Automation attention fallback for an explicitly owned window.

Only focus and read-only structural inspection are permitted. Never invoke a
control, synthesize input, read field values or passwords, or click Submit.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import PureWindowsPath
from typing import Any

from applypilot.config import WindowsNativeSettings


def _connect(handle: int) -> tuple[Any, str]:
    import psutil
    from pywinauto import Desktop

    window = Desktop(backend="uia").window(handle=handle)
    window.wait("exists visible", timeout=3)
    executable = psutil.Process(window.process_id()).exe()
    return window, executable


class WindowsAttentionFallback:
    def __init__(self, settings: WindowsNativeSettings, *, connector: Any = None, platform: str | None = None):
        self.settings = settings
        self.connector = connector or _connect
        self.platform = platform or sys.platform

    def inspect_or_focus(self, ownership: dict[str, Any], *, focus: bool = False) -> dict[str, Any]:
        if not self.settings.enabled or self.platform != "win32":
            raise ValueError("Windows UI Automation fallback is disabled or unavailable")
        if not ownership.get("tab_id") or not ownership.get("window_id"):
            raise ValueError("extension ownership is required")
        binding = self.settings.bindings.get(str(ownership["window_id"]))
        if not binding:
            raise ValueError("no explicit native binding for the owned Firefox window")
        window, executable = self.connector(binding.handle)
        if PureWindowsPath(executable).name.casefold() != "firefox.exe":
            raise ValueError("native target is not Firefox")
        if window.window_text() != binding.title or window.handle != binding.handle:
            raise ValueError("native window identity changed; rebind it explicitly")
        # Exclude names and values: accessible text can contain credentials or PII.
        info = {"window_id": ownership["window_id"], "handle": binding.handle,
                "control_type": window.element_info.control_type, "focused": False}
        if focus:
            window.set_focus()
            info["focused"] = bool(window.has_keyboard_focus())
        return info

    async def focus(self, ownership: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.wait_for(asyncio.to_thread(self.inspect_or_focus, ownership, focus=True), timeout=5)
