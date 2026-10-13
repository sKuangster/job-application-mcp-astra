import threading
import time

import pytest
from conftest import make_job

from applypilot.db import Database, DuplicateSubmission
from applypilot.ingest.listings import JobRecord
from applypilot.state import ALLOWED, InvalidTransition, LeaseLost, S, can_transition


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def cdb(tmp_path):
    clock = Clock()
    database = Database(tmp_path / "c.sqlite3", clock=clock)
    database.initialize()
    database.clock_obj = clock
    return database


def test_migrations_idempotent(tmp_path):
    d = Database(tmp_path / "m.sqlite3")
    d.initialize()
    d.initialize()
    assert d.schema_version() == 2
    assert d.meta_get("control") == "STOPPED"


def test_state_machine_rules():
    assert can_transition("QUEUED", "CHECKING_ELIGIBILITY")
    assert not can_transition("SUBMITTED", "QUEUED")
    assert not can_transition("SUBMITTING", "SKIPPED")  # never abandon an in-flight submission silently
    assert can_transition("NEEDS_USER", "APPLYING")
    assert can_transition("SUBMITTING", "WAITING_FOR_CAPTCHA")
    assert can_transition("WAITING_FOR_CAPTCHA", "SUBMITTING")
    assert all(s in ALLOWED for s in S)


def test_job_identity_survives_url_changes(cdb):
    rec = JobRecord("Acme", "SWE Intern", ["NYC"], "aaaaaaaa-1111-2222-3333-444444444444", None,
                    "https://job-boards.greenhouse.io/acme/jobs/1?utm_source=Simplify")
    first = cdb.upsert_jobs([rec.to_db()], batch="baseline")
    rec2 = JobRecord("Acme, Inc.", "SWE Intern", ["NYC"], "aaaaaaaa-1111-2222-3333-444444444444", None,
                     "https://boards.greenhouse.io/acme/jobs/1")
    second = cdb.upsert_jobs([rec2.to_db()], batch="incremental")
    assert first["new"] == 1 and second["new"] == 0
    # A second listing for the same ATS posting (different Simplify id) collapses into the same job.
    rec3 = JobRecord("Acme", "Software Engineering Intern", ["NYC"], "bbbbbbbb-1111-2222-3333-444444444444", None,
                     "https://job-boards.greenhouse.io/acme/jobs/1")
    third = cdb.upsert_jobs([rec3.to_db()], batch="incremental")
    assert third["new"] == 0 and third["duplicate_listings"]
    assert cdb.job_counts()["total"] == 1


def test_backlog_vs_new_jobs_queueing(cdb):
    make_job(cdb, company="Old", url="https://a.example/1", batch="baseline")
    make_job(cdb, company="New", url="https://a.example/2", batch="incremental")
    assert cdb.queue_jobs(mode="new_jobs") == 1
    assert cdb.queue_jobs(mode="backlog") == 1
    assert cdb.queue_jobs(mode="backlog") == 0  # idempotent: never re-queued
    assert len(cdb.query("SELECT * FROM applications")) == 2


def _queued_app(db, **kw):
    job_id = make_job(db, **kw)
    app, _ = db.create_application(job_id, mode="backlog")
    return app


def test_two_workers_never_claim_the_same_job(tmp_path):
    db = Database(tmp_path / "race.sqlite3")
    db.initialize()
    for i in range(30):
        _queued_app(db, company=f"C{i}", url=f"https://x{i}.example/job")
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        while True:
            app = db.claim(f"w{n}", "prep", lease_seconds=60)
            if app is None:
                return
            with lock:
                claimed.append(app["id"])

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 30 and len(set(claimed)) == 30


