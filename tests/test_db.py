import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobpilot.db import Database, normalize_url
from jobpilot.state import InvalidTransition, LeaseLost


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "jobs.sqlite3", clock=Clock())
    database.initialize()
    database.set_control("RUNNING")
    return database


def job(index=1, domain=None):
    return {
        "company": f"Company {index}",
        "title": "Software Engineer Intern",
        "simplify_url": f"https://simplify.jobs/p/role-{index}?utm_source=repo",
        "application_url": f"https://{domain or f'company{index}.example'}/jobs/{index}",
        "domain": domain or f"company{index}.example",
        "description": "Internship",
        "metadata": {},
    }


def claimed(db, index=1):
    db.ingest([job(index)])
    return db.claim(f"worker-{index}")


def fence(app):
    return {"worker_id": app["worker_id"], "lease_token": app["lease_token"]}


def ready_to_submit(db):
    app = claimed(db)
    for state in ("OPENING", "FILLING", "VALIDATING"):
        db.transition(app["id"], state, **fence(app))
    return app


def parallel(count, fn):
    barrier = threading.Barrier(count)

    def run(index):
        barrier.wait()
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(run, range(count)))


def test_migrations_idempotent_wal_and_foreign_keys(db):
    db.initialize()
    connection = db._connect()
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO applications(id,job_id,state,created_at,updated_at) VALUES('bad','missing','QUEUED',1,1)"
            )
    finally:
        connection.close()


def test_parallel_claim_exactly_once(db):
    db.ingest([job()])
    results = parallel(8, lambda index: db.claim(f"worker-{index}"))
    assert sum(app is not None for app in results) == 1
    assert len(db.applications()) == 1


def test_parallel_duplicate_ingestion(db):
    results = parallel(6, lambda _: db.ingest([job()]))
    assert sum(result["discovered"] for result in results) == 1
    assert len(db.applications()) == 1


def test_normalized_url_and_simplify_identity(db):
    assert (
        normalize_url("http://www.example.com/jobs/12/?utm_campaign=foo&b=2&a=1#form")
        == "https://example.com/jobs/12?a=1&b=2"
    )
    db.ingest([job()])
    changed = job()
    changed["simplify_url"] = "https://simplify.jobs/p/role-1/?utm_campaign=changed"
    changed["application_url"] += "?source=board"
    assert db.ingest([changed])["discovered"] == 0
    assert len(db.applications()) == 1


def test_ambiguous_duplicate_never_queues_second_submission(db):
    db.ingest([job()])
    ambiguous = job()
    ambiguous["simplify_url"] = "https://simplify.jobs/p/different"
    ambiguous["application_url"] = "https://company1.example/different"
    result = db.ingest([ambiguous])
    assert result["ambiguous"] == 1
    app = db.applications()[0]
    assert db.events(app["id"])[-1]["event_type"] == "DUPLICATE_AMBIGUITY"
    assert len(db.queue()) == 1


def test_domain_limit_and_redirect_domain_acquisition(db):
    db.ingest([job(1, "same.example"), job(2, "same.example"), job(3)])
    first, second = db.claim("first"), db.claim("second")
    assert {first["job"]["domain"], second["job"]["domain"]} == {"same.example", "company3.example"}
    assert db.claim("third") is None
    assert db.acquire_domain(first["id"], "ats.example", **fence(first))
    assert not db.acquire_domain(second["id"], "ats.example", **fence(second))


def test_crash_recovery_fences_old_worker_and_preserves_state(db):
    old = claimed(db)
    db.transition(old["id"], "OPENING", **fence(old))
    db.clock.now += 121
    assert db.recover_expired() == 1
    new = db.claim("replacement")
    assert new["id"] == old["id"]
    assert new["state"] == "OPENING"
    assert new["lease_token"] != old["lease_token"]
    with pytest.raises(LeaseLost):
        db.transition(old["id"], "FILLING", **fence(old))
    with pytest.raises(LeaseLost):
        db.heartbeat(old["id"], **fence(old))


def test_submission_race_only_one_reservation(db):
    app = ready_to_submit(db)
    results = parallel(8, lambda _: db.reserve_submission(app["id"], **fence(app)))
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert db.get_application(app["id"])["state"] == "SUBMITTING"


def test_crash_after_intent_never_retries_submission(db):
    app = ready_to_submit(db)
    assert db.reserve_submission(app["id"], **fence(app))
    db.clock.now += 121
    restarted = Database(db.path, clock=db.clock)
    restarted.initialize()
    restarted.recover_expired()
    assert restarted.get_application(app["id"])["state"] == "SUBMISSION_UNCONFIRMED"
    assert restarted.claim("new-worker") is None
    with pytest.raises(InvalidTransition):
        restarted.resume_application(app["id"])
    assert (
        restarted.finish_submission(app["id"], True, {"receipt": "Applicant 123"})["state"]
        == "SUBMITTED"
    )


def test_submission_requires_evidence_and_intent(db):
    app = ready_to_submit(db)
    with pytest.raises(InvalidTransition):
        db.finish_submission(app["id"], True, {"text": "Thanks"})
    with pytest.raises(InvalidTransition):
        db.transition(app["id"], "SUBMITTED", **fence(app))
    assert db.reserve_submission(app["id"], **fence(app))
    with pytest.raises(ValueError):
        db.finish_submission(app["id"], True, {})
    assert (
        db.finish_submission(app["id"], False, {"clicked": True})["state"]
        == "SUBMISSION_UNCONFIRMED"
    )


