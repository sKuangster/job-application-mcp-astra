"""Regression tests for issues found in the adversarial review."""
import pytest
from conftest import edit_yaml, make_job, page_field

from applypilot.config import load_config
from applypilot.db import Database
from applypilot.engine.runner import _final_click, scrub_codes, sender_domains_for
from applypilot.models import PageSnapshot
from applypilot.questions.resolver import ResolveContext, Resolver

YN = ["Yes", "No"]


def resolve(cfg, db, label, ftype="radio", options=YN, company="Acme", **kw):
    job_id = make_job(db, company=company, url=f"https://{company.lower()}.example/{abs(hash(label))}", **kw)
    f = page_field(label=label, type=ftype, options=options if ftype in ("radio", "select", "combobox") else None,
                   required=True)
    return Resolver(cfg, db).resolve(f, ResolveContext(job=db.get_job(job_id), application_id="a"))


@pytest.mark.parametrize("label", [
    "Have you worked with Python before?", "Have you ever worked with distributed systems?",
    "Have you ever worked in a fast-paced environment?", "Have you ever been employed by a government agency?",
    "Are you currently or have you ever been employed by the federal government?",
])
def test_previously_employed_only_for_this_company(cfg, db, label):
    r = resolve(cfg, db, label)
    assert r.catalog_id != "previously_employed", label


def test_previously_employed_matches_company_reference(cfg, db):
    assert resolve(cfg, db, "Have you previously worked for Acme?").catalog_id == "previously_employed"
    assert resolve(cfg, db, "Have you ever worked for this company?").catalog_id == "previously_employed"


@pytest.mark.parametrize("label,ftype,bad", [
    ("State your reason for applying to this role", "textarea", "region"),
    ("Mobile app development experience", "textarea", "phone"),
    ("Out of all our products, which is your favorite and why?", "textarea", "gpa_scale"),
    ("How many years until you graduate?", "text", "years_experience"),
    ("What is your major GPA?", "text", "major"),
    ("Please describe your visa status", "text", "sponsorship"),
    ("Which business unit are you interested in?", "text", "address_line2"),
    ("Are you proficient with the Microsoft Office suite?", "text", "address_line2"),
    ("Signature Date", "text", "full_name"),
    ("Are you able to work in-person at our NYC office?", "radio", "work_authorized"),
    ("Do you have work authorization in the US through OPT/CPT?", "radio", "work_authorized"),
])
def test_misclassifications_fixed(cfg, db, label, ftype, bad):
    r = resolve(cfg, db, label, ftype=ftype, options=YN if ftype == "radio" else None)
    assert r.catalog_id != bad, (label, r.catalog_id, r.value)


def test_signature_date_is_today(cfg, db):
    import datetime as dt

    r = resolve(cfg, db, "Signature Date", ftype="date", options=None)
    assert r.catalog_id == "todays_date" and r.value == dt.date.today().isoformat()


def test_citizen_or_permanent_resident(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"citizen": False, "permanent_resident": True}}})
    cfg = load_config(project)
    db = Database(cfg.paths.db_path)
    db.initialize()
    for q in ("Are you a citizen of the United States or a lawful permanent resident?",
              "Are you a U.S. citizen or green card holder?"):
        assert resolve(cfg, db, q).value == "Yes", q
    assert resolve(cfg, db, "Are you a U.S. citizen?").value == "No"


def test_country_detection_and_now_vs_future(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {
        "US": {"requires_sponsorship_now": False, "requires_sponsorship_future": True},
        "CA": {"authorized": True, "requires_sponsorship_now": False, "requires_sponsorship_future": False,
               "citizen": True, "permanent_resident": False}}})
    cfg = load_config(project)
    db = Database(cfg.paths.db_path)
    db.initialize()
    canada = resolve(cfg, db, "Will you require us to sponsor a work permit for you to work in Canada?",
                     locations=["Toronto, ON"])
    assert canada.value == "No"  # Canadian facts, not "us" = United States
    now_only = resolve(cfg, db, "Do you currently require visa sponsorship?")
    assert now_only.value == "No"
    future = resolve(cfg, db, "Will you now or in the future require visa sponsorship?")
    assert future.value == "Yes"


