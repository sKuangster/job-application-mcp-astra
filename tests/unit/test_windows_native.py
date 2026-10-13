from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from applypilot.config import NativeWindowBinding, WindowsNativeSettings
from applypilot.windows_native import WindowsAttentionFallback


def fallback(executable="C:\\Program Files\\Mozilla Firefox\\firefox.exe", title="Application — Mozilla Firefox"):
    window = Mock()
    window.handle = 123
    window.window_text.return_value = title
    window.has_keyboard_focus.return_value = True
    window.element_info = SimpleNamespace(control_type="Window")
    settings = WindowsNativeSettings(enabled=True, bindings={"7": NativeWindowBinding(handle=123, title="Application — Mozilla Firefox")})
    connector = Mock(return_value=(window, executable))
    return WindowsAttentionFallback(settings, connector=connector, platform="win32"), window, connector


async def test_bound_firefox_focus_only():
    native, window, _ = fallback()
    result = await native.focus({"window_id": 7, "tab_id": 8})
    assert result["focused"] is True
    window.set_focus.assert_called_once()
    window.descendants.assert_not_called()
    window.click_input.assert_not_called()
    window.type_keys.assert_not_called()


def test_inspection_never_reads_fields_or_names():
    native, window, _ = fallback()
    result = native.inspect_or_focus({"window_id": 7, "tab_id": 8})
    assert set(result) == {"window_id", "handle", "control_type", "focused"}
    window.set_focus.assert_not_called()
    window.descendants.assert_not_called()


@pytest.mark.parametrize("ownership", [{}, {"window_id": 7}, {"window_id": 9, "tab_id": 8}])
def test_unowned_or_unbound_window_rejected(ownership):
    native, _, connector = fallback()
    with pytest.raises(ValueError):
        native.inspect_or_focus(ownership, focus=True)
    connector.assert_not_called()


@pytest.mark.parametrize("exe,title", [("C:\\Windows\\notepad.exe", "Application — Mozilla Firefox"),
                                     ("C:\\Firefox\\firefox.exe", "Other application")])
def test_changed_identity_or_wrong_process_rejected(exe, title):
    native, window, _ = fallback(exe, title)
    with pytest.raises(ValueError):
        native.inspect_or_focus({"window_id": 7, "tab_id": 8}, focus=True)
    window.set_focus.assert_not_called()


def test_linux_and_disabled_settings_rejected():
    native, _, connector = fallback()
    native.platform = "linux"
    with pytest.raises(ValueError):
        native.inspect_or_focus({"window_id": 7, "tab_id": 8})
    native.platform = "win32"
    native.settings.enabled = False
    with pytest.raises(ValueError):
        native.inspect_or_focus({"window_id": 7, "tab_id": 8})
    connector.assert_not_called()


async def test_runner_uses_native_attention_fallback_after_extension_focus_failure(cfg, db, monkeypatch):
    from conftest import make_job

    from applypilot import windows_native
    from applypilot.bridge import BrowserBridge, BrowserError
    from applypilot.engine.runner import ApplicationRunner
    from applypilot.engine.scheduler import Engine

    app, _ = db.create_application(make_job(db), mode="backlog")
    db.update_application(app["id"], state="APPLYING")
    claimed = db.claim("browser-1", "browser", lease_seconds=60, app_id=app["id"])
    bridge = BrowserBridge()
    bridge.owned[app["id"]] = {"tab_id": 8, "window_id": 7}
    bridge.call = AsyncMock(side_effect=BrowserError("INTERNAL", "extension focus failed"))
    cfg.settings.browser.windows_native.enabled = True
    native_focus = AsyncMock(return_value={"focused": True, "handle": 123, "window_id": 7})
    monkeypatch.setattr(windows_native.WindowsAttentionFallback, "focus", native_focus)
    runner = ApplicationRunner(Engine(cfg, db, bridge), claimed, "browser-1")
    result = await runner.cmd("focus")
    assert result["focused"] is True
    native_focus.assert_awaited_once_with({"tab_id": 8, "window_id": 7})
    assert any(event["event_type"] == "native_attention_focus" for event in db.events(app["id"]))