def test_lease_expiry_recovers_crashed_worker(cdb):
    app = _queued_app(cdb)
    first = cdb.claim("w1", "prep", lease_seconds=30)
    assert first and cdb.claim("w2", "prep", lease_seconds=30) is None
    cdb.clock_obj.t += 31  # w1 "crashed": no heartbeat
    second = cdb.claim("w2", "prep", lease_seconds=30)
    assert second and second["id"] == app["id"]
    # The crashed worker's writes are fenced off.
    with pytest.raises(LeaseLost):
        cdb.transition(app["id"], S.CHECKING_ELIGIBILITY, worker_id="w1", token=first["lease_token"])
    cdb.transition(app["id"], S.CHECKING_ELIGIBILITY, worker_id="w2", token=second["lease_token"])
    assert any(e["event_type"] == "claimed" and "recovered" in e["message"] for e in cdb.events(app["id"]))


def test_heartbeat_extends_lease(cdb):
    _queued_app(cdb)
    app = cdb.claim("w1", "prep", lease_seconds=30)
    cdb.clock_obj.t += 20
    assert cdb.heartbeat(app["id"], "w1", app["lease_token"], 30)
    cdb.clock_obj.t += 20
    assert cdb.claim("w2", "prep", lease_seconds=30) is None


def test_per_domain_concurrency(cdb):
    a = _queued_app(cdb, company="A", url="https://wd.example/a")
    b = _queued_app(cdb, company="B", url="https://wd.example/b")
    for app in (a, b):
        cdb.update_application(app["id"], state=S.READY, domain="same.example")
    assert cdb.claim("w1", "browser", lease_seconds=30, per_domain=1)
    assert cdb.claim("w2", "browser", lease_seconds=30, per_domain=1) is None
    assert cdb.claim("w2", "browser", lease_seconds=30, per_domain=2)


def test_domain_cooldown_blocks_claims(cdb):
    app = _queued_app(cdb)
    cdb.update_application(app["id"], state=S.READY, domain="flaky.example")
    for _ in range(3):
        cdb.record_domain_result("flaky.example", False, threshold=3, cooldown=600)
    assert cdb.claim("w", "browser", lease_seconds=30) is None
    cdb.clock_obj.t += 601
    assert cdb.claim("w", "browser", lease_seconds=30)


def test_tab_budget_limits_new_browser_work(cdb):
    a = _queued_app(cdb, company="A", url="https://a.example/1")
    b = _queued_app(cdb, company="B", url="https://b.example/1")
    cdb.update_application(a["id"], state=S.ANSWERING, blocked_on="reasoning", tab_id=7)  # holds a tab while parked
    cdb.update_application(b["id"], state=S.READY)
    assert cdb.claim("w", "browser", lease_seconds=30, max_tabs=1) is None
    assert cdb.claim("w", "browser", lease_seconds=30, max_tabs=2)


def _to_validating(db, app, worker="w1"):
    db.update_application(app["id"], state=S.VALIDATING)
    claimed = db.claim(worker, "browser", lease_seconds=60, app_id=app["id"])
    assert claimed
    return claimed