def test_pause_persists_prevents_claim_and_submit(db):
    app = ready_to_submit(db)
    db.ingest([job(2)])
    db.set_control("PAUSED")
    assert db.claim("other") is None
    assert not db.reserve_submission(app["id"], **fence(app))
    db.heartbeat(app["id"], **fence(app))
    restarted = Database(db.path, clock=db.clock)
    restarted.initialize()
    assert restarted.get_control() == "PAUSED"
    restarted.set_control("RUNNING")
    assert restarted.claim("other") is not None


def test_captcha_and_failure_do_not_block_other_workers(db):
    db.ingest([job(1), job(2), job(3)])
    captcha = db.claim("captcha")
    db.transition(captcha["id"], "OPENING", **fence(captcha))
    db.transition(captcha["id"], "NEEDS_USER", details={"reason": "CAPTCHA"}, **fence(captcha))
    db.release(captcha["id"], **fence(captcha))
    failed = db.claim("failed")
    db.transition(failed["id"], "FAILED", **fence(failed))
    db.release(failed["id"], **fence(failed))
    success = db.claim("success")
    assert success is not None
    db.resume_application(captcha["id"])
    resumed = db.claim("resumed")
    assert resumed["id"] == captcha["id"]
    assert resumed["state"] == "OPENING"


def test_reasoning_parks_worker_and_requeues_without_reset(db):
    app = claimed(db)
    db.transition(app["id"], "OPENING", **fence(app))
    db.transition(app["id"], "ANSWERING", **fence(app))
    db.release(app["id"], **fence(app), blocked=True)
    assert db.claim("other") is None
    db.requeue(app["id"])
    assert db.claim("other")["state"] == "ANSWERING"


def test_ordered_audit_and_answer_memory_write_contention(db):
    app = claimed(db)
    parallel(
        12,
        lambda index: db.event(
            app["id"],
            "TEST",
            {"index": index, "password": "do-not-log"},
            worker_id=f"audit-{index}",
        ),
    )
    events = db.events(app["id"])
    assert [event["id"] for event in events] == sorted(event["id"] for event in events)
    assert len({event["id"] for event in events}) == len(events)
    assert all(event["job_id"] == app["job_id"] for event in events)
    assert events[-1]["data"]["password"] == "[REDACTED]"
    record = {
        "question": "Why this company?",
        "profile_hash": "v1",
        "company": "Company 1",
        "answer": "A truthful response",
    }
    ids = parallel(8, lambda _: db.save_answer_memory(record))
    assert len(set(ids)) == 1
    assert len(db.find_answers("v1", company="Company 1")) == 1
    assert db.find_answers("changed-profile") == []
    db.record_answer(
        app["id"],
        "University?",
        {"value": "Example", "method": "deterministic", "facts_used": ["education.school"]},
        **fence(app),
    )
    assert db.answers(app["id"])[0]["answer"]["method"] == "deterministic"


def test_research_single_flight_and_stale_completion_fencing(db):
    results = parallel(
        8, lambda index: db.cache_acquire("research", "company", f"owner-{index}", 10)
    )
    assert results.count(True) == 1
    first_owner = f"owner-{results.index(True)}"
    db.clock.now += 11
    assert db.cache_acquire("research", "company", "replacement", 10)
    assert not db.cache_complete("research", "company", first_owner, {"research": "stale"}, 30)
    assert db.cache_complete(
        "research", "company", "replacement", {"sources": ["https://example.com"]}, 30
    )
    assert not db.cache_acquire("research", "company", "third")
    assert db.cache_get("research", "company")["sources"] == ["https://example.com"]
    db.clock.now += 31
    assert db.cache_get("research", "company") is None


def test_new_mode_does_not_repeat_considered_jobs_and_material_change_reconsiders(db):
    app = claimed(db)
    db.transition(app["id"], "CHECKING_ELIGIBILITY", **fence(app))
    db.transition(app["id"], "INELIGIBLE", **fence(app))
    db.release(app["id"], **fence(app))
    assert db.ingest([job()], mode="new_jobs")["queued"] == 0
    assert db.claim("other") is None
    updated = job() | {"description": "Now accepting undergraduate applicants"}
    assert db.ingest([updated], mode="new_jobs")["queued"] == 1
    assert db.claim("other")["state"] == "QUEUED"


def test_shared_reasoning_tasks_are_deduplicated_and_completion_is_idempotent(db):
    tasks = parallel(
        4, lambda _: db.enqueue_reasoning("profile1:question1", {"question": "Why us?"})
    )
    assert len({task["id"] for task in tasks}) == 1
    task_id = tasks[0]["id"]
    db.complete_reasoning(task_id, {"answer": "First"})
    db.complete_reasoning(task_id, {"answer": "Duplicate"})
    assert db.get_reasoning(task_id)["result"]["answer"] == "First"
    assert db.get_reasoning() == []


def test_memory_database_and_checkpoint_metadata():
    database = Database(":memory:")
    try:
        database.initialize()
        database.meta_set("source_checkpoint", {"commit": "abc"})
        assert database.meta_get("source_checkpoint") == {"commit": "abc"}
        assert database.meta_get("missing", 42) == 42
        assert database.get_control() == "STOPPED"
    finally:
        database.close()
