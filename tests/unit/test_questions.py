import pytest
from conftest import edit_yaml, make_job, page_field

from applypilot.config import load_config
from applypilot.db import Database
from applypilot.questions.catalog import classify
from applypilot.questions.options import DECLINE, match_option, to_field_value
from applypilot.questions.resolver import ResolveContext, Resolver

YES_NO = ["Select...", "Yes", "No"]
CHOICE_ONLY = {"work_authorized", "sponsorship", "authorized_without_sponsorship", "previously_employed", "age_18",
               "citizen"}




@pytest.mark.parametrize("label,expected", [
    ("First Name", "first_name"), ("Legal Last Name", "last_name"), ("Email Address", "email"),
    ("Confirm your email", "email_confirm"), ("Phone", "phone"), ("LinkedIn Profile", "linkedin"),
    ("Are you legally authorized to work in the United States?", "work_authorized"),
    ("Will you now or in the future require sponsorship for employment visa status (e.g. H-1B visa status)?", "sponsorship"),
    ("Are you authorized to work in the US without the need for visa sponsorship?", "authorized_without_sponsorship"),
    ("What is your expected graduation date?", "graduation_date"), ("Graduation Year", "graduation_year"),
    ("School", "school"), ("Degree", "degree"), ("Major / Field of Study", "major"), ("GPA", "gpa"),
    ("How did you hear about this job?", "how_heard"), ("Gender", "eeo_gender"),
    ("Are you Hispanic/Latino?", "eeo_hispanic"), ("Veteran Status", "eeo_veteran"), ("Disability Status", "eeo_disability"),
    ("Why do you want to work at Fixture Robotics?", "why_company"),
    ("Why are you interested in this role?", "why_role"),
    ("Tell us about a project you are proud of", "technical_project"),
    ("Have you previously worked for Fixture Robotics?", "previously_employed"),
    ("Are you at least 18 years of age?", "age_18"),
    ("Location (City)", "location"), ("Country", "country"), ("State", "region"),
    ("Are you a U.S. citizen?", "citizen"),
])
def test_catalog_classification(label, expected):
    ftype = "textarea" if expected in {"why_company", "why_role", "technical_project"} else "text"
    if expected in CHOICE_ONLY:
        ftype = "radio"  # yes/no facts are only answered deterministically on choice widgets
    c = classify(page_field(label=label, type=ftype), company="Fixture Robotics")
    assert c.entry is not None and c.entry.id == expected, (label, c.entry and c.entry.id)


def test_unknown_and_job_specific_classification():
    assert classify(page_field(label="Do you have experience with Kubernetes?", type="radio",
                               options=YES_NO)).qclass == "JOB_SPECIFIC"
    assert classify(page_field(label="What is your favourite fruit?", type="text")).qclass == "UNKNOWN"
    assert classify(page_field(label="Why Acme?", type="textarea"), company="Acme").qclass == "WRITTEN"


def test_option_mapping_booleans():
    assert match_option(True, YES_NO).value == "Yes"
    assert match_option(False, ["Yes, I will require sponsorship", "No, I will not require sponsorship"]).value.startswith("No")
    assert match_option(True, ["I am authorized", "I am not authorized"]).value == "I am authorized"
    amb = match_option(True, ["Yes - H1B", "Yes - OPT", "No"])
    assert not amb.ok and amb.ambiguous


def test_option_mapping_synonyms_and_decline():
    assert match_option("United States", ["Canada", "United States of America", "Mexico"]).value == "United States of America"
    assert match_option("WA", ["Washington", "Oregon"]).value == "Washington"
    assert match_option(DECLINE, ["Male", "Female", "I don't wish to answer"]).value == "I don't wish to answer"
    assert match_option("bachelor", ["High School", "Bachelor's Degree", "Master's Degree"]).value == "Bachelor's Degree"
    assert not match_option("Purple", ["Red", "Blue"]).ok


def test_date_formatting(cfg):
    import datetime as dt

    d = dt.date(2028, 5, 15)
    assert to_field_value(page_field(label="Graduation date", type="date"), [d]).value == "2028-05-15"
    assert to_field_value(page_field(label="Graduation date", type="text", placeholder="MM/YYYY"), [d]).value == "05/2028"
    assert to_field_value(page_field(label="Graduation", type="select", options=["2027", "2028"]), [d]).value == "2028"


@pytest.fixture
def resolver(cfg, db):
    return Resolver(cfg, db)


def ctx(db, **kw):
    job_id = make_job(db, **kw)
    return ResolveContext(job=db.get_job(job_id), application_id="app_x")


def test_factual_fields_resolve_from_source_of_truth(resolver, db):
    c = ctx(db)
    r = resolver.resolve(page_field(label="First Name"), c)
    assert (r.status, r.value, r.method) == ("resolved", "Testy", "fact")
    assert r.sources["facts"][0]["path"] == "candidate.identity.first_name"
    r = resolver.resolve(page_field(label="Are you legally authorized to work in the United States?", type="select",
                                    options=YES_NO, required=True), c)
    assert r.value == "Yes" and r.sources["derived"] == "work_authorized"
    r = resolver.resolve(page_field(label="Will you now or in the future require sponsorship?", type="radio",
                                    options=["Yes", "No"], required=True), c)
    assert r.value == "No"
    r = resolver.resolve(page_field(label="Have you previously worked for Fixture Labs?", type="radio", options=["Yes", "No"]),
                         ctx(db, company="Fixture Labs", url="https://jobs.lever.co/fl/1"))
    assert r.value == "Yes"


