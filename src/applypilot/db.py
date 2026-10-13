"""SQLite persistence: short IMMEDIATE transactions, durable leases with fencing tokens,
atomic queue claiming, a uniqueness-backed submission guard and an append-only audit log.

Every public method opens its own connection, so one ``Database`` can be shared by
asyncio workers, threads and separate processes (backend + stdio MCP proxy). WAL mode
lets readers proceed while a writer holds the lock; writers retry with jittered
backoff on ``database is locked``.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import closing
from pathlib import Path
from typing import Any, TypeVar

from applypilot.security import redact
from applypilot.state import (
    BROWSER_STATES,
    CLAIM_PRIORITY,
    PARKED,
    PREP_STATES,
    SUBMISSION,
    TAB_HOLDING,
    TERMINAL,
    InvalidTransition,
    LeaseLost,
    S,
    validate_transition,
)

T = TypeVar("T")
MIGRATIONS_DIR = Path(__file__).with_name("migrations")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class DuplicateSubmission(Exception):
    """Another application for the same (or an equivalent) job is submitted/submitting."""

    def __init__(self, message: str, conflicts: list[dict[str, Any]]):
        super().__init__(message)
        self.conflicts = conflicts


class Database:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time, busy_timeout_ms: int = 5000):
        self.clock = clock
        self.busy_timeout_ms = busy_timeout_ms
        self._keeper: sqlite3.Connection | None = None
        raw = str(path)
        if raw == ":memory:":
            self.path = f"file:applypilot-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._connect()  # keep the shared in-memory DB alive
        else:
            self._uri = raw.startswith("file:")
            self.path = raw
            if not self._uri:
                Path(raw).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None,
                               uri=self._uri, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def write(self, op: Callable[[sqlite3.Connection], T], *, attempts: int = 8) -> T:
        """Run ``op`` inside BEGIN IMMEDIATE with bounded retry on lock contention."""
        for attempt in range(attempts):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = op(conn)
                conn.execute("COMMIT")
                return result
            except sqlite3.OperationalError as exc:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                msg = str(exc).lower()
                if ("locked" not in msg and "busy" not in msg) or attempt == attempts - 1:
                    raise
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            time.sleep(min(1.0, 0.01 * 2**attempt) + random.uniform(0, 0.02))
        raise RuntimeError("unreachable")

    def read(self, op: Callable[[sqlite3.Connection], T]) -> T:
        with closing(self._connect()) as conn:
            return op(conn)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return self.read(lambda c: [dict(r) for r in c.execute(sql, tuple(params)).fetchall()])

    def initialize(self) -> None:
        with closing(self._connect()) as conn:
            if not self._uri:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

        def migrate(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            files = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
            known = {int(p.name[:3]) for p in files}
            if applied - known:
                raise RuntimeError(
                    f"Database schema version {max(applied)} is newer than this ApplyPilot build; upgrade ApplyPilot."
                )
            for path in files:
                version = int(path.name[:3])
                if version in applied:
                    continue
                statement = ""
                for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        if statement.strip():
                            conn.execute(statement)
                        statement = ""
                if statement.strip() and not all(
                    ln.strip().startswith("--") or not ln.strip() for ln in statement.splitlines()
                ):
                    raise RuntimeError(f"Incomplete SQL statement at end of migration {path.name}")
                conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, self.clock()))
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('control', '\"STOPPED\"')")
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('mode', '\"backlog\"')")

        self.write(migrate)

    def schema_version(self) -> int:
        rows = self.query("SELECT MAX(version) AS v FROM schema_migrations")
        return int(rows[0]["v"] or 0)

    # ------------------------------------------------------------------ meta/control
    def meta_get(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return loads(rows[0]["value"], default) if rows else default

    def meta_set(self, key: str, value: Any) -> None:
        self.write(lambda c: c.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, dumps(value))))

    def meta_delete(self, key: str) -> None:
        self.write(lambda c: c.execute("DELETE FROM meta WHERE key=?", (key,)))

    def execution_scope(self) -> dict[str, Any] | None:
        value = self.meta_get("execution_scope")
        return value if isinstance(value, dict) else None

    # ------------------------------------------------------------------ locks
    def acquire_lock(self, name: str, owner: str, ttl: float) -> bool:
        now = self.clock()

        def op(c: sqlite3.Connection) -> bool:
            row = c.execute("SELECT owner, expires_at FROM locks WHERE name=?", (name,)).fetchone()
            if row and row["owner"] != owner and row["expires_at"] > now:
                return False
            c.execute(
                "INSERT INTO locks(name, owner, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                (name, owner, now + ttl))
            return True

        return self.write(op)

    def release_lock(self, name: str, owner: str) -> None:
        self.write(lambda c: c.execute("DELETE FROM locks WHERE name=? AND owner=?", (name, owner)))

    def lock_owner(self, name: str) -> str | None:
        rows = self.query("SELECT owner FROM locks WHERE name=? AND expires_at>?", (name, self.clock()))
        return rows[0]["owner"] if rows else None

    # ------------------------------------------------------------------ jobs
    @staticmethod
    def _job_row(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        job = dict(row)
        for key in ("locations", "terms", "degrees"):
            job[key] = loads(job.get(key), [])
        job["details"] = loads(job.get("details"), {})
        job["active"] = bool(job.get("active"))
        return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.read(lambda c: self._job_row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))

    def find_job_by_aliases(self, aliases: list[tuple[str, str]]) -> str | None:
        def op(c: sqlite3.Connection) -> str | None:
            for kind, value in aliases:
                row = c.execute("SELECT job_id FROM job_aliases WHERE kind=? AND value=?", (kind, value)).fetchone()
                if row:
                    return str(row["job_id"])
            return None

        return self.read(op)

    def upsert_jobs(self, records: list[dict[str, Any]], *, batch: str) -> dict[str, Any]:
        """Insert or update normalized job records (see ingest.listings.JobRecord.to_db).

        Identity resolution uses every alias (Simplify id, ATS key, normalized URLs), so a
        job keeps its id when repository formatting or URLs change slightly.
        """
        now = self.clock()
        stats: dict[str, Any] = {"new": 0, "updated": 0, "unchanged": 0, "closed": 0, "new_ids": [], "reopened_ids": []}

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            for rec in records:
                aliases: list[tuple[str, str]] = rec.pop("_aliases", [])
                job_id = None
                for kind, value in aliases:
                    row = c.execute("SELECT job_id FROM job_aliases WHERE kind=? AND value=?", (kind, value)).fetchone()
                    if row:
                        job_id = row["job_id"]
                        break
                if job_id is None:
                    job_id = rec["id"]
                    existing = c.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
                else:
                    existing = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                columns = {k: v for k, v in rec.items() if k != "id"}
                for key in ("locations", "terms", "degrees", "details"):
                    if key in columns and not isinstance(columns[key], str):
                        columns[key] = dumps(columns[key])
                if "active" in columns:
                    columns["active"] = 1 if columns["active"] else 0
                if existing is None:
                    columns.update({"source_batch": batch, "first_seen": now, "last_seen": now})
                    names = ["id", *columns]
                    c.execute(
                        f"INSERT INTO jobs({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                        [job_id, *columns.values()])
                    stats["new"] += 1
                    stats["new_ids"].append(job_id)
                else:
                    old = c.execute("SELECT content_hash, active, simplify_id FROM jobs WHERE id=?", (job_id,)).fetchone()
                    rec_sid = columns.pop("simplify_id", None)
                    if rec_sid and old["simplify_id"] and rec_sid != old["simplify_id"]:
                        # A second listing for the same underlying posting (shared ATS key/URL):
                        # keep the first listing's data, remember the alias, never apply twice.
                        stats.setdefault("duplicate_listings", []).append({"job_id": job_id, "simplify_id": rec_sid})
                        for kind, value in aliases:
                            c.execute("INSERT OR IGNORE INTO job_aliases(kind, value, job_id) VALUES (?, ?, ?)",
                                      (kind, value, job_id))
                        continue
                    if rec_sid and not old["simplify_id"]:
                        columns["simplify_id"] = rec_sid
                    changed = old["content_hash"] != columns.get("content_hash")
                    if old["active"] and not columns.get("active", 1):
                        stats["closed"] += 1
                    if not old["active"] and columns.get("active", 1):
                        stats["reopened_ids"].append(job_id)
                    columns["last_seen"] = now
                    for keep in ("details", "details_fetched_at", "apply_url"):
                        if columns.get(keep) in (None, "", "{}"):
                            columns.pop(keep, None)  # never wipe enrichment fetched later
                    assignments = ",".join(f"{k}=?" for k in columns)
                    c.execute(f"UPDATE jobs SET {assignments} WHERE id=?", [*columns.values(), job_id])
                    stats["updated" if changed else "unchanged"] += 1
                    if changed:
                        stats.setdefault("changed_ids", []).append(job_id)
                for kind, value in aliases:
                    c.execute("INSERT OR IGNORE INTO job_aliases(kind, value, job_id) VALUES (?, ?, ?)",
                              (kind, value, job_id))
            return stats

        return self.write(op)

    def update_job(self, job_id: str, **fields: Any) -> None:
        for key in ("locations", "terms", "degrees", "details"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = dumps(fields[key])
        if not fields:
            return
        assignments = ",".join(f"{k}=?" for k in fields)
        self.write(lambda c: c.execute(f"UPDATE jobs SET {assignments} WHERE id=?", [*fields.values(), job_id]))

    def add_job_aliases(self, job_id: str, aliases: list[tuple[str, str]]) -> list[str]:
        """Attach aliases; returns ids of OTHER jobs that already own any alias (dupes)."""

        def op(c: sqlite3.Connection) -> list[str]:
            others = []
            for kind, value in aliases:
                row = c.execute("SELECT job_id FROM job_aliases WHERE kind=? AND value=?", (kind, value)).fetchone()
                if row and row["job_id"] != job_id:
                    others.append(row["job_id"])
                elif not row:
                    c.execute("INSERT INTO job_aliases(kind, value, job_id) VALUES (?, ?, ?)", (kind, value, job_id))
            return sorted(set(others))

        return self.write(op)

    def job_counts(self) -> dict[str, int]:
        rows = self.query(
            "SELECT COUNT(*) AS total, SUM(active) AS active, "
            "SUM(CASE WHEN source_batch='incremental' THEN 1 ELSE 0 END) AS incremental FROM jobs")
        r = rows[0]
        return {"total": r["total"] or 0, "active": r["active"] or 0, "incremental": r["incremental"] or 0}

    # ------------------------------------------------------------------ applications
    def _app(self, c: sqlite3.Connection, app_id: str) -> dict[str, Any]:
        row = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown application {app_id}")
        return self._app_row(row)

    @staticmethod
    def _app_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        app = dict(row)
        app["data"] = loads(app.get("data"), {})
        return app

    def get_application(self, app_id: str) -> dict[str, Any]:
        return self.read(lambda c: self._app(c, app_id))

    def application_for_job(self, job_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM applications WHERE job_id=?", (job_id,))
        return self._app_row(rows[0]) if rows else None

    def create_application(self, job_id: str, *, mode: str, state: str = S.QUEUED,
                           priority: int | None = None, data: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        """Idempotent: one application per job (UNIQUE job_id). Returns (app, created)."""
        now = self.clock()
        app_id = new_id("app")

        def op(c: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
            cur = c.execute(
                "INSERT OR IGNORE INTO applications(id, job_id, state, priority, mode, data, created_at, updated_at, available_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (app_id, job_id, state, priority if priority is not None else CLAIM_PRIORITY.get(S(state), 5),
                 mode, dumps(data or {}), now, now, now))
            created = cur.rowcount == 1
            row = c.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
            app = self._app_row(row)
            if created:
                self._event(c, app, None, "queue", "application_created", f"Queued ({mode})", {"state": state})
            return app, created

        return self.write(op)

    def queue_jobs(self, *, mode: str, job_ids: list[str] | None = None) -> int:
        """Create QUEUED applications for open jobs that have never been considered.

        backlog: every active job without an application.
        new_jobs: only jobs discovered after the baseline ingestion.
        """
        now = self.clock()

        def op(c: sqlite3.Connection) -> int:
            sql = "SELECT id FROM jobs j WHERE active=1 AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id)"
            params: list[Any] = []
            if mode == "new_jobs":
                sql += " AND source_batch='incremental'"
            if job_ids is not None:
                if not job_ids:
                    return 0
                sql += f" AND id IN ({','.join('?' for _ in job_ids)})"
                params += job_ids
            rows = c.execute(sql + " ORDER BY date_posted DESC, first_seen", params).fetchall()
            count = 0
            for row in rows:
                app_id = new_id("app")
                c.execute(
                    "INSERT OR IGNORE INTO applications(id, job_id, state, priority, mode, data, created_at, updated_at, available_at) "
                    "VALUES (?, ?, 'QUEUED', ?, ?, '{}', ?, ?, ?)",
                    (app_id, row["id"], CLAIM_PRIORITY[S.QUEUED], mode, now, now, now))
                c.execute(
                    "INSERT INTO application_events(application_id, job_id, ts, component, event_type, level, message, data) "
                    "VALUES (?, ?, ?, 'queue', 'application_created', 'INFO', ?, '{}')",
                    (app_id, row["id"], now, f"Job discovered and queued ({mode})"))
                count += 1
            return count

        return self.write(op)

    def test_job_candidates(self, job_id: str | None = None, *, new_only: bool = False) -> list[dict[str, Any]]:
        """Read-only candidates for a single offline test.

        Only imported, active listings with a stored employer URL are returned. Jobs
        with a submission intent, an unsafe terminal application, or an equivalent
        submitted posting are excluded.
        """
        params: list[Any] = []
        where = [
            "j.active=1", "j.source<>'manual'", "COALESCE(j.apply_url,j.direct_url) IS NOT NULL",
            "NOT EXISTS (SELECT 1 FROM submissions sx WHERE sx.job_id=j.id AND sx.status<>'ABORTED')",
            "NOT EXISTS (SELECT 1 FROM submissions se WHERE se.status IN "
            "('SUBMITTING','SUBMITTED','SUBMISSION_UNCONFIRMED') AND "
            "(se.dedupe_key=(j.company_norm || '|' || j.title_norm) OR "
            "(j.ats_job_key IS NOT NULL AND se.ats_job_key=j.ats_job_key)))",
            "(a.id IS NULL OR a.state IN ('QUEUED','CHECKING_ELIGIBILITY','READY','OPENING','APPLYING',"
            "'ACCOUNT_REQUIRED','VERIFYING_EMAIL','FILLING','ANSWERING','WAITING_FOR_CAPTCHA','NEEDS_USER',"
            "'VALIDATING','READY_TO_SUBMIT'))",
        ]
        if job_id is not None:
            where.append("j.id=?")
            params.append(job_id)
        if new_only:
            where.append("a.id IS NULL")
        rows = self.query(
            "SELECT j.*, a.id AS application_id, a.state AS application_state, a.worker_id, a.lease_until "
            "FROM jobs j LEFT JOIN applications a ON a.job_id=j.id WHERE " + " AND ".join(where) +
            " ORDER BY CASE WHEN a.id IS NULL THEN 0 ELSE 1 END, j.date_posted DESC, j.first_seen ASC", params)
        return [self._job_row(r) or {} for r in rows]

    def requeue_changed_ineligible(self, job_ids: list[str]) -> int:
        """Re-evaluate INELIGIBLE applications whose job data materially changed."""
        if not job_ids:
            return 0
        now = self.clock()

        def op(c: sqlite3.Connection) -> int:
            n = 0
            for job_id in job_ids:
                row = c.execute("SELECT * FROM applications WHERE job_id=? AND state='INELIGIBLE'", (job_id,)).fetchone()
                if row:
                    c.execute("UPDATE applications SET state='QUEUED', state_reason='job data changed', available_at=?, "
                              "priority=?, updated_at=? WHERE id=?", (now, CLAIM_PRIORITY[S.QUEUED], now, row["id"]))
                    self._event(c, self._app_row(row), None, "queue", "requeued", "Job data changed; re-checking eligibility", {})
                    n += 1
            return n

        return self.write(op)

    # -- claiming / leases ---------------------------------------------------------
    def claim(self, worker_id: str, pool: str, *, lease_seconds: float, per_domain: int = 1,
              max_tabs: int = 1000, app_id: str | None = None) -> dict[str, Any] | None:
        """Atomically lease the highest-priority claimable application for ``pool``.

        A row is claimable when it is in a state served by the pool, not blocked on
        reasoning, due (available_at), not leased (or its lease expired — crash
        recovery), its domain is under the per-domain limit and not cooling down, and,
        for brand-new browser work, the open-tab budget allows another tab.
        """
        states = PREP_STATES if pool == "prep" else BROWSER_STATES
        now = self.clock()
        token = uuid.uuid4().hex

        def op(c: sqlite3.Connection) -> dict[str, Any] | None:
            tabs_open = c.execute(
                f"SELECT COUNT(*) FROM applications WHERE tab_id IS NOT NULL AND state IN ({','.join('?' for _ in TAB_HOLDING)})",
                [s.value for s in TAB_HOLDING]).fetchone()[0]
            params: list[Any] = [s.value for s in states]
            sql = (
                f"SELECT a.* FROM applications a WHERE a.state IN ({','.join('?' for _ in states)}) "
                "AND a.blocked_on IS NULL AND a.available_at <= ? "
                "AND (a.lease_until IS NULL OR a.lease_until < ?) "
            )
            params += [now, now]
            if app_id is not None:
                sql += "AND a.id = ? "
                params.append(app_id)
            if pool == "browser":
                sql += (
                    "AND (a.domain IS NULL OR ((SELECT COUNT(*) FROM applications b WHERE b.domain=a.domain "
                    "AND b.id<>a.id AND b.lease_until >= ? AND b.state IN ("
                    + ",".join("?" for _ in BROWSER_STATES) + ")) < ? "
                    "AND NOT EXISTS (SELECT 1 FROM domain_health d WHERE d.domain=a.domain AND d.cooldown_until > ?))) "
                )
                params += [now, *[s.value for s in BROWSER_STATES], per_domain, now]
                if tabs_open >= max_tabs:
                    sql += "AND a.state <> 'READY' "
            sql += "ORDER BY a.priority ASC, a.available_at ASC, a.created_at ASC LIMIT 1"
            row = c.execute(sql, params).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE applications SET worker_id=?, lease_token=?, lease_until=?, updated_at=?, "
                "started_at=COALESCE(started_at, ?) WHERE id=?",
                (worker_id, token, now + lease_seconds, now, now, row["id"]))
            app = self._app(c, row["id"])
            recovered = bool(row["lease_until"] and row["lease_until"] < now and row["worker_id"])
            self._event(c, app, worker_id, "queue", "claimed",
                        f"Claimed by {worker_id}" + (" (recovered expired lease)" if recovered else ""),
                        {"pool": pool, "state": app["state"], "previous_worker": row["worker_id"] if recovered else None})
            return app

        return self.write(op)

    def _fence(self, c: sqlite3.Connection, app_id: str, worker_id: str | None, token: str | None) -> dict[str, Any]:
        app = self._app(c, app_id)
        if token is None:
            return app
        if app["lease_token"] != token or app["worker_id"] != worker_id or (app["lease_until"] or 0) < self.clock():
            raise LeaseLost(f"{worker_id} no longer owns {app_id}")
        return app

    def heartbeat(self, app_id: str, worker_id: str, token: str, lease_seconds: float) -> bool:
        now = self.clock()

        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "UPDATE applications SET lease_until=? WHERE id=? AND worker_id=? AND lease_token=? AND lease_until>=?",
                (now + lease_seconds, app_id, worker_id, token, now))
            return cur.rowcount == 1

        return self.write(op)

    def release(self, app_id: str, worker_id: str, token: str, *, delay: float = 0,
                blocked_on: str | None = None) -> None:
        now = self.clock()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE applications SET worker_id=NULL, lease_token=NULL, lease_until=NULL, available_at=?, "
                "blocked_on=?, updated_at=? WHERE id=? AND lease_token=?",
                (now + delay, blocked_on, now, app_id, token))

        self.write(op)

    def release_all_leases(self, *, except_prefix: str | None = None) -> int:
        """Startup recovery: drop leases held by workers of a dead backend process."""
        def op(c: sqlite3.Connection) -> int:
            sql = "UPDATE applications SET worker_id=NULL, lease_token=NULL, lease_until=NULL WHERE lease_token IS NOT NULL"
            params: list[Any] = []
            if except_prefix:
                sql += " AND (worker_id IS NULL OR worker_id NOT LIKE ?)"
                params.append(except_prefix + "%")
            return c.execute(sql, params).rowcount

        return self.write(op)

    # -- state ---------------------------------------------------------------------
    def transition(self, app_id: str, to_state: str, *, worker_id: str | None = None, token: str | None = None,
                   reason: str | None = None, data: dict[str, Any] | None = None,
                   fields: dict[str, Any] | None = None, event: tuple[str, str] | None = None,
                   force: bool = False) -> dict[str, Any]:
        """Persist a state change (fenced by lease token when given)."""
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            app = self._fence(c, app_id, worker_id, token)
            src = app["state"]
            if not force:
                validate_transition(src, to_state)
            merged = dict(app["data"])
            if data:
                merged.update(data)
            values: dict[str, Any] = {
                "state": to_state, "state_reason": reason, "updated_at": now, "data": dumps(merged),
                "priority": CLAIM_PRIORITY.get(S(to_state), 9),
            }
            if to_state in {s.value for s in TERMINAL}:
                values["finished_at"] = now
            if to_state == S.SUBMITTED:
                values["submitted_at"] = now
            if fields:
                values.update(fields)
            assignments = ",".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE applications SET {assignments} WHERE id=?", [*values.values(), app_id])
            updated = self._app(c, app_id)
            etype, msg = event or ("state_changed", f"{src} → {to_state}" + (f": {reason}" if reason else ""))
            self._event(c, updated, worker_id, "state", etype, msg, {"from": src, "to": to_state, "reason": reason})
            return updated

        return self.write(op)

    def update_application(self, app_id: str, *, worker_id: str | None = None, token: str | None = None,
                           data: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            app = self._fence(c, app_id, worker_id, token)
            values = dict(fields)
            if data:
                merged = dict(app["data"])
                merged.update(data)
                values["data"] = dumps(merged)
            values["updated_at"] = now
            assignments = ",".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE applications SET {assignments} WHERE id=?", [*values.values(), app_id])
            return self._app(c, app_id)

        return self.write(op)

    def user_action(self, app_id: str, to_state: str, *, reason: str, clear_attempts: bool = False,
                    data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Human-initiated change (resume/retry/skip). Refuses while a worker holds the lease
        unless the target is a pause-like parking state."""
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            app = self._app(c, app_id)
            if app["lease_until"] and app["lease_until"] >= now and to_state not in {S.SKIPPED}:
                raise InvalidTransition("application is currently being processed by a worker; pause first")
            if app["state"] in {s.value for s in SUBMISSION} and to_state != S.SUBMITTED:
                raise InvalidTransition("a submission was already attempted; refusing to restart it")
            validate_transition(app["state"], to_state)
            merged = dict(app["data"])
            merged.pop("needs_user", None)
            if data:
                merged.update(data)
            values: dict[str, Any] = {
                "state": to_state, "state_reason": reason, "updated_at": now, "available_at": now,
                "blocked_on": None, "data": dumps(merged), "priority": CLAIM_PRIORITY.get(S(to_state), 9),
                "worker_id": None, "lease_token": None, "lease_until": None,
            }
            if to_state in {s.value for s in TERMINAL}:
                values["finished_at"] = now
            else:
                values["finished_at"] = None
            if clear_attempts:
                values["attempts"] = 0
            assignments = ",".join(f"{k}=?" for k in values)
            c.execute(f"UPDATE applications SET {assignments} WHERE id=?", [*values.values(), app_id])
            updated = self._app(c, app_id)
            self._event(c, updated, None, "user", "user_action", f"{app['state']} → {to_state}: {reason}",
                        {"from": app["state"], "to": to_state})
            return updated

        return self.write(op)

    def unblock(self, app_ids: Iterable[str]) -> int:
        ids = list(set(app_ids))
        if not ids:
            return 0
        now = self.clock()
        return self.write(lambda c: c.execute(
            f"UPDATE applications SET blocked_on=NULL, available_at=?, updated_at=? WHERE blocked_on IS NOT NULL "
            f"AND id IN ({','.join('?' for _ in ids)})", [now, now, *ids]).rowcount)

    def increment_attempts(self, app_id: str) -> int:
        def op(c: sqlite3.Connection) -> int:
            c.execute("UPDATE applications SET attempts=attempts+1 WHERE id=?", (app_id,))
            return int(c.execute("SELECT attempts FROM applications WHERE id=?", (app_id,)).fetchone()[0])

        return self.write(op)

    # ------------------------------------------------------------------ events
    def _event(self, c: sqlite3.Connection, app: dict[str, Any] | None, worker_id: str | None, component: str,
               event_type: str, message: str, data: dict[str, Any] | None, level: str = "INFO") -> int:
        cur = c.execute(
            "INSERT INTO application_events(application_id, job_id, worker_id, ts, component, event_type, level, message, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (app["id"] if app else None, app["job_id"] if app else None, worker_id, self.clock(), component,
             event_type, level, message[:2000], dumps(redact(data or {}))))
        return int(cur.lastrowid or 0)

    def event(self, app_id: str | None, component: str, event_type: str, message: str = "",
              data: dict[str, Any] | None = None, *, worker_id: str | None = None, level: str = "INFO",
              job_id: str | None = None) -> int:
        def op(c: sqlite3.Connection) -> int:
            app = None
            if app_id:
                row = c.execute("SELECT id, job_id FROM applications WHERE id=?", (app_id,)).fetchone()
                app = dict(row) if row else {"id": app_id, "job_id": job_id}
            return self._event(c, app, worker_id, component, event_type, message, data, level)

        return self.write(op)

    def events(self, app_id: str, *, after_id: int = 0, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM application_events WHERE application_id=? AND id>? ORDER BY id LIMIT ?",
            (app_id, after_id, limit))
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    def recent_events(self, *, level: str | None = None, component: str | None = None, limit: int = 200,
                      since: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM application_events WHERE 1=1"
        params: list[Any] = []
        if level:
            levels = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}
            allowed = [k for k, v in levels.items() if v >= levels.get(level.upper(), 0)]
            sql += f" AND level IN ({','.join('?' for _ in allowed)})"
            params += allowed
        if component:
            sql += " AND component=?"
            params.append(component)
        if since:
            sql += " AND ts>=?"
            params.append(since)
        rows = self.query(sql + " ORDER BY id DESC LIMIT ?", [*params, limit])
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    # ------------------------------------------------------------------ answers (audit)
    def upsert_answer(self, app_id: str, rec: dict[str, Any]) -> None:
        now = self.clock()

        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO answers(application_id, field_key, page_index, question, normalized, qclass, catalog_id, field_type, "
                "options, required, limits, value, method, source, confidence, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(application_id, field_key) DO UPDATE SET "
                "page_index=excluded.page_index, question=excluded.question, normalized=excluded.normalized, "
                "qclass=excluded.qclass, catalog_id=excluded.catalog_id, field_type=excluded.field_type, "
                "options=excluded.options, required=excluded.required, limits=excluded.limits, value=excluded.value, "
                "method=excluded.method, source=excluded.source, confidence=excluded.confidence, "
                "status=excluded.status, updated_at=excluded.updated_at",
                (app_id, rec["field_key"], rec.get("page_index", 0), rec["question"], rec["normalized"], rec["qclass"],
                 rec.get("catalog_id"), rec["field_type"], dumps(rec.get("options") or []), int(bool(rec.get("required"))),
                 dumps(rec.get("limits") or {}), dumps(rec.get("value")), rec.get("method"),
                 dumps(redact(rec.get("source") or {})), rec.get("confidence"), rec.get("status", "planned"), now, now))

        self.write(op)

    def set_answer_status(self, app_id: str, field_keys: list[str], status: str) -> None:
        if not field_keys:
            return
        now = self.clock()
        self.write(lambda c: c.execute(
            f"UPDATE answers SET status=?, updated_at=? WHERE application_id=? AND field_key IN ({','.join('?' for _ in field_keys)})",
            [status, now, app_id, *field_keys]))

    def answers(self, app_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM answers WHERE application_id=? ORDER BY page_index, id", (app_id,))
        for r in rows:
            r["options"] = loads(r["options"], [])
            r["limits"] = loads(r["limits"], {})
            r["value"] = loads(r["value"])
            r["source"] = loads(r["source"], {})
            r["required"] = bool(r["required"])
        return rows

    # ------------------------------------------------------------------ answer memory
    def save_memory(self, rec: dict[str, Any]) -> str:
        now = self.clock()
        mem_id = rec.get("id") or new_id("mem")

        def op(c: sqlite3.Connection) -> str:
            existing = c.execute(
                "SELECT id FROM answer_memory WHERE normalized=? AND company_norm=? AND options_sig=? AND profile_hash=? "
                "AND value=?", (rec["normalized"], rec.get("company_norm", ""), rec.get("options_sig", ""),
                                rec["profile_hash"], dumps(rec["value"]))).fetchone()
            if existing:
                c.execute("UPDATE answer_memory SET use_count=use_count+1, updated_at=?, submitted=MAX(submitted, ?) WHERE id=?",
                          (now, int(bool(rec.get("submitted"))), existing["id"]))
                return str(existing["id"])
            c.execute(
                "INSERT INTO answer_memory(id, normalized, question, qclass, catalog_id, company_norm, role, options_sig, value, "
                "method, facts_used, template, research_ids, limits, profile_hash, application_id, submitted, use_count, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mem_id, rec["normalized"], rec["question"], rec["qclass"], rec.get("catalog_id"),
                 rec.get("company_norm", ""), rec.get("role"), rec.get("options_sig", ""), dumps(rec["value"]),
                 rec["method"], dumps(rec.get("facts_used") or []), rec.get("template"),
                 dumps(rec.get("research_ids") or []), dumps(rec.get("limits") or {}), rec["profile_hash"],
                 rec.get("application_id"), int(bool(rec.get("submitted"))), 1, now, now))
            return mem_id

        return self.write(op)

    def mark_memory_submitted(self, app_id: str) -> None:
        self.write(lambda c: c.execute("UPDATE answer_memory SET submitted=1 WHERE application_id=?", (app_id,)))

    def memory_candidates(self, *, qclass: str | None = None, since: float = 0, limit: int = 5000) -> list[dict[str, Any]]:
        sql = "SELECT * FROM answer_memory WHERE updated_at>=?"
        params: list[Any] = [since]
        if qclass:
            sql += " AND qclass=?"
            params.append(qclass)
        rows = self.query(sql + " ORDER BY updated_at DESC LIMIT ?", [*params, limit])
        for r in rows:
            r["value"] = loads(r["value"])
            r["facts_used"] = loads(r["facts_used"], [])
            r["research_ids"] = loads(r["research_ids"], [])
            r["limits"] = loads(r["limits"], {})
        return rows

    def memory_by_normalized(self, normalized: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM answer_memory WHERE normalized=? ORDER BY submitted DESC, updated_at DESC", (normalized,))
        for r in rows:
            r["value"] = loads(r["value"])
            r["facts_used"] = loads(r["facts_used"], [])
        return rows

    def save_user_answer(self, normalized: str, question: str, value: Any, company_norm: str = "") -> None:
        now = self.clock()
        self.write(lambda c: c.execute(
            "INSERT INTO user_answers(normalized, company_norm, question, value, created_at, updated_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(normalized, company_norm) DO UPDATE SET value=excluded.value, question=excluded.question, "
            "updated_at=excluded.updated_at", (normalized, company_norm, question, dumps(value), now, now)))

    def user_answers(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM user_answers ORDER BY updated_at DESC")
        for r in rows:
            r["value"] = loads(r["value"])
        return rows

    # ------------------------------------------------------------------ reasoning queue
    def enqueue_reasoning(self, *, kind: str, dedupe_key: str, payload: dict[str, Any], app_id: str,
                          field_key: str = "") -> dict[str, Any]:
        """Create (or join) an open reasoning item. Identical concurrent questions share one item."""
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute(
                "SELECT * FROM reasoning_items WHERE dedupe_key=? AND status IN ('PENDING','LEASED')", (dedupe_key,)
            ).fetchone()
            joined = row is not None
            if row is None:
                item_id = new_id("rsn")
                c.execute(
                    "INSERT INTO reasoning_items(id, kind, dedupe_key, status, payload, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'PENDING', ?, ?, ?)", (item_id, kind, dedupe_key, dumps(payload), now, now))
            else:
                item_id = row["id"]
            c.execute("INSERT OR IGNORE INTO reasoning_waiters(item_id, application_id, field_key) VALUES (?, ?, ?)",
                      (item_id, app_id, field_key))
            return {"id": item_id, "joined": joined}

        return self.write(op)

    def lease_reasoning(self, owner: str, *, limit: int, lease_seconds: float, kinds: list[str] | None = None,
                        application_id: str | None = None) -> list[dict[str, Any]]:
        now = self.clock()

        def op(c: sqlite3.Connection) -> list[dict[str, Any]]:
            sql = ("SELECT DISTINCT r.* FROM reasoning_items r " +
                   ("JOIN reasoning_waiters w ON w.item_id=r.id " if application_id else "") +
                   "WHERE (r.status='PENDING' OR (r.status='LEASED' AND r.lease_until < ?)) "
                   "AND NOT EXISTS (SELECT 1 FROM reasoning_routes t WHERE t.item_id=r.id AND t.available_at>?)")
            params: list[Any] = [now, now]
            if application_id:
                sql += " AND w.application_id=?"
                params.append(application_id)
            if kinds:
                sql += f" AND r.kind IN ({','.join('?' for _ in kinds)})"
                params += kinds
            rows = c.execute(sql + " ORDER BY r.created_at LIMIT ?", [*params, limit]).fetchall()
            out = []
            for row in rows:
                c.execute("UPDATE reasoning_items SET status='LEASED', lease_owner=?, lease_until=?, attempts=attempts+1, "
                          "updated_at=? WHERE id=?", (owner, now + lease_seconds, now, row["id"]))
                item = dict(row)
                item["payload"] = loads(item["payload"], {})
                item["waiters"] = [dict(w) for w in c.execute(
                    "SELECT application_id, field_key FROM reasoning_waiters WHERE item_id=?", (row["id"],)).fetchall()]
                out.append(item)
            return out

        return self.write(op)

    def requeue_reasoning(self, item_id: str, payload: dict[str, Any]) -> None:
        """Put a rejected item back in the queue with its rejection history."""
        self.write(lambda c: c.execute(
            "UPDATE reasoning_items SET status='PENDING', payload=?, lease_owner=NULL, lease_until=NULL, updated_at=? "
            "WHERE id=? AND status IN ('PENDING','LEASED')", (dumps(payload), self.clock(), item_id)))

    def return_reasoning(self, ids: list[str]) -> None:
        """Release leased items that were not delivered (deferred or over the batch size)."""
        if ids:
            self.write(lambda c: c.execute(
                f"UPDATE reasoning_items SET status='PENDING', lease_owner=NULL, lease_until=NULL "
                f"WHERE status='LEASED' AND id IN ({','.join('?' for _ in ids)})", ids))

    def pending_reasoning_count(self, application_id: str | None = None) -> int:
        sql = "SELECT COUNT(DISTINCT r.id) AS n FROM reasoning_items r"
        params: list[Any] = []
        if application_id:
            sql += " JOIN reasoning_waiters w ON w.item_id=r.id"
        sql += " WHERE r.status IN ('PENDING','LEASED')"
        if application_id:
            sql += " AND w.application_id=?"
            params.append(application_id)
        rows = self.query(sql, params)
        return int(rows[0]["n"])

    def reasoning_items(self, *, status: str | None = None, ids: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reasoning_items WHERE 1=1"
        params: list[Any] = []
        if status:
            statuses = [s.strip().upper() for s in status.split(",")]
            sql += f" AND status IN ({','.join('?' for _ in statuses)})"
            params += statuses
        if ids is not None:
            if not ids:
                return []
            sql += f" AND id IN ({','.join('?' for _ in ids)})"
            params += ids
        rows = self.query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, limit])
        for r in rows:
            r["payload"] = loads(r["payload"], {})
            r["result"] = loads(r["result"])
            r["waiters"] = self.query("SELECT application_id, field_key FROM reasoning_waiters WHERE item_id=?", (r["id"],))
        return rows

    def complete_reasoning(self, item_id: str, result: dict[str, Any], *, status: str = "DONE") -> list[dict[str, Any]]:
        """Store the result and return waiters. Idempotent: completing twice keeps the first result."""
        now = self.clock()

        def op(c: sqlite3.Connection) -> list[dict[str, Any]]:
            row = c.execute("SELECT status FROM reasoning_items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown reasoning item {item_id}")
            if row["status"] in ("PENDING", "LEASED"):
                c.execute("UPDATE reasoning_items SET status=?, result=?, completed_at=?, updated_at=?, lease_until=NULL "
                          "WHERE id=?", (status, dumps(result), now, now, item_id))
            return [dict(w) for w in c.execute(
                "SELECT application_id, field_key FROM reasoning_waiters WHERE item_id=?", (item_id,)).fetchall()]

        return self.write(op)

    def reasoning_for_application(self, app_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT r.*, w.field_key AS waiter_field FROM reasoning_items r JOIN reasoning_waiters w ON w.item_id=r.id "
            "WHERE w.application_id=? ORDER BY r.created_at", (app_id,))
        for r in rows:
            r["payload"] = loads(r["payload"], {})
            r["result"] = loads(r["result"])
        return rows

    def cancel_reasoning_for(self, app_id: str) -> None:
        def op(c: sqlite3.Connection) -> None:
            items = [r["item_id"] for r in c.execute("SELECT item_id FROM reasoning_waiters WHERE application_id=?", (app_id,))]
            c.execute("DELETE FROM reasoning_waiters WHERE application_id=?", (app_id,))
            for item_id in items:
                left = c.execute("SELECT COUNT(*) FROM reasoning_waiters WHERE item_id=?", (item_id,)).fetchone()[0]
                if left == 0:
                    c.execute("UPDATE reasoning_items SET status='CANCELLED', updated_at=? WHERE id=? "
                              "AND status IN ('PENDING','LEASED')", (self.clock(), item_id))

        self.write(op)

    # ------------------------------------------------------------------ research cache
    def save_research(self, *, company: str, company_norm: str, topic: str, summary: str, facts: list[str],
                      sources: list[dict[str, Any]], origin: str, ttl: float) -> str:
        now = self.clock()
        rid = new_id("res")
        self.write(lambda c: c.execute(
            "INSERT INTO company_research(id, company_norm, company, topic, summary, facts, sources, origin, retrieved_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (rid, company_norm, company, topic, summary, dumps(facts), dumps(sources), origin, now, now + ttl)))
        return rid

    def research(self, company_norm: str, *, topic: str | None = None, fresh_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM company_research WHERE company_norm=?"
        params: list[Any] = [company_norm]
        if topic:
            sql += " AND topic=?"
            params.append(topic)
        if fresh_only:
            sql += " AND expires_at>?"
            params.append(self.clock())
        rows = self.query(sql + " ORDER BY retrieved_at DESC", params)
        for r in rows:
            r["facts"] = loads(r["facts"], [])
            r["sources"] = loads(r["sources"], [])
        return rows

    def research_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        rows = self.query(f"SELECT * FROM company_research WHERE id IN ({','.join('?' for _ in ids)})", ids)
        for r in rows:
            r["facts"] = loads(r["facts"], [])
            r["sources"] = loads(r["sources"], [])
        return rows

    # ------------------------------------------------------------------ domain health
    def record_domain_result(self, domain: str | None, ok: bool, *, threshold: int, cooldown: float,
                             error: str | None = None) -> dict[str, Any] | None:
        if not domain:
            return None
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute("SELECT * FROM domain_health WHERE domain=?", (domain,)).fetchone()
            failures = 0 if ok else (row["consecutive_failures"] if row else 0) + 1
            until = (row["cooldown_until"] if row else 0) if ok else (now + cooldown if failures >= threshold else 0)
            c.execute(
                "INSERT INTO domain_health(domain, consecutive_failures, cooldown_until, last_error, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(domain) DO UPDATE SET consecutive_failures=excluded.consecutive_failures, "
                "cooldown_until=excluded.cooldown_until, last_error=excluded.last_error, updated_at=excluded.updated_at",
                (domain, failures, until, error, now))
            return {"domain": domain, "consecutive_failures": failures, "cooldown_until": until}

        return self.write(op)

    def domain_health(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM domain_health ORDER BY updated_at DESC LIMIT 200")

    # ------------------------------------------------------------------ accounts
    def get_account(self, account_key: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM accounts WHERE account_key=?", (account_key,))
        return rows[0] if rows else None

    def upsert_account(self, account_key: str, *, domain: str, username: str, secret_ref: str, status: str,
                       application_id: str | None = None, notes: str | None = None) -> dict[str, Any]:
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            c.execute(
                "INSERT INTO accounts(id, account_key, domain, username, secret_ref, status, application_id, notes, created_at, updated_at, verified_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_key) DO UPDATE SET status=excluded.status, "
                "username=excluded.username, secret_ref=excluded.secret_ref, notes=COALESCE(excluded.notes, accounts.notes), "
                "updated_at=excluded.updated_at, verified_at=COALESCE(excluded.verified_at, accounts.verified_at)",
                (new_id("acct"), account_key, domain, username, secret_ref, status, application_id, notes, now, now,
                 now if status == "active" else None))
            return dict(c.execute("SELECT * FROM accounts WHERE account_key=?", (account_key,)).fetchone())

        return self.write(op)

    def accounts(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM accounts ORDER BY updated_at DESC")

    # ------------------------------------------------------------------ submissions
    def reserve_submission(self, app_id: str, *, worker_id: str, token: str) -> dict[str, Any]:
        """The double-submission guard. Atomically (single IMMEDIATE transaction):

        * fence the lease,
        * refuse if this application already has a submission intent,
        * refuse if an equivalent job (same normalized company+title, or same ATS job key)
          is SUBMITTING/SUBMITTED/SUBMISSION_UNCONFIRMED,
        * record the intent and move the application to SUBMITTING.

        ``submissions.job_id`` is UNIQUE, so even a bug elsewhere cannot record two
        intents for one job.
        """
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            app = self._fence(c, app_id, worker_id, token)
            job = self._job_row(c.execute("SELECT * FROM jobs WHERE id=?", (app["job_id"],)).fetchone())
            assert job is not None
            existing = c.execute("SELECT * FROM submissions WHERE application_id=?", (app_id,)).fetchone()
            if existing and existing["status"] != "ABORTED":
                raise DuplicateSubmission("this application already has a submission record", [dict(existing)])
            dedupe_key = f"{job['company_norm']}|{job['title_norm']}"
            conflicts = [dict(r) for r in c.execute(
                "SELECT s.*, j.company, j.title FROM submissions s JOIN jobs j ON j.id=s.job_id "
                "WHERE s.application_id<>? AND s.status IN ('SUBMITTING','SUBMITTED','SUBMISSION_UNCONFIRMED') "
                "AND (s.dedupe_key=? OR (s.ats_job_key IS NOT NULL AND s.ats_job_key=?))",
                (app_id, dedupe_key, job.get("ats_job_key"))).fetchall()]
            if conflicts:
                raise DuplicateSubmission("an equivalent job was already submitted", conflicts)
            if existing:
                history = loads(existing["evidence"], {}) or {}
                previous = list(history.pop("previous_attempts", []))
                previous.append({**history, "status": existing["status"], "started_at": existing["started_at"],
                                 "finished_at": existing["finished_at"]})
                c.execute("UPDATE submissions SET status='SUBMITTING', worker_id=?, started_at=?, finished_at=NULL, "
                          "evidence=? WHERE application_id=?",
                          (worker_id, now, dumps({"previous_attempts": previous[-10:]}), app_id))
            else:
                c.execute(
                    "INSERT INTO submissions(application_id, job_id, dedupe_key, ats_job_key, status, worker_id, started_at) "
                    "VALUES (?, ?, ?, ?, 'SUBMITTING', ?, ?)",
                    (app_id, app["job_id"], dedupe_key, job.get("ats_job_key"), worker_id, now))
            validate_transition(app["state"], S.SUBMITTING)
            c.execute("UPDATE applications SET state='SUBMITTING', state_reason=NULL, priority=0, updated_at=? WHERE id=?",
                      (now, app_id))
            updated = self._app(c, app_id)
            self._event(c, updated, worker_id, "submission", "submission_reserved",
                        "Submission intent recorded (duplicate check passed)", {"dedupe_key": dedupe_key})
            return updated

        return self.write(op)

    def finish_submission(self, app_id: str, status: str, evidence: dict[str, Any], *, worker_id: str | None,
                          token: str | None) -> dict[str, Any]:
        """Record outcome: SUBMITTED, SUBMISSION_UNCONFIRMED, or ABORTED (page proved nothing
        was sent, e.g. validation errors after clicking submit → the app may retry)."""
        now = self.clock()

        def op(c: sqlite3.Connection) -> dict[str, Any]:
            app = self._fence(c, app_id, worker_id, token)
            row = c.execute("SELECT evidence FROM submissions WHERE application_id=?", (app_id,)).fetchone()
            previous = (loads(row["evidence"], {}) or {}).get("previous_attempts") if row else None
            merged = {**redact(evidence), **({"previous_attempts": previous} if previous else {})}
            c.execute("UPDATE submissions SET status=?, finished_at=?, evidence=? WHERE application_id=?",
                      (status, now, dumps(merged), app_id))
            return app

        return self.write(op)

    def submission(self, app_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM submissions WHERE application_id=?", (app_id,))
        if not rows:
            return None
        rows[0]["evidence"] = loads(rows[0]["evidence"], {})
        return rows[0]

    def find_equivalent_applications(self, job: dict[str, Any], *, exclude_app: str | None = None) -> list[dict[str, Any]]:
        """Applications for jobs equivalent to ``job`` that are submitted or in flight."""
        rows = self.query(
            "SELECT a.id, a.state, a.job_id, j.company, j.title, j.ats_job_key FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.id<>? AND ((j.company_norm=? AND j.title_norm=?) OR (j.ats_job_key IS NOT NULL AND j.ats_job_key=?))",
            (exclude_app or "", job["company_norm"], job["title_norm"], job.get("ats_job_key")))
        return rows

    # ------------------------------------------------------------------ model calls
    def record_model_call(self, *, provider: str, kind: str, items: int, application_ids: list[str],
                          request_chars: int, response_chars: int, duration_ms: int, ok: bool = True,
                          input_tokens: int | None = None, output_tokens: int | None = None,
                          error: str | None = None, model: str | None = None, estimated_usd: float = 0,
                          escalation_reason: str | None = None, usage_estimated: bool = False,
                          reservation_id: str | None = None) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute(
            "INSERT INTO model_calls(ts, provider, kind, items, application_ids, request_chars, response_chars, input_tokens, "
            "output_tokens, duration_ms, ok, error, model, estimated_usd, escalation_reason, usage_estimated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.clock(), provider, kind, items, dumps(application_ids), request_chars, response_chars, input_tokens,
             output_tokens, duration_ms, int(ok), error, model, estimated_usd, escalation_reason, int(usage_estimated)))
            if reservation_id:
                c.execute("UPDATE model_reservations SET status='DONE' WHERE id=?", (reservation_id,))
        self.write(op)

    def reserve_model_budget(self, *, provider: str, application_ids: list[str], amount: float,
                             daily_limit: float, application_limit: float, premium_limit: int) -> str:
        """Reserve worst-case cost before sending a request, atomically across processes.

        Unsettled crash reservations remain charged. A request can have reached a paid
        endpoint before process death; releasing those automatically would evade budgets.
        """
        def op(c: sqlite3.Connection) -> str:
            day = int(self.clock() // 86400) * 86400
            spent = c.execute("SELECT COALESCE(SUM(estimated_usd),0) FROM model_calls WHERE ts>=?", (day,)).fetchone()[0]
            held = c.execute("SELECT COALESCE(SUM(reserved_usd),0) FROM model_reservations WHERE status='PENDING' AND ts>=?", (day,)).fetchone()[0]
            if amount and spent + held + amount > daily_limit + 1e-12:
                raise ValueError("daily budget exhausted")
            for app_id in application_ids:
                totals = c.execute(
                    "SELECT COALESCE(SUM(estimated_usd / MAX(1,json_array_length(application_ids))),0), "
                    "SUM(CASE WHEN provider='openai' AND kind LIKE 'completion%' THEN 1 ELSE 0 END) "
                    "FROM model_calls WHERE EXISTS (SELECT 1 FROM json_each(application_ids) WHERE value=?)", (app_id,)).fetchone()
                pending = c.execute(
                    "SELECT COALESCE(SUM(reserved_usd / MAX(1,json_array_length(application_ids))),0), "
                    "SUM(CASE WHEN provider='openai' THEN 1 ELSE 0 END) FROM model_reservations "
                    "WHERE status='PENDING' AND EXISTS (SELECT 1 FROM json_each(application_ids) WHERE value=?)", (app_id,)).fetchone()
                if amount and totals[0] + pending[0] + amount / max(1, len(application_ids)) > application_limit + 1e-12:
                    raise ValueError("application budget exhausted")
                if provider == "openai" and (totals[1] or 0) + (pending[1] or 0) >= premium_limit:
                    raise ValueError("premium request limit exhausted")
            reservation = new_id("cost")
            c.execute("INSERT INTO model_reservations(id,ts,provider,application_ids,reserved_usd) VALUES (?,?,?,?,?)",
                      (reservation, self.clock(), provider, dumps(application_ids), amount))
            return reservation
        return self.write(op)

    def cost_totals(self) -> dict[str, Any]:
        return {
            "estimated_usd": self.query("SELECT COALESCE(SUM(estimated_usd),0) AS usd FROM model_calls")[0]["usd"],
            "pending_reserved_usd": self.query("SELECT COALESCE(SUM(reserved_usd),0) AS usd FROM model_reservations WHERE status='PENDING'")[0]["usd"],
            "by_provider_model": self.query("SELECT provider,model,SUM(estimated_usd) AS estimated_usd, SUM(input_tokens) AS input_tokens, "
                                            "SUM(output_tokens) AS output_tokens, COUNT(*) AS requests FROM model_calls "
                                            "WHERE kind LIKE 'completion%' GROUP BY provider,model"),
            "by_application": self.query("SELECT j.value AS application_id, SUM(m.estimated_usd / MAX(1,json_array_length(m.application_ids))) AS estimated_usd "
                                         "FROM model_calls m,json_each(m.application_ids) j GROUP BY j.value"),
        }

    # ------------------------------------------------------------------ ingest runs
    def start_ingest(self, mode: str, source: str) -> int:
        return self.write(lambda c: int(c.execute(
            "INSERT INTO ingest_runs(started_at, mode, source) VALUES (?, ?, ?)", (self.clock(), mode, source)).lastrowid or 0))

    def finish_ingest(self, run_id: int, **values: Any) -> None:
        values["finished_at"] = self.clock()
        assignments = ",".join(f"{k}=?" for k in values)
        self.write(lambda c: c.execute(f"UPDATE ingest_runs SET {assignments} WHERE id=?", [*values.values(), run_id]))

    def ingest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ reporting
    def state_counts(self) -> dict[str, int]:
        return {r["state"]: r["n"] for r in self.query("SELECT state, COUNT(*) AS n FROM applications GROUP BY state")}

    def today_counts(self, since: float) -> dict[str, int]:
        rows = self.query(
            "SELECT state, COUNT(*) AS n FROM applications WHERE updated_at>=? GROUP BY state", (since,))
        counts = {r["state"]: r["n"] for r in rows}
        active = sum(v for k, v in counts.items() if k not in {s.value for s in PARKED})
        return {
            "submitted": counts.get("SUBMITTED", 0), "skipped": counts.get("SKIPPED", 0),
            "ineligible": counts.get("INELIGIBLE", 0),
            "needs_user": counts.get("NEEDS_USER", 0) + counts.get("WAITING_FOR_CAPTCHA", 0),
            "failed": counts.get("FAILED", 0), "unconfirmed": counts.get("SUBMISSION_UNCONFIRMED", 0),
            "active": active,
        }

    def search_applications(self, *, q: str | None = None, company: str | None = None, role: str | None = None,
                            state: str | None = None, ats: str | None = None, resume: str | None = None,
                            failure: bool = False, date_from: float | None = None, date_to: float | None = None,
                            limit: int = 50, offset: int = 0, order: str = "updated_at DESC") -> tuple[list[dict[str, Any]], int]:
        where: list[str] = ["1=1"]
        params: list[Any] = []
        if q:
            where.append("(j.company LIKE ? OR j.title LIKE ? OR a.id LIKE ? OR j.locations LIKE ?)")
            params += [f"%{q}%"] * 4
        if company:
            where.append("j.company LIKE ?")
            params.append(f"%{company}%")
        if role:
            where.append("j.title LIKE ?")
            params.append(f"%{role}%")
        if state:
            states = [s.strip().upper() for s in state.split(",") if s.strip()]
            where.append(f"a.state IN ({','.join('?' for _ in states)})")
            params += states
        if ats:
            where.append("COALESCE(a.ats, j.ats) LIKE ?")
            params.append(f"%{ats}%")
        if resume:
            where.append("a.resume LIKE ?")
            params.append(f"%{resume}%")
        if failure:
            where.append("a.state IN ('FAILED','NEEDS_USER','WAITING_FOR_CAPTCHA','SUBMISSION_UNCONFIRMED')")
        if date_from:
            where.append("a.updated_at>=?")
            params.append(date_from)
        if date_to:
            where.append("a.updated_at<=?")
            params.append(date_to)
        allowed_orders = {"updated_at DESC", "updated_at ASC", "created_at DESC", "priority ASC"}
        order = order if order in allowed_orders else "updated_at DESC"
        base = f"FROM applications a JOIN jobs j ON j.id=a.job_id WHERE {' AND '.join(where)}"
        total = self.query(f"SELECT COUNT(*) AS n {base}", params)[0]["n"]
        rows = self.query(
            f"SELECT a.*, j.company, j.title, j.locations, j.simplify_url, j.apply_url, j.direct_url, j.ats AS job_ats "
            f"{base} ORDER BY a.{order} LIMIT ? OFFSET ?", [*params, limit, offset])
        for r in rows:
            r["data"] = loads(r["data"], {})
            r["locations"] = loads(r["locations"], [])
        return rows, int(total)

    def needs_user(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT a.*, j.company, j.title FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE a.state IN ('WAITING_FOR_CAPTCHA','NEEDS_USER','READY_TO_SUBMIT') ORDER BY a.updated_at")
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    def claimable_counts(self) -> dict[str, int]:
        now = self.clock()
        prep = self.query(
            f"SELECT COUNT(*) AS n FROM applications WHERE state IN ({','.join('?' for _ in PREP_STATES)})",
            [s.value for s in PREP_STATES])[0]["n"]
        browser = self.query(
            f"SELECT COUNT(*) AS n FROM applications WHERE state IN ({','.join('?' for _ in BROWSER_STATES)}) "
            "AND blocked_on IS NULL AND available_at<=?", [*[s.value for s in BROWSER_STATES], now])[0]["n"]
        blocked = self.query("SELECT COUNT(*) AS n FROM applications WHERE blocked_on IS NOT NULL")[0]["n"]
        return {"prep": prep, "browser": browser, "blocked": blocked}
