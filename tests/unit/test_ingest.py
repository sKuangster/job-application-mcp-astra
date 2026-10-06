import json

import httpx
import pytest

from applypilot.ingest.listings import fetch_jobs, parse_listings, parse_readme
from applypilot.ingest.simplify_page import parse_simplify_html, resolve_click

LISTINGS = [
    {"source": "Simplify", "category": "Software", "company_name": "Acme", "id": "11111111-1111-1111-1111-111111111111",
     "title": "Software Engineer Intern", "active": True, "terms": ["Summer 2027"], "date_updated": 1, "date_posted": 1,
     "url": "https://job-boards.greenhouse.io/acme/jobs/1?utm_source=Simplify&ref=Simplify", "locations": ["NYC, NY"],
     "company_url": "", "is_visible": True, "sponsorship": "Other", "degrees": ["Bachelor's"]},
    {"source": "Simplify", "category": "Software", "company_name": "OldCo", "id": "22222222-2222-2222-2222-222222222222",
     "title": "SWE Intern", "active": True, "terms": ["Summer 2026"], "date_updated": 1, "date_posted": 1,
     "url": "https://x.com", "locations": [], "company_url": "", "is_visible": True, "sponsorship": "Other", "degrees": []},
    {"source": "Simplify", "category": "Quant", "company_name": "Hidden", "id": "33333333-3333-3333-3333-333333333333",
     "title": "Quant Intern", "active": True, "terms": ["Summer 2027"], "date_updated": 1, "date_posted": 1,
     "url": "https://y.com", "locations": [], "company_url": "", "is_visible": False, "sponsorship": "Other", "degrees": []},
]

README = """
<table><thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr></thead>
<tbody>
<tr><td>🔥 <strong><a href="https://simplify.jobs/c/Databricks">Databricks</a></strong></td><td>Software Engineer Intern</td>
<td>London, UK</td><td><div><a href="https://boards.greenhouse.io/embed/job_app?token=8847738002&utm_source=Simplify&ref=Simplify">
<img alt="Apply"></a> <a href="https://simplify.jobs/p/956eedbe-799d-459b-8556-9184a7f7f706?utm_source=GHList"><img alt="Simplify"></a></div></td><td>1d</td></tr>
<tr><td>↳</td><td>Data Intern</td><td><details><summary>2 locations</summary>NYC, NY</br>SF, CA</details></td>
<td><div><a href="https://boards.greenhouse.io/x/jobs/2"><img alt="Apply"></a></div></td><td>2d</td></tr>
<tr><td>Closed Inc</td><td>Intern</td><td>Remote</td><td>🔒</td><td>9d</td></tr>
</tbody></table>
"""

SIMPLIFY_HTML = '<html><script id="__NEXT_DATA__" type="application/json">{}</script></html>'.format(json.dumps({
    "props": {"pageProps": {"jobPosting": {
        "title": "Software Engineer Intern", "description": "<p>Build things.</p><ul><li>Python</li></ul>",
        "requirements": ["You will graduate between fall 2027 and Summer 2028."], "responsibilities": ["Ship features"],
        "tracked_obj": "greenhouse:databricks/jobs/8847738002", "url": "https://simplify.jobs/jobs/click/abc",
        "locations": [{"value": "London, UK", "country": "United Kingdom"}],
        "job": {"company": {"name": "Databricks", "url": "https://databricks.com/", "description": "Data + AI",
                            "news": [{"title": "News", "url": "https://n.example"}]}}}}}}))


def test_parse_listings_filters_terms_and_visibility():
    recs = parse_listings(LISTINGS, terms=["Summer 2027"])
    assert [r.company for r in recs] == ["Acme"]
    rec = recs[0].to_db()
    assert rec["simplify_url"] == "https://simplify.jobs/p/11111111-1111-1111-1111-111111111111"
    assert rec["ats_job_key"] == "greenhouse:1"
    assert rec["ats"] == "greenhouse"


def test_content_hash_stable_and_material():
    a = parse_listings(LISTINGS, terms=["Summer 2027"])[0]
    b = parse_listings(LISTINGS, terms=["Summer 2027"])[0]
    assert a.content_hash() == b.content_hash()
    b.sponsorship = "Does Not Offer Sponsorship"
    assert a.content_hash() != b.content_hash()


def test_parse_readme_fallback():
    recs = parse_readme(README)
    assert recs[0].company == "Databricks"
    assert recs[0].simplify_id == "956eedbe-799d-459b-8556-9184a7f7f706"
    assert "utm_source" not in recs[0].direct_url
    assert recs[1].company == "Databricks" and recs[1].locations == ["NYC, NY", "SF, CA"]
    assert recs[2].active is False


def test_parse_simplify_page():
    d = parse_simplify_html(SIMPLIFY_HTML)
    assert d["tracked_obj"] == "greenhouse:databricks/jobs/8847738002"
    assert "Build things." in d["description_text"] and "Python" in d["description_text"]
    assert d["company"]["url"] == "https://databricks.com/"
    assert d["locations"][0]["country"] == "United Kingdom"


async def test_resolve_click_follows_redirect_off_simplify():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "simplify.jobs":
            return httpx.Response(307, headers={"location": "https://boards.greenhouse.io/embed/job_app?token=1"})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await resolve_click("https://simplify.jobs/jobs/click/abc", client) == \
            "https://boards.greenhouse.io/embed/job_app?token=1"


async def test_fetch_jobs_uses_etag_and_falls_back_to_readme():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("listings.json"):
            if request.headers.get("if-none-match") == "v1":
                return httpx.Response(304)
            return httpx.Response(500)
        return httpx.Response(200, text=README, headers={"etag": "r1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res = await fetch_jobs(repository="https://github.com/SimplifyJobs/Summer2027-Internships", branch="dev",
                               listings_path=".github/scripts/listings.json", readme_path="README.md", terms=[],
                               categories=[], client=client)
        assert res.source.endswith("README.md") and len(res.records) == 3
        res2 = await fetch_jobs(repository="https://github.com/SimplifyJobs/Summer2027-Internships", branch="dev",
                                listings_path=".github/scripts/listings.json", readme_path="README.md", terms=[],
                                categories=[], etag="v1", client=client)
        assert res2.not_modified


@pytest.mark.skipif("not __import__('os').environ.get('APPLYPILOT_NETWORK_TESTS')")
async def test_live_repository_parses():  # pragma: no cover - network
    res = await fetch_jobs(repository="https://github.com/SimplifyJobs/Summer2027-Internships", branch="dev",
                           listings_path=".github/scripts/listings.json", readme_path="README.md",
                           terms=["Summer 2027"], categories=[])
    assert len(res.records) > 100