def test_eeo_never_inferred_and_declines(resolver, db):
    c = ctx(db)
    r = resolver.resolve(page_field(label="Gender", type="select", options=["Male", "Female", "Decline To Self Identify"]), c)
    assert r.value == "Decline To Self Identify" and r.method == "eeo"
    r = resolver.resolve(page_field(label="Gender", type="text"), c)
    assert r.status == "skip"


def test_eeo_configured_value(project):
    edit_yaml(project, "config/preferences.yaml", {"eeo": {"gender": "Female"}})
    cfg = load_config(project)
    db = Database(cfg.paths.db_path)
    db.initialize()
    r = Resolver(cfg, db).resolve(page_field(label="Gender", type="select", options=["Man", "Woman", "Decline"]), ctx(db))
    assert r.value == "Woman"


def test_missing_required_fact_needs_user(project):
    edit_yaml(project, "config/candidate.yaml", {"education": {"gpa": None}})
    cfg = load_config(project)
    db = Database(cfg.paths.db_path)
    db.initialize()
    res = Resolver(cfg, db)
    required = res.resolve(page_field(label="GPA", required=True), ctx(db))
    assert required.status == "needs_user" and "candidate.education.gpa" in required.missing
    optional = res.resolve(page_field(label="GPA", required=False), ctx(db))
    assert optional.status == "skip"


def test_ambiguous_option_goes_to_reasoning_with_known_fact(resolver, db):
    r = resolver.resolve(page_field(label="Are you legally authorized to work in the United States?", type="select",
                                    options=["Yes - citizen", "Yes - visa holder", "No"], required=True), ctx(db))
    assert r.status == "reasoning"
    assert r.reasoning["hints"]["known_value"] == [True]


def test_written_goes_to_reasoning_with_template(resolver, db):
    r = resolver.resolve(page_field(label="Why do you want to work at Fixture Robotics?", type="textarea", required=True,
                                    max_length=1500), ctx(db))
    assert r.status == "reasoning" and r.reasoning["template"] == "why_company"
    assert r.reasoning["research_recommended"] and r.reasoning["limits"]["max_chars"] == 1500


def test_optional_unknown_is_skipped_to_save_calls(resolver, db):
    r = resolver.resolve(page_field(label="Favourite movie?", type="text", required=False), ctx(db))
    assert r.status == "skip"


def test_factual_answers_and_user_answers(resolver, db):
    r = resolver.resolve(page_field(label="Do you have any relatives employed by this company?", type="radio",
                                    options=["Yes", "No"], required=True), ctx(db))
    assert r.value == "No" and r.method == "fact"
    from applypilot.textutil import normalize_question

    db.save_user_answer(normalize_question("Are you comfortable working weekends?"), "Are you comfortable working weekends?", True)
    r = resolver.resolve(page_field(label="Are you comfortable working weekends?", type="radio", options=["Yes", "No"],
                                    required=True), ctx(db))
    assert r.value == "Yes" and r.method == "user"


def _remember(db, cfg, question, value, qclass, company=""):
    from applypilot.textutil import normalize_question

    db.save_memory({"normalized": normalize_question(question), "question": question, "qclass": qclass,
                    "company_norm": company, "value": value, "method": "generated", "profile_hash": cfg.profile_hash,
                    "options_sig": ""})


def test_memory_reuse_rules(cfg, db, resolver):
    _remember(db, cfg, "Describe how you handle tight deadlines", "I plan backwards from the deadline.", "WRITTEN")
    r = resolver.resolve(page_field(label="Describe how you handle tight deadlines", type="textarea", required=True), ctx(db))
    assert r.method == "reused"
    # Company-specific prose is never reused for another company.
    _remember(db, cfg, "Why do you want to work at OtherCo?", "OtherCo is great", "WRITTEN", company="otherco")
    r = resolver.resolve(page_field(label="Why do you want to work at Fixture Robotics?", type="textarea", required=True), ctx(db))
    assert r.status == "reasoning"
    # Facts are never taken from memory.
    _remember(db, cfg, "First Name", "Wrong", "FACTUAL")
    assert resolver.resolve(page_field(label="First Name"), ctx(db)).value == "Testy"


def test_memory_ignored_when_profile_changes(cfg, db, resolver):
    from applypilot.textutil import normalize_question

    q = "Describe how you handle tight deadlines"
    db.save_memory({"normalized": normalize_question(q), "question": q, "qclass": "WRITTEN", "company_norm": "",
                    "value": "old", "method": "generated", "profile_hash": "different", "options_sig": ""})
    assert resolver.resolve(page_field(label=q, type="textarea", required=True), ctx(db)).status == "reasoning"


def test_alternate_rotation_for_rejected_formats(resolver, db):
    c = ctx(db)
    first = resolver.resolve(page_field(label="Phone", key="f_phone"), c).value
    c.alternates["f_phone"] = 1
    second = resolver.resolve(page_field(label="Phone", key="f_phone"), c).value
    assert first != second


def test_previous_employment_requires_exact_company(resolver, db):
    """Regression: 'Fixture Labs' (past employer) must not answer Yes for 'Fixture Corp'."""
    q = "Have you previously worked for this company?"
    other = resolver.resolve(page_field(label=q, type="radio", options=["Yes", "No"]),
                             ctx(db, company="Fixture Corp", url="https://corp.example/1"))
    same = resolver.resolve(page_field(label=q, type="radio", options=["Yes", "No"]),
                            ctx(db, company="Fixture Labs, Inc.", url="https://labs.example/1"))
    assert other.value == "No" and same.value == "Yes"
