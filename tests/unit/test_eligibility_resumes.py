from conftest import edit_yaml

from applypilot.config import load_config
from applypilot.eligibility import evaluate, job_countries
from applypilot.resumes import select_resume


def job(text="", **kw):
    base = {"id": "j1", "company": "Acme", "title": kw.pop("title", "Software Engineer Intern"),
            "locations": kw.pop("locations", ["Seattle, WA"]), "sponsorship": kw.pop("sponsorship", "Other"),
            "degrees": kw.pop("degrees", ["Bachelor's"]), "category": kw.pop("category", "Software"),
            "details": {"description_text": text, "requirements": kw.pop("requirements", [])}}
    base.update(kw)
    return base


PERMISSIVE = [
    "Kubernetes preferred.",
    "Experience with Kubernetes.",
    "Previous SWE internship preferred.",
    "Familiarity with distributed systems.",
    "Strong C++ skills required.",
    "PyTorch experience.",
    "3+ years of experience with Java preferred.",
    "Master's degree preferred.",
]


def test_skill_requirements_never_disqualify(cfg):
    for text in PERMISSIVE:
        assert evaluate(job(text), cfg).decision == "eligible", text


def test_listing_citizenship_rule_uses_facts(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"citizen": False}}})
    cfg = load_config(project)
    res = evaluate(job(sponsorship="U.S. Citizenship is Required"), cfg)
    assert res.decision == "ineligible"
    assert "listing_us_citizenship_required" in res.reasons[0]


def test_citizen_passes_citizenship_rule(cfg):
    assert evaluate(job(sponsorship="U.S. Citizenship is Required"), cfg).decision == "eligible"


def test_no_sponsorship_only_hurts_if_candidate_needs_it(project):
    cfg = load_config(project)
    assert evaluate(job(sponsorship="Does Not Offer Sponsorship"), cfg).decision == "eligible"
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"requires_sponsorship_future": True}}})
    cfg = load_config(project)
    assert evaluate(job(sponsorship="Does Not Offer Sponsorship"), cfg).decision == "ineligible"


def test_rule_scoped_to_country(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"citizen": False}}})
    cfg = load_config(project)
    res = evaluate(job(sponsorship="U.S. Citizenship is Required", locations=["London, UK"]), cfg)
    assert res.decision == "eligible"


def test_grad_only_needs_both_listing_and_text(cfg):
    assert evaluate(job("Build cool stuff.", degrees=["PhD"]), cfg).decision == "eligible"
    res = evaluate(job("This role is for PhD students in robotics.", degrees=["PhD"]), cfg)
    assert res.decision == "ineligible"
    assert evaluate(job("For PhD students or exceptional undergrads", degrees=["Bachelor's", "PhD"]), cfg).decision == "eligible"


def test_graduation_window_is_decided_deterministically(cfg):
    ok = evaluate(job(requirements=["You will graduate between fall 2027 and Summer 2028."]), cfg)
    assert ok.decision == "eligible"
    bad = evaluate(job(requirements=["Must be graduating between December 2026 and June 2027."]), cfg)
    assert bad.decision == "ineligible" and "graduation_window" in bad.reasons[0]


def test_vague_graduation_language_goes_to_astra_review(cfg):
    res = evaluate(job(requirements=["Ideally graduating around spring 2028 or so."]), cfg)
    assert res.decision == "review" and res.review[0]["rule_id"] == "graduation_window"


def test_high_school_only_programs(cfg):
    assert evaluate(job("This role is intended for current high school students graduating in 2027 or later."),
                    cfg).decision == "ineligible"


def test_unknown_fact_never_disqualifies_by_default(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"citizen": None}}})
    cfg = load_config(project)
    res = evaluate(job(sponsorship="U.S. Citizenship is Required"), cfg)
    assert res.decision == "eligible" and res.warnings


def test_unknown_fact_needs_user_policy(project):
    edit_yaml(project, "config/candidate.yaml", {"work_authorization": {"US": {"citizen": None}}})
    edit_yaml(project, "config/settings.yaml", {"eligibility": {"unknown_fact_policy": "needs_user"}})
    cfg = load_config(project)
    assert evaluate(job(sponsorship="U.S. Citizenship is Required"), cfg).decision == "needs_user"


def test_user_rules_are_data(project):
    edit_yaml(project, "config/preferences.yaml", {"eligibility_rules": [{
        "id": "must_be_in_canada", "patterns": [r"(?i)must reside in canada"], "candidate_path": "candidate.address.country",
        "operator": "in", "expected": ["Canada", "CA"], "explanation": "Canadian residency required"}]})
    cfg = load_config(project)
    assert evaluate(job("Applicants must reside in Canada."), cfg).decision == "ineligible"
    assert evaluate(job("Applicants may reside anywhere."), cfg).decision == "eligible"


def test_preference_filters(project):
    edit_yaml(project, "config/preferences.yaml", {"application": {"excluded_companies": ["Acme"]}})
    cfg = load_config(project)
    assert evaluate(job(), cfg).decision == "skip_preference"


def test_job_countries():
    assert job_countries({"locations": ["Seattle, WA", "Toronto, ON"]}) == {"US", "CA"}
    assert job_countries({"locations": ["London, UK"]}) == {"GB"}


def test_resume_selection(cfg):
    assert select_resume(job(title="Machine Learning Intern", category="AI/ML/Data"), cfg).filename == "resume_ml.pdf"
    swe = select_resume(job(title="Backend Software Engineer Intern"), cfg)
    assert swe.filename == "resume_swe.pdf" and swe.method == "alias"
    fallback = select_resume(job(title="Hardware Engineering Intern", category="Hardware"), cfg)
    assert fallback.filename == "resume_swe.pdf" and fallback.method == "default"


def test_resume_explicit_mapping_wins(project):
    edit_yaml(project, "config/preferences.yaml", {"resumes": {"mappings": {"ml": "resume_swe.pdf"}}})
    cfg = load_config(project)
    choice = select_resume(job(title="Machine Learning Intern"), cfg)
    assert choice.filename == "resume_swe.pdf" and choice.method == "mapping"
