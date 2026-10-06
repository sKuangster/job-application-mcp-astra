"""Structured JSON logging with application/worker context and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any

from applypilot.security import redact

ctx_application: contextvars.ContextVar[str | None] = contextvars.ContextVar("application_id", default=None)
ctx_job: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)
ctx_worker: contextvars.ContextVar[str | None] = contextvars.ContextVar("worker_id", default=None)

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime"}


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.application_id = getattr(record, "application_id", None) or ctx_application.get()
        record.job_id = getattr(record, "job_id", None) or ctx_job.get()
        record.worker_id = getattr(record, "worker_id", None) or ctx_worker.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "component": record.name.removeprefix("applypilot."),
            "message": record.getMessage(),
            "application_id": getattr(record, "application_id", None),
            "job_id": getattr(record, "job_id", None),
            "worker_id": getattr(record, "worker_id", None),
        }
        extra = {k: v for k, v in record.__dict__.items() if k not in _STD and k not in payload}
        if extra:
            payload["data"] = redact(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = " ".join(f"{k}={getattr(record, k)}" for k in ("worker_id", "application_id") if getattr(record, k, None))
        base = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:<7} " \
               f"{record.name.removeprefix('applypilot.'):<12} {record.getMessage()}"
        if ctx:
            base += f"  [{ctx}]"
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(logs_dir: Path | None, level: str = "INFO", *, max_bytes: int = 20_000_000, backups: int = 5,
                  console: bool = True, stream: Any = None) -> None:
    root = logging.getLogger("applypilot")
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    filt = ContextFilter()
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(logs_dir / "applypilot.jsonl", maxBytes=max_bytes,
                                                  backupCount=backups, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        fh.addFilter(filt)
        root.addHandler(fh)
        eh = logging.handlers.RotatingFileHandler(logs_dir / "errors.jsonl", maxBytes=max_bytes // 4,
                                                  backupCount=backups, encoding="utf-8")
        eh.setLevel(logging.WARNING)
        eh.setFormatter(JsonFormatter())
        eh.addFilter(filt)
        root.addHandler(eh)
    if console:
        ch = logging.StreamHandler(stream or sys.stderr)
        ch.setFormatter(ConsoleFormatter())
        ch.addFilter(filt)
        root.addHandler(ch)
    root.propagate = False


def tail_logs(logs_dir: Path, *, limit: int = 200, level: str | None = None, component: str | None = None,
              application_id: str | None = None) -> list[dict[str, Any]]:
    path = logs_dir / "applypilot.jsonl"
    if not path.exists():
        return []
    levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    floor = levels.get((level or "DEBUG").upper(), 10)
    out: list[dict[str, Any]] = []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 4_000_000))
        lines = fh.read().decode("utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if levels.get(rec.get("level", "INFO"), 20) < floor:
            continue
        if component and not str(rec.get("component", "")).startswith(component):
            continue
        if application_id and rec.get("application_id") != application_id:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return list(reversed(out))
