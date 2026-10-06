from applypilot.identity import (
    ats_job_key,
    detect_ats,
    domain_key,
    job_aliases,
    normalize_url,
    simplify_id_from_url,
)
from applypilot.textutil import (
    company_norm,
    normalize_question,
    parse_limits,
    similarity,
    title_norm,
)


def test_normalize_question_ignores_filler_and_punctuation():
    a = normalize_question("Are you legally authorized to work in the U.S.? *")
    b = normalize_question("are you authorized to work in the US")
    assert a == b


def test_similarity_orders_related_questions():
    q = "Will you now or in the future require visa sponsorship?"
    assert similarity(q, "Will you require sponsorship now or in the future?") > 0.75
    assert similarity(q, "What is your favorite color?") < 0.3
    assert similarity(q, q) == 1.0


def test_company_and_title_norms():
    assert company_norm("Stripe, Inc.") == company_norm("stripe")
    assert company_norm("Jane Street Capital LLC") == "jane street capital"
    assert title_norm("Software Engineer Intern - Summer 2027") == title_norm("Software Engineering Internship (2027)")
    assert title_norm("SWE Intern") == title_norm("Software Engineer Intern")
    assert title_norm("ML Intern - Infrastructure") != title_norm("ML Intern - Frontend")


def test_parse_limits():
    assert parse_limits("Why us? (max 300 words)") == {"max_words": 300}
    assert parse_limits("Answer in 500 characters or less") == {"max_chars": 500}
    assert parse_limits("Tell us more") == {}


def test_normalize_url_drops_tracking():
    a = normalize_url("https://www.Boards.Greenhouse.io/acme/jobs/123/?utm_source=Simplify&ref=Simplify#app")
    b = normalize_url("http://boards.greenhouse.io/acme/jobs/123")
    assert a == b
    assert normalize_url("javascript:alert(1)") == ""


def test_ats_detection_and_keys():
    assert detect_ats("https://job-boards.greenhouse.io/x/jobs/1") == "greenhouse"
    assert detect_ats("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternal/job/x_JR123") == "workday"
    assert ats_job_key("https://boards.greenhouse.io/embed/job_app?token=8847738002&gh_src=Simplify") == "greenhouse:8847738002"
    assert ats_job_key("https://job-boards.greenhouse.io/databricks/jobs/8847738002") == "greenhouse:8847738002"
    assert ats_job_key(None, "greenhouse:databricks/jobs/8847738002") == "greenhouse:8847738002"
    assert ats_job_key("https://jobs.lever.co/acme/3f2a1b4c-1111-2222-3333-444455556666/apply").startswith("lever:3f2a1b4c")
    assert ats_job_key("https://biotechne.wd5.myworkdayjobs.com/en-US/Biotechne/job/Austin-TX/Software-Engineering-Intern_JR101487") \
        == "workday:biotechne:jr101487"


def test_domain_key_splits_multi_tenant_hosts():
    assert domain_key("https://job-boards.greenhouse.io/acme/jobs/1") != domain_key("https://job-boards.greenhouse.io/other/jobs/2")
    assert domain_key("https://nvidia.wd5.myworkdayjobs.com/a") == "nvidia.wd5.myworkdayjobs.com"
    assert domain_key("https://jobs.ashbyhq.com/acme/123") == "jobs.ashbyhq.com/acme"


def test_aliases_order_and_simplify_id():
    sid = simplify_id_from_url("https://simplify.jobs/p/956EEDBE-799d-459b-8556-9184a7f7f706?utm_source=GHList")
    assert sid == "956eedbe-799d-459b-8556-9184a7f7f706"
    aliases = job_aliases(simplify_id=sid, ats_key="greenhouse:1", urls=["https://x.com/a?utm_source=y"],
                          company="Acme", title="SWE Intern", locations=["NYC"])
    assert [k for k, _ in aliases] == ["simplify", "ats", "url", "ctl"]