def test_sponsorship_timing_options_go_to_reasoning(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"requires_sponsorship_now": False,
                                                                                 "requires_sponsorship_future": True}}})
    cfg = load_config(project)
    db = Database(cfg.paths.db_path)
    db.initialize()
    r = resolve(cfg, db, "Do you require sponsorship?",
                options=["Yes, I currently require sponsorship", "Not currently, but I will in the future", "No"])
    assert r.status == "reasoning" and r.reasoning["hints"]["requires_sponsorship_future"] is True


def test_company_referential_questions_are_scoped(cfg, db):
    a = resolve(cfg, db, "Have you applied to this company before?", company="Alpha")
    b = resolve(cfg, db, "Have you applied to this company before?", company="Beta")
    assert a.company_specific and a.reasoning["dedupe_key"] != b.reasoning["dedupe_key"]


def _snap(fields, buttons):
    return PageSnapshot.model_validate({"fields": fields, "buttons": buttons})


def test_apply_button_on_filled_form_is_final():
    filled = [{"id": "0:1", "key": "k", "type": "text", "label": "Name", "value": "Testy"}]
    snap = _snap(filled, [{"id": "0:2", "text": "Apply Now", "kind": "apply"}])
    assert [b.text for b in snap.final_buttons()] == ["Apply Now"]
    empty = _snap([], [{"id": "0:2", "text": "Apply Now", "kind": "apply"}])
    assert empty.final_buttons() == []


def test_page_actions_cannot_click_final_buttons():
    snap = _snap([{"id": "0:1", "key": "k", "type": "text", "label": "Name", "value": "x"}],
                 [{"id": "0:2", "text": "Finish", "kind": "other"}, {"id": "0:3", "text": "Back", "kind": "back"}])
    assert _final_click({"type": "click_button", "button_id": "0:2"}, snap)
    assert not _final_click({"type": "click_button", "button_id": "0:3"}, snap)
    assert _final_click({"type": "click_text", "text": "Send application"}, snap)
    assert _final_click({"type": "click_point", "x": 1, "y": 2}, snap)


def test_verification_helpers():
    assert scrub_codes("482913 is your verification code") == "[code] is your verification code"
    doms = sender_domains_for("https://acme.wd5.myworkdayjobs.com/x", None, "Acme Robotics")
    assert "myworkdayjobs.com" in doms and "myworkday" in doms and "acmerobotics" in doms


def test_equivalent_listings_in_prep_do_not_skip_each_other(cfg, db):
    from applypilot.db import Database as _D  # noqa: F401

    a_job = make_job(db, company="Twin", title="SWE Intern", url="https://twin.example/1", locations=["NYC"])
    b_job = make_job(db, company="Twin", title="SWE Intern", url="https://twin.example/2", locations=["SF"])
    a, _ = db.create_application(a_job, mode="backlog")
    b, _ = db.create_application(b_job, mode="backlog")
    for app in (a, b):
        db.update_application(app["id"], state="CHECKING_ELIGIBILITY")
    lo, hi = sorted([a, b], key=lambda x: x["id"])
    others_for_lo = [x for x in db.find_equivalent_applications(db.get_job(lo["job_id"]), exclude_app=lo["id"])
                     if not (x["state"] in ("QUEUED", "CHECKING_ELIGIBILITY") and x["id"] > lo["id"])]
    others_for_hi = [x for x in db.find_equivalent_applications(db.get_job(hi["job_id"]), exclude_app=hi["id"])
                     if not (x["state"] in ("QUEUED", "CHECKING_ELIGIBILITY") and x["id"] > hi["id"])]
    assert others_for_lo == [] and len(others_for_hi) == 1  # exactly one of the pair proceeds


def test_tab_budget_ignores_apps_without_tabs(db):
    jobs = [make_job(db, company=f"P{i}", url=f"https://p{i}.example/1") for i in range(3)]
    apps = [db.create_application(j, mode="backlog")[0] for j in jobs]
    for app in apps[:2]:  # parked during prep (never opened a tab)
        db.update_application(app["id"], state="NEEDS_USER")
    db.update_application(apps[2]["id"], state="READY")
    assert db.claim("w", "browser", lease_seconds=30, max_tabs=1)
