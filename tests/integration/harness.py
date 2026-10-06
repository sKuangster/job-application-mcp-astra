"""End-to-end harness: real backend + real extension code (jsdom fake browser) + mock ATS sites."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "ats"


def node_bin() -> str | None:
    for candidate in (shutil.which("node"), str(Path.home() / ".local/node/bin/node")):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FixtureServer:
    """Serves tests/fixtures/ats and records POST /__fixture/submit (exactly-once assertions)."""

    def __init__(self) -> None:
        self.submissions: list[dict[str, Any]] = []
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a: Any, **kw: Any) -> None:
                super().__init__(*a, directory=str(FIXTURES), **kw)

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                if self.path.startswith("/__fixture/submit"):
                    length = int(self.headers.get("content-length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    outer.submissions.append(body)
                    payload = b'{"ok":true}'
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_error(404)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/__fixture/submissions"):
                    payload = json.dumps(outer.submissions).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                super().do_GET()

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"

    def __enter__(self) -> FixtureServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()

    def by_fixture(self, name: str) -> list[dict[str, Any]]:
        return [s for s in self.submissions if s.get("fixture") == name]


class Backend:
    """Runs the full ASGI app (engine + bridge + MCP) under uvicorn in a background thread."""

    def __init__(self, app: Any, port: int):
        import uvicorn

        self.port = port
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Backend:
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("backend did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


class FakeBrowser:
    def __init__(self, port: int, token: str, log: bool = False):
        node = node_bin()
        if node is None:
            raise RuntimeError("node not available")
        args = [node, str(ROOT / "tests/js/fake_browser.cjs"), "--url", f"ws://127.0.0.1:{port}/bridge", "--token", token]
        if log or os.environ.get("APPLYPILOT_FAKE_BROWSER_LOG"):
            args.append("--log")
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=None, text=True, cwd=str(ROOT))

    def __enter__(self) -> FakeBrowser:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if "FAKE_BROWSER_READY" not in line:
            raise RuntimeError(f"fake browser failed: {line!r}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=10)
        if self.proc.poll() is None:
            self.proc.kill()


def run_coro(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 60) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


class SimulatedAstra:
    """Answers reasoning batches the way Astra would, deterministically, for tests."""

    def __init__(self) -> None:
        self.batches: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []

    def answer(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        self.batches.append(batch)
        out = []
        for item in batch["items"]:
            kind = item["kind"]
            if kind == "eligibility":
                out.append({"item_id": item["item_id"], "eligible": True, "reason": "within window"})
                continue
            if kind == "page":
                out.append({"item_id": item["item_id"], "needs_user": "test: unknown page"})
                continue
            options = item.get("options") or []
            ftype = item.get("field_type")
            q = (item.get("question") or "").lower()
            known = (item.get("hints") or {}).get("known_value")
            if ftype == "checkbox_group":
                value: Any = [options[0]]
            elif options:
                if known is not None and isinstance(known[0], bool):
                    pool = [o for o in options if o.lower().startswith("yes" if known[0] else "no")]
                    value = pool[0] if pool else options[0]
                elif any(o.lower() == "yes" for o in options):
                    value = next(o for o in options if o.lower() == ("no" if "sponsor" in q else "yes"))
                else:
                    value = options[0]
            elif ftype == "checkbox":
                value = True
            else:
                limits = item.get("limits") or {}
                text = ("I built RoboSort, a Python robot-arm sorting simulator, and want to apply that motion-planning "
                        "experience to real robots on your team.")
                if limits.get("max_chars"):
                    text = text[: limits["max_chars"]]
                value = text
            ans = {"item_id": item["item_id"], "value": value, "fact_paths": [], "confidence": 0.9,
                   "template": item.get("template")}
            out.append(ans)
            self.answered.append({**ans, "question": item.get("question")})
        return out
