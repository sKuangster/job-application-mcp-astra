"""SQLite persistence with short transactions, durable leases and submission fencing.

Each operation opens its own connection, so a Database can be shared by asyncio
workers, threads and the MCP/HTTP processes. BEGIN IMMEDIATE makes claim, duplicate
resolution and submission intent creation atomic across all those processes.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .state import (
    ALLOWED_TRANSITIONS,
    PARKED_STATES,
    SUBMISSION_STATES,
    TERMINAL_STATES,
    InvalidTransition,
    LeaseLost,
    validate_transition,
)

T = TypeVar("T")
_TRACKING_KEYS = {"ref", "referrer", "source", "src", "gh_src", "lever-source", "fbclid", "gclid"}
_SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "cookies",
    "cookie",
}


def normalize_text(value: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def normalize_url(url: str) -> str:
    """Ignore tracking and cosmetic URL changes while preserving requisition IDs."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.lower().removeprefix("www.")
    port = parts.port
    if port and port not in {80, 443}:
        host += f":{port}"
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_KEYS
    )
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", urlencode(query), ""))


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).lower() in _SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: str | None, fallback: Any = None) -> Any:
    return json.loads(value) if value else fallback


class Database:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = (
            str(path)
            if str(path).startswith("file:") or str(path) == ":memory:"
            else str(Path(path).expanduser())
        )
        self.clock = clock
        self._keeper: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self.path = f"file:jobpilot-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._connect()
        else:
            self._uri = self.path.startswith("file:")
            if not self._uri:
                Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        for attempt in range(7):
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                result = operation(connection)
                connection.commit()
                return result
            except sqlite3.OperationalError as error:
                connection.rollback()
                if (
                    not any(word in str(error).lower() for word in ("locked", "busy"))
                    or attempt == 6
                ):
                    raise
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            time.sleep(min(0.5, 0.01 * 2**attempt) + random.uniform(0, 0.01))
        raise RuntimeError("Database retry exhausted")

    def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        connection = self._connect()
        try:
            return operation(connection)
        finally:
            connection.close()

    def initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        finally:
            connection.close()

        def migrate(connection: sqlite3.Connection) -> None:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            migrations = sorted(Path(__file__).with_name("migrations").glob("[0-9]*.sql"))
            known = {int(path.name.split("_", 1)[0]) for path in migrations}
            if applied - known:
                raise RuntimeError("Database schema is newer than this version of JobPilot")
            for path in migrations:
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                statement = ""
                for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise RuntimeError(f"Incomplete migration SQL: {path.name}")
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)", (version, self.clock())
                )

        self._write(migrate)

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _decode(result["metadata"], {})
        return result

    def _application(self, connection: sqlite3.Connection, application_id: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown application: {application_id}")
        result = dict(row)
        result["data"] = _decode(result["data"], {})
        result["job"] = self._job(
            connection.execute("SELECT * FROM jobs WHERE id=?", (result["job_id"],)).fetchone()
        )
        return result

    def _event(
        self,
        connection: sqlite3.Connection,
        application_id: str,
        event_type: str,
        data: Any,
        worker_id: str | None = None,
        component: str = "engine",
    ) -> int:
        row = connection.execute(
            "SELECT job_id, worker_id FROM applications WHERE id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown application: {application_id}")
        cursor = connection.execute(
            "INSERT INTO application_events(application_id,job_id,worker_id,timestamp,component,event_type,data) VALUES(?,?,?,?,?,?,?)",
            (
                application_id,
                row["job_id"],
                worker_id or row["worker_id"],
                self.clock(),
                component,
                event_type,
                _json(data),
            ),
        )
        return int(cursor.lastrowid)

    def _fence(
        self, row: sqlite3.Row | dict[str, Any], worker_id: str | None, lease_token: str | None
    ) -> None:
        if worker_id is None and lease_token is None:
            if row["worker_id"] is not None and (row["lease_until"] or 0) > self.clock():
                raise LeaseLost("Active application requires its worker ID and lease token")
            return
        if (
            not worker_id
            or not lease_token
            or row["worker_id"] != worker_id
            or row["lease_token"] != lease_token
            or (row["lease_until"] or 0) <= self.clock()
        ):
            raise LeaseLost("Application lease expired or belongs to another worker")

    @staticmethod
    def _aliases(job: dict[str, Any]) -> list[tuple[str, str]]:
        aliases: set[tuple[str, str]] = set()
        for field in ("simplify_url", "application_url"):
            url = normalize_url(str(job.get(field) or ""))
            if url:
                aliases.add(("url", url))
                parts = urlsplit(url)
                if parts.hostname in {"simplify.jobs", "simplify.com"}:
                    match = re.search(r"/(?:p|jobs|j)/([^/]+)", parts.path)
                    if match:
                        aliases.add(("simplify_id", match.group(1).casefold()))
        if job.get("simplify_id"):
            aliases.add(("simplify_id", str(job["simplify_id"]).casefold()))
        for field in ("ats_id", "requisition_id"):
            if job.get(field):
                aliases.add(
                    (
                        field,
                        normalize_text(str(job.get("company", "")))
                        + ":"
                        + str(job[field]).casefold(),
                    )
                )
        return sorted(aliases)

    def ingest(self, jobs: list[dict[str, Any]], mode: str = "backlog") -> dict[str, Any]:
        mode = mode.lower().replace("-", "_")
        if mode not in {"backlog", "new", "new_jobs"}:
            raise ValueError("mode must be backlog or new_jobs")

        def ingest_batch(connection: sqlite3.Connection) -> dict[str, Any]:
            result: dict[str, Any] = {
                "discovered": 0,
                "updated": 0,
                "duplicates": 0,
                "ambiguous": 0,
                "queued": 0,
                "job_ids": [],
            }
            now = self.clock()
            for incoming in jobs:
                company, title = (
                    str(incoming.get("company") or "").strip(),
                    str(incoming.get("title") or "").strip(),
                )
                if not company or not title:
                    raise ValueError("Each job requires nonempty company and title")
                identity_key = normalize_text(company) + ":" + normalize_text(title)
                aliases = self._aliases(incoming)
                matches = set()
                for kind, value in aliases:
                    matches.update(
                        row[0]
                        for row in connection.execute(
                            "SELECT job_id FROM job_aliases WHERE kind=? AND value=?", (kind, value)
                        )
                    )
                if not matches:
                    matches.update(
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM jobs WHERE identity_key=?", (identity_key,)
                        )
                    )
                    ambiguous = bool(matches and aliases)
                else:
                    ambiguous = len(matches) > 1
                if ambiguous:
                    result["ambiguous"] += 1
                    for job_id in sorted(matches):
                        app_id = connection.execute(
                            "SELECT id FROM applications WHERE job_id=?", (job_id,)
                        ).fetchone()[0]
                        self._event(
                            connection,
                            app_id,
                            "DUPLICATE_AMBIGUITY",
                            {
                                "candidate": incoming,
                                "matched_job_ids": sorted(matches),
                                "action": "not_queued",
                            },
                            component="ingestion",
                        )
                    continue
                material = {
                    key: incoming.get(key, "")
                    for key in ("company", "title", "location", "description")
                }
                content_hash = hashlib.sha256(_json(material).encode()).hexdigest()
                metadata = dict(incoming.get("metadata") or {})
                for key in ("simplify_id", "ats_id", "requisition_id"):
                    if key in incoming:
                        metadata[key] = incoming[key]
                domain = str(
                    incoming.get("domain")
                    or urlsplit(
                        str(incoming.get("application_url") or incoming.get("simplify_url") or "")
                    ).hostname
                    or "unknown"
                )
                values = (
                    company,
                    title,
                    str(incoming.get("location") or ""),
                    str(incoming.get("source_url") or ""),
                    str(incoming.get("simplify_url") or ""),
                    str(incoming.get("application_url") or ""),
                    domain,
                    str(incoming.get("description") or ""),
                    _json(metadata),
                    identity_key,
                    content_hash,
                )
                if matches:
                    job_id = next(iter(matches))
                    old = connection.execute(
                        "SELECT content_hash FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    app = connection.execute(
                        "SELECT * FROM applications WHERE job_id=?", (job_id,)
                    ).fetchone()
                    connection.execute(
                        "UPDATE jobs SET company=?,title=?,location=?,source_url=?,simplify_url=?,application_url=?,domain=?,description=?,metadata=?,identity_key=?,content_hash=?,last_seen=? WHERE id=?",
                        (*values, now, job_id),
                    )
                    result["duplicates"] += 1
                    if old[0] != content_hash:
                        result["updated"] += 1
                        self._event(
                            connection,
                            app["id"],
                            "JOB_UPDATED",
                            {"previous_hash": old[0], "content_hash": content_hash},
                            component="ingestion",
                        )
                        if app["state"] == "INELIGIBLE":
                            connection.execute(
                                "UPDATE applications SET state='QUEUED',updated_at=? WHERE id=?",
                                (now, app["id"]),
                            )
                            connection.execute(
                                "UPDATE queue_items SET status='AVAILABLE',available_at=? WHERE application_id=?",
                                (now, app["id"]),
                            )
                            self._event(
                                connection,
                                app["id"],
                                "STATE_CHANGED",
                                {
                                    "from": "INELIGIBLE",
                                    "to": "QUEUED",
                                    "reason": "job_data_changed",
                                },
                                component="ingestion",
                            )
                            result["queued"] += 1
                else:
                    job_id = uuid.uuid4().hex
                    app_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO jobs(id,company,title,location,source_url,simplify_url,application_url,domain,description,metadata,identity_key,content_hash,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (job_id, *values, now, now),
                    )
                    connection.execute(
                        "INSERT INTO applications(id,job_id,state,created_at,updated_at) VALUES(?,?,'QUEUED',?,?)",
                        (app_id, job_id, now, now),
                    )
                    connection.execute(
                        "INSERT INTO queue_items(application_id,status,available_at,created_at) VALUES(?,'AVAILABLE',?,?)",
                        (app_id, now, now),
                    )
                    self._event(
                        connection,
                        app_id,
                        "JOB_DISCOVERED",
                        {"mode": mode, "simplify_url": incoming.get("simplify_url")},
                        component="ingestion",
                    )
                    result["discovered"] += 1
                    result["queued"] += 1
                for kind, value in aliases:
                    connection.execute(
                        "INSERT OR IGNORE INTO job_aliases(kind,value,job_id) VALUES(?,?,?)",
                        (kind, value, job_id),
                    )
                result["job_ids"].append(job_id)
            connection.execute(
                "INSERT INTO meta(key,value) VALUES('last_ingest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_json({"mode": mode, "timestamp": now}),),
            )
            return result

        return self._write(ingest_batch)

    def get_application(self, application_id: str) -> dict[str, Any]:
        return self._read(lambda connection: self._application(connection, application_id))

    def applications(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            sql = (
                "SELECT id FROM applications"
                + (" WHERE state=?" if status else "")
                + " ORDER BY created_at DESC,id LIMIT ?"
            )
            params = (status, max(1, min(limit, 10000))) if status else (max(1, min(limit, 10000)),)
            return [
                self._application(connection, row[0])
                for row in connection.execute(sql, params).fetchall()
            ]

        return self._read(read)

    def _recover_expired(self, connection: sqlite3.Connection) -> int:
        now = self.clock()
        rows = connection.execute(
            "SELECT * FROM applications WHERE worker_id IS NOT NULL AND lease_until<=?", (now,)
        ).fetchall()
        for row in rows:
            state = row["state"]
            if (
                state == "SUBMITTING"
                or connection.execute(
                    "SELECT 1 FROM submission_intents WHERE application_id=?", (row["id"],)
                ).fetchone()
            ):
                state = "SUBMITTED" if state == "SUBMITTED" else "SUBMISSION_UNCONFIRMED"
                connection.execute(
                    "UPDATE submission_intents SET status=?,finished_at=? WHERE application_id=?",
                    (state, now, row["id"]),
                )
            queue_state = (
                "BLOCKED"
                if state == "NEEDS_USER"
                else "DONE"
                if state in TERMINAL_STATES
                else "AVAILABLE"
            )
            connection.execute(
                "UPDATE applications SET state=?,worker_id=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (state, now, row["id"]),
            )
            connection.execute(
                "UPDATE queue_items SET status=?,available_at=? WHERE application_id=?",
                (queue_state, now, row["id"]),
            )
            self._event(
                connection,
                row["id"],
                "LEASE_RECOVERED",
                {"previous_state": row["state"], "state": state},
                row["worker_id"],
                "scheduler",
            )
        return len(rows)

    def recover_expired(self) -> int:
        return self._write(self._recover_expired)

    def claim(
        self, worker_id: str, lease_seconds: float = 120, per_domain_limit: int = 1
    ) -> dict[str, Any] | None:
        if not worker_id or lease_seconds <= 0 or per_domain_limit < 1:
            raise ValueError("Worker ID, positive lease and domain limit are required")

        def claim_transaction(connection: sqlite3.Connection) -> dict[str, Any] | None:
            self._recover_expired(connection)
            if (
                connection.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0]
                != "RUNNING"
            ):
                return None
            now = self.clock()
            # One worker may own only one application. Recover/reuse that lease explicitly.
            if connection.execute(
                "SELECT 1 FROM applications WHERE worker_id=? AND lease_until>?", (worker_id, now)
            ).fetchone():
                return None
            rows = connection.execute(
                "SELECT a.id,COALESCE(json_extract(a.data,'$.current_domain'),j.domain) AS domain FROM queue_items q JOIN applications a ON a.id=q.application_id JOIN jobs j ON j.id=a.job_id WHERE q.status='AVAILABLE' AND q.available_at<=? ORDER BY q.priority DESC,q.created_at,a.id",
                (now,),
            ).fetchall()
            for row in rows:
                active = connection.execute(
                    "SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id WHERE COALESCE(json_extract(a.data,'$.current_domain'),j.domain)=? AND a.worker_id IS NOT NULL AND a.lease_until>?",
                    (row["domain"], now),
                ).fetchone()[0]
                if active >= per_domain_limit:
                    continue
                token = uuid.uuid4().hex
                connection.execute(
                    "UPDATE applications SET worker_id=?,lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                    (worker_id, token, now + lease_seconds, now, row["id"]),
                )
                connection.execute(
                    "UPDATE queue_items SET status='LEASED',attempts=attempts+1 WHERE application_id=?",
                    (row["id"],),
                )
                self._event(
                    connection,
                    row["id"],
                    "LEASE_CLAIMED",
                    {"lease_until": now + lease_seconds},
                    worker_id,
                    "scheduler",
                )
                return self._application(connection, row["id"])
            return None

        return self._write(claim_transaction)

    def heartbeat(
        self, application_id: str, worker_id: str, lease_token: str, lease_seconds: float = 120
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        def heartbeat_transaction(connection: sqlite3.Connection) -> bool:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            connection.execute(
                "UPDATE applications SET lease_until=? WHERE id=?",
                (self.clock() + lease_seconds, application_id),
            )
            return True

        return self._write(heartbeat_transaction)

    def release(
        self,
        application_id: str,
        worker_id: str,
        lease_token: str,
        delay_seconds: float = 0,
        *,
        delay: float | None = None,
        blocked: bool = False,
    ) -> None:
        if delay is not None:
            delay_seconds = delay

        def release_transaction(connection: sqlite3.Connection) -> None:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            if app["state"] == "SUBMITTING":
                raise InvalidTransition(
                    "A submission intent must be resolved before releasing its lease"
                )
            queue_state = (
                "DONE"
                if app["state"] in TERMINAL_STATES
                else "BLOCKED"
                if blocked or app["state"] == "NEEDS_USER"
                else "AVAILABLE"
            )
            connection.execute(
                "UPDATE applications SET worker_id=NULL,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (self.clock(), application_id),
            )
            connection.execute(
                "UPDATE queue_items SET status=?,available_at=? WHERE application_id=?",
                (queue_state, self.clock() + max(0, delay_seconds), application_id),
            )
            self._event(
                connection,
                application_id,
                "LEASE_RELEASED",
                {"delay_seconds": delay_seconds},
                worker_id,
                "scheduler",
            )

        self._write(release_transaction)

    def acquire_domain(
        self, application_id: str, domain: str, worker_id: str, lease_token: str, limit: int = 1
    ) -> bool:
        domain = domain.strip().lower().rstrip(".")
        if "://" in domain:
            domain = urlsplit(domain).hostname or ""
        if not domain or limit < 1:
            raise ValueError("A domain and positive limit are required")

        def acquire(connection: sqlite3.Connection) -> bool:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            active = connection.execute(
                "SELECT COUNT(*) FROM applications a JOIN jobs j ON j.id=a.job_id WHERE COALESCE(json_extract(a.data,'$.current_domain'),j.domain)=? AND a.id!=? AND a.worker_id IS NOT NULL AND a.lease_until>?",
                (domain, application_id, self.clock()),
            ).fetchone()[0]
            if active >= limit:
                return False
            connection.execute(
                "UPDATE applications SET data=?,updated_at=? WHERE id=?",
                (_json(app["data"] | {"current_domain": domain}), self.clock(), application_id),
            )
            return True

        return self._write(acquire)

    def requeue(self, application_id: str, delay_seconds: float = 0) -> dict[str, Any]:
        """Wake a parked reasoning task without rewinding its application state."""

        def wake(connection: sqlite3.Connection) -> dict[str, Any]:
            app = self._application(connection, application_id)
            self._fence(app, None, None)
            if app["state"] in PARKED_STATES or app["state"] in SUBMISSION_STATES:
                raise InvalidTransition(
                    "Use resume_application for failed or human-blocked applications"
                )
            connection.execute(
                "UPDATE queue_items SET status='AVAILABLE',available_at=? WHERE application_id=?",
                (self.clock() + max(0, delay_seconds), application_id),
            )
            self._event(
                connection, application_id, "APPLICATION_REQUEUED", {}, component="scheduler"
            )
            return app

        return self._write(wake)

    def transition(
        self,
        application_id: str,
        to_state: str,
        worker_id: str | None = None,
        lease_token: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        to_state = str(to_state)
        if to_state in SUBMISSION_STATES:
            raise InvalidTransition(
                "Use reserve_submission/finish_submission for submission states"
            )

        def transition_transaction(connection: sqlite3.Connection) -> dict[str, Any]:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            validate_transition(app["state"], to_state)
            if app["state"] == to_state:
                return app
            data = app["data"]
            if to_state == "NEEDS_USER":
                data["resume_state"] = app["state"]
                data["intervention"] = details or {}
            connection.execute(
                "UPDATE applications SET state=?,data=?,updated_at=? WHERE id=?",
                (to_state, _json(data), self.clock(), application_id),
            )
            if to_state in PARKED_STATES:
                connection.execute(
                    "UPDATE queue_items SET status=? WHERE application_id=?",
                    ("BLOCKED" if to_state == "NEEDS_USER" else "DONE", application_id),
                )
            elif app["state"] in PARKED_STATES:
                connection.execute(
                    "UPDATE queue_items SET status=?,available_at=? WHERE application_id=?",
                    ("LEASED" if app["worker_id"] else "AVAILABLE", self.clock(), application_id),
                )
            self._event(
                connection,
                application_id,
                "STATE_CHANGED",
                {"from": app["state"], "to": to_state, "details": details or {}},
                worker_id,
            )
            return self._application(connection, application_id)

        return self._write(transition_transaction)

    def resume_application(self, application_id: str) -> dict[str, Any]:
        app = self.get_application(application_id)
        if app["state"] in SUBMISSION_STATES:
            raise InvalidTransition("An application with a submission intent cannot be retried")
        if app["state"] == "NEEDS_USER":
            state = app["data"].get("resume_state", "READY")
            if state not in ALLOWED_TRANSITIONS["NEEDS_USER"]:
                state = "READY"
        elif app["state"] in {"FAILED", "SKIPPED", "INELIGIBLE"}:
            state = "QUEUED"
        else:
            return app
        return self.transition(application_id, state, details={"reason": "explicit_retry"})

    retry = resume_application

    def event(
        self,
        application_id: str,
        event_type: str,
        data: Any,
        worker_id: str | None = None,
        component: str = "engine",
        lease_token: str | None = None,
    ) -> int:
        def append(connection: sqlite3.Connection) -> int:
            if lease_token is not None:
                self._fence(self._application(connection, application_id), worker_id, lease_token)
            return self._event(connection, application_id, event_type, data, worker_id, component)

        return self._write(append)

    def events(
        self, application_id: str, limit: int = 10000, after_id: int = 0
    ) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            result = []
            for row in connection.execute(
                "SELECT * FROM application_events WHERE application_id=? AND id>? ORDER BY id LIMIT ?",
                (application_id, after_id, max(1, min(limit, 100000))),
            ):
                item = dict(row)
                item["data"] = _decode(item["data"], {})
                result.append(item)
            return result

        return self._read(read)

    def set_application_data(
        self,
        application_id: str,
        *,
        worker_id: str | None = None,
        lease_token: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        forbidden = {"state", "id", "job_id", "created_at", "updated_at", "lease_until"}
        if forbidden.intersection(kwargs):
            raise ValueError("Application identity, state and leases cannot be changed as data")

        def update(connection: sqlite3.Connection) -> dict[str, Any]:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            data = app["data"] | kwargs
            connection.execute(
                "UPDATE applications SET data=?,updated_at=? WHERE id=?",
                (_json(data), self.clock(), application_id),
            )
            return self._application(connection, application_id)

        return self._write(update)

    def set_control(self, control: str) -> str:
        control = control.upper()
        if control not in {"RUNNING", "PAUSED", "STOPPED"}:
            raise ValueError("Control must be RUNNING, PAUSED or STOPPED")
        self._write(
            lambda connection: connection.execute(
                "UPDATE meta SET value=? WHERE key='control'", (control,)
            )
        )
        return control

    def get_control(self) -> str:
        return self._read(
            lambda connection: connection.execute(
                "SELECT value FROM meta WHERE key='control'"
            ).fetchone()[0]
        )

    def meta_get(self, key: str, default: Any = None) -> Any:
        if key == "control":
            return self.get_control()

        def read(connection: sqlite3.Connection) -> Any:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return _decode(row[0]) if row else default

        return self._read(read)

    def meta_set(self, key: str, value: Any) -> None:
        if key == "control":
            self.set_control(str(value))
            return
        self._write(
            lambda connection: connection.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _json(value)),
            )
        )

    def queue(self, limit: int = 1000) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            result = []
            for row in connection.execute(
                "SELECT * FROM queue_items WHERE status!='DONE' ORDER BY priority DESC,created_at LIMIT ?",
                (max(1, min(limit, 10000)),),
            ).fetchall():
                item = dict(row)
                item["application"] = self._application(connection, row["application_id"])
                result.append(item)
            return result

        return self._read(read)

    def status(self) -> dict[str, Any]:
        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            counts = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT state,COUNT(*) FROM applications GROUP BY state"
                )
            }
            queue_counts = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT status,COUNT(*) FROM queue_items GROUP BY status"
                )
            }
            active = [
                self._application(connection, row[0])
                for row in connection.execute(
                    "SELECT id FROM applications WHERE worker_id IS NOT NULL AND lease_until>? ORDER BY worker_id",
                    (self.clock(),),
                ).fetchall()
            ]
            return {
                "control": connection.execute(
                    "SELECT value FROM meta WHERE key='control'"
                ).fetchone()[0],
                "counts": counts,
                "queue": queue_counts,
                "total": sum(counts.values()),
                "active_workers": active,
                "submitted": counts.get("SUBMITTED", 0),
                "needs_user": counts.get("NEEDS_USER", 0),
                "queued": queue_counts.get("AVAILABLE", 0),
            }

        return self._read(read)

    def reserve_submission(self, application_id: str, worker_id: str, lease_token: str) -> bool:
        def reserve(connection: sqlite3.Connection) -> bool:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            if connection.execute(
                "SELECT 1 FROM submission_intents WHERE job_id=?", (app["job_id"],)
            ).fetchone():
                return False
            if (
                connection.execute("SELECT value FROM meta WHERE key='control'").fetchone()[0]
                != "RUNNING"
            ):
                return False
            if app["state"] != "VALIDATING":
                raise InvalidTransition("Only a validated application may reserve submission")
            connection.execute(
                "INSERT INTO submission_intents(application_id,job_id,worker_id,lease_token,created_at,status) VALUES(?,?,?,?,?,'SUBMITTING')",
                (application_id, app["job_id"], worker_id, lease_token, self.clock()),
            )
            connection.execute(
                "UPDATE applications SET state='SUBMITTING',updated_at=? WHERE id=?",
                (self.clock(), application_id),
            )
            self._event(
                connection,
                application_id,
                "SUBMISSION_RESERVED",
                {"from": "VALIDATING", "to": "SUBMITTING"},
                worker_id,
                "submission",
            )
            return True

        return self._write(reserve)

    def finish_submission(
        self,
        application_id: str,
        confirmed: bool,
        evidence: Any,
        worker_id: str | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        if confirmed and not evidence:
            raise ValueError("Confirmed submission requires positive confirmation evidence")

        def finish(connection: sqlite3.Connection) -> dict[str, Any]:
            app = self._application(connection, application_id)
            intent = connection.execute(
                "SELECT * FROM submission_intents WHERE application_id=?", (application_id,)
            ).fetchone()
            if intent is None:
                raise InvalidTransition("No durable submission intent exists")
            if worker_id is not None or lease_token is not None:
                self._fence(app, worker_id, lease_token)
            if app["state"] == "SUBMITTED":
                return app
            state = "SUBMITTED" if confirmed else "SUBMISSION_UNCONFIRMED"
            validate_transition(app["state"], state)
            connection.execute(
                "UPDATE submission_intents SET status=?,evidence=?,finished_at=? WHERE application_id=?",
                (state, _json(evidence), self.clock(), application_id),
            )
            connection.execute(
                "UPDATE applications SET state=?,updated_at=? WHERE id=?",
                (state, self.clock(), application_id),
            )
            connection.execute(
                "UPDATE queue_items SET status='DONE' WHERE application_id=?", (application_id,)
            )
            self._event(
                connection,
                application_id,
                "SUBMISSION_RESULT",
                {"state": state, "evidence": evidence},
                worker_id or intent["worker_id"],
                "submission",
            )
            return self._application(connection, application_id)

        return self._write(finish)

    def record_answer(
        self,
        application_id: str,
        question: str,
        answer: dict[str, Any],
        worker_id: str | None = None,
        lease_token: str | None = None,
    ) -> int:
        def record(connection: sqlite3.Connection) -> int:
            app = self._application(connection, application_id)
            self._fence(app, worker_id, lease_token)
            cursor = connection.execute(
                "INSERT INTO answers(application_id,question,normalized_question,company,role,answer,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    application_id,
                    question,
                    normalize_text(question),
                    app["job"]["company"],
                    app["job"]["title"],
                    _json(answer),
                    self.clock(),
                ),
            )
            self._event(
                connection,
                application_id,
                "ANSWER_RECORDED",
                {"question": question, **answer},
                worker_id,
                "answers",
            )
            return int(cursor.lastrowid)

        return self._write(record)

    def answers(self, application_id: str) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            result = []
            for row in connection.execute(
                "SELECT * FROM answers WHERE application_id=? ORDER BY id", (application_id,)
            ):
                item = dict(row)
                item["answer"] = _decode(item["answer"], {})
                result.append(item)
            return result

        return self._read(read)

    def save_answer_memory(self, record: dict[str, Any]) -> str:
        normalized = record.get("normalized_question") or normalize_text(
            str(record.get("question", ""))
        )
        if not normalized or not record.get("profile_hash"):
            raise ValueError("Answer memory requires a question and profile_hash")
        normalized_record = record | {"normalized_question": normalized}
        fingerprint = hashlib.sha256(_json(normalized_record).encode()).hexdigest()
        memory_id = uuid.uuid4().hex

        def save(connection: sqlite3.Connection) -> str:
            connection.execute(
                "INSERT OR IGNORE INTO answer_memory(id,fingerprint,normalized_question,profile_hash,company,job_id,record,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    memory_id,
                    fingerprint,
                    normalized,
                    record["profile_hash"],
                    record.get("company") or "",
                    record.get("job_id"),
                    _json(normalized_record),
                    self.clock(),
                ),
            )
            return connection.execute(
                "SELECT id FROM answer_memory WHERE fingerprint=?", (fingerprint,)
            ).fetchone()[0]

        return self._write(save)

    def find_answers(
        self,
        profile_hash: str | None = None,
        company: str | None = None,
        job_id: str | None = None,
        normalized_question: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        for column, value in (
            ("profile_hash", profile_hash),
            ("company", company),
            ("job_id", job_id),
            ("normalized_question", normalized_question),
        ):
            if value is not None:
                clauses.append(column + "=?")
                params.append(value)
        sql = (
            "SELECT * FROM answer_memory"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 10000)))
        return self._read(
            lambda connection: [
                _decode(row["record"], {}) | {"id": row["id"], "created_at": row["created_at"]}
                for row in connection.execute(sql, params)
            ]
        )

    def cache_get(self, namespace: str, key: str) -> Any:
        def read(connection: sqlite3.Connection) -> Any:
            row = connection.execute(
                "SELECT value,expires_at FROM shared_cache WHERE namespace=? AND key=?",
                (namespace, key),
            ).fetchone()
            if (
                row is None
                or row["value"] is None
                or (row["expires_at"] is not None and row["expires_at"] <= self.clock())
            ):
                return None
            return _decode(row["value"])

        return self._read(read)

    def cache_set(
        self, namespace: str, key: str, value: Any, ttl_seconds: float | None = None
    ) -> None:
        expiry = None if ttl_seconds is None else self.clock() + ttl_seconds
        self._write(
            lambda connection: connection.execute(
                "INSERT INTO shared_cache(namespace,key,value,expires_at,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at,owner=NULL,lease_until=NULL,updated_at=excluded.updated_at",
                (namespace, key, _json(value), expiry, self.clock()),
            )
        )

    def cache_acquire(
        self, namespace: str, key: str, owner: str, lease_seconds: float = 120
    ) -> bool:
        if not owner or lease_seconds <= 0:
            raise ValueError("Cache lease requires an owner and positive timeout")

        def acquire(connection: sqlite3.Connection) -> bool:
            now = self.clock()
            row = connection.execute(
                "SELECT * FROM shared_cache WHERE namespace=? AND key=?", (namespace, key)
            ).fetchone()
            if row is not None:
                if row["value"] is not None and (
                    row["expires_at"] is None or row["expires_at"] > now
                ):
                    return False
                if row["owner"] and (row["lease_until"] or 0) > now:
                    return False
            connection.execute(
                "INSERT INTO shared_cache(namespace,key,owner,lease_until,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET owner=excluded.owner,lease_until=excluded.lease_until,updated_at=excluded.updated_at",
                (namespace, key, owner, now + lease_seconds, now),
            )
            return True

        return self._write(acquire)

    def cache_complete(
        self, namespace: str, key: str, owner: str, value: Any, ttl_seconds: float | None = None
    ) -> bool:
        def complete(connection: sqlite3.Connection) -> bool:
            expiry = None if ttl_seconds is None else self.clock() + ttl_seconds
            cursor = connection.execute(
                "UPDATE shared_cache SET value=?,expires_at=?,owner=NULL,lease_until=NULL,updated_at=? WHERE namespace=? AND key=? AND owner=? AND lease_until>?",
                (_json(value), expiry, self.clock(), namespace, key, owner, self.clock()),
            )
            return cursor.rowcount == 1

        return self._write(complete)

    def cache_release(self, namespace: str, key: str, owner: str) -> bool:
        return self._write(
            lambda connection: (
                connection.execute(
                    "UPDATE shared_cache SET owner=NULL,lease_until=NULL WHERE namespace=? AND key=? AND owner=?",
                    (namespace, key, owner),
                ).rowcount
                == 1
            )
        )

    def enqueue_reasoning(
        self,
        cache_key: str,
        payload: dict[str, Any],
        application_id: str | None = None,
        kind: str = "answer",
    ) -> dict[str, Any]:
        def enqueue(connection: sqlite3.Connection) -> dict[str, Any]:
            now = self.clock()
            connection.execute(
                "INSERT OR IGNORE INTO reasoning_tasks(id,cache_key,application_id,kind,status,payload,created_at,updated_at) VALUES(?,?,?,?,'PENDING',?,?,?)",
                (uuid.uuid4().hex, cache_key, application_id, kind, _json(payload), now, now),
            )
            row = connection.execute(
                "SELECT * FROM reasoning_tasks WHERE cache_key=?", (cache_key,)
            ).fetchone()
            return self._reasoning(row)

        return self._write(enqueue)

    @staticmethod
    def _reasoning(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _decode(result["payload"], {})
        result["result"] = _decode(result["result"])
        return result

    def get_reasoning(
        self, task_id: str | None = None, status: str | None = "PENDING", limit: int = 100
    ) -> Any:
        def read(connection: sqlite3.Connection) -> Any:
            if task_id:
                row = connection.execute(
                    "SELECT * FROM reasoning_tasks WHERE id=?", (task_id,)
                ).fetchone()
                return self._reasoning(row) if row else None
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM reasoning_tasks ORDER BY created_at LIMIT ?", (limit,)
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM reasoning_tasks WHERE status=? ORDER BY created_at LIMIT ?",
                    (status, limit),
                )
            return [self._reasoning(row) for row in rows]

        return self._read(read)

    def complete_reasoning(
        self, task_id: str, result: dict[str, Any], failed: bool = False
    ) -> dict[str, Any]:
        def complete(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                "SELECT * FROM reasoning_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown reasoning task: {task_id}")
            if row["status"] == "PENDING":
                connection.execute(
                    "UPDATE reasoning_tasks SET status=?,result=?,updated_at=? WHERE id=?",
                    ("FAILED" if failed else "COMPLETE", _json(result), self.clock(), task_id),
                )
            return self._reasoning(
                connection.execute(
                    "SELECT * FROM reasoning_tasks WHERE id=?", (task_id,)
                ).fetchone()
            )

        return self._write(complete)