def test_duplicate_submission_race(tmp_path):
    db = Database(tmp_path / "dup.sqlite3")
    db.initialize()
    a = _queued_app(db, company="Acme", title="SWE Intern", url="https://acme.example/jobs/1", locations=["NYC"])
    b = _queued_app(db, company="Acme Inc", title="SWE Intern", url="https://acme.example/jobs/2", locations=["SF"])
    la, lb = _to_validating(db, a, "w1"), _to_validating(db, b, "w2")
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def attempt(app, worker):
        barrier.wait()
        try:
            db.reserve_submission(app["id"], worker_id=worker, token=app["lease_token"])
            results[worker] = "reserved"
        except DuplicateSubmission:
            results[worker] = "duplicate"

    threads = [threading.Thread(target=attempt, args=(la, "w1")), threading.Thread(target=attempt, args=(lb, "w2"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["duplicate", "reserved"]


def test_submission_is_once_per_application(db):
    app = _queued_app(db)
    leased = _to_validating(db, app)
    db.reserve_submission(app["id"], worker_id="w1", token=leased["lease_token"])
    with pytest.raises((DuplicateSubmission, InvalidTransition)):
        db.reserve_submission(app["id"], worker_id="w1", token=leased["lease_token"])
    db.finish_submission(app["id"], "SUBMITTED", {"ok": True}, worker_id="w1", token=leased["lease_token"])
    with pytest.raises(InvalidTransition):
        db.user_action(app["id"], S.QUEUED, reason="retry")


def test_aborted_submission_can_be_retried(db):
    app = _queued_app(db)
    leased = _to_validating(db, app)
    db.reserve_submission(app["id"], worker_id="w1", token=leased["lease_token"])
    db.finish_submission(app["id"], "ABORTED", {}, worker_id="w1", token=leased["lease_token"])
    db.transition(app["id"], S.VALIDATING, worker_id="w1", token=leased["lease_token"])
    db.reserve_submission(app["id"], worker_id="w1", token=leased["lease_token"])
    assert db.submission(app["id"])["status"] == "SUBMITTING"


def test_reasoning_dedupe_across_applications(db):
    a, b = _queued_app(db, company="A", url="https://a.example/1"), _queued_app(db, company="B", url="https://b.example/1")
    first = db.enqueue_reasoning(kind="question", dedupe_key="k1", payload={"question": "Q"}, app_id=a["id"], field_key="f1")
    second = db.enqueue_reasoning(kind="question", dedupe_key="k1", payload={"question": "Q"}, app_id=b["id"], field_key="f9")
    assert second["joined"] and first["id"] == second["id"]
    waiters = db.complete_reasoning(first["id"], {"value": "x"})
    assert {w["application_id"] for w in waiters} == {a["id"], b["id"]}
    # Completing twice is idempotent and keeps the first result.
    db.complete_reasoning(first["id"], {"value": "y"})
    assert db.reasoning_items(ids=[first["id"]])[0]["result"] == {"value": "x"}


def test_concurrent_answer_memory_writes(tmp_path):
    db = Database(tmp_path / "mem.sqlite3")
    db.initialize()

    def writer(n: int) -> None:
        for i in range(20):
            db.save_memory({"normalized": f"q{i % 5}", "question": f"Q{i % 5}", "qclass": "UNKNOWN", "value": f"v{n % 2}",
                            "method": "generated", "profile_hash": "h"})

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = db.query("SELECT normalized, value, use_count FROM answer_memory")
    assert len(rows) == 10  # 5 questions x 2 distinct values, deduplicated
    assert sum(r["use_count"] for r in rows) == 120


def test_lock_single_flight(tmp_path):
    db = Database(tmp_path / "lock.sqlite3")
    db.initialize()
    winners = []

    def grab(n):
        if db.acquire_lock("research:acme", f"o{n}", ttl=30):
            winners.append(n)

    threads = [threading.Thread(target=grab, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_events_are_per_application_and_ordered(db):
    a, b = _queued_app(db, company="A", url="https://a.example/1"), _queued_app(db, company="B", url="https://b.example/1")
    for i in range(20):
        db.event(a["id"], "test", "tick", f"a{i}")
        db.event(b["id"], "test", "tick", f"b{i}")
    ev = [e["message"] for e in db.events(a["id"]) if e["event_type"] == "tick"]
    assert ev == [f"a{i}" for i in range(20)]


def test_secrets_are_redacted_in_events(db):
    app = _queued_app(db)
    db.event(app["id"], "account", "x", "msg", {"password": "hunter2", "nested": {"token": "abc"}, "ok": 1})
    data = db.events(app["id"])[-1]["data"]
    assert data["password"] == "[REDACTED]" and data["nested"]["token"] == "[REDACTED]" and data["ok"] == 1


def test_write_contention_retries(tmp_path):
    db = Database(tmp_path / "busy.sqlite3", busy_timeout_ms=50)
    db.initialize()
    errors = []

    def writer(n):
        try:
            for i in range(30):
                db.meta_set(f"k{n}", i)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and time.time() - start < 30
