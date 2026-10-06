"""Simplify job page enrichment over plain HTTP (no browser tab needed).

``https://simplify.jobs/p/<id>`` is server-rendered and embeds the posting in
``__NEXT_DATA__``: description, requirements, ATS tracking id, company profile and
recent company news (useful, cached company research for free). The Apply button
points at ``/jobs/click/<id>``, which 30x-redirects to the real ATS URL — resolving it
over HTTP lets the browser worker start directly on the application page while still
following Simplify's own link.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from applypilot.textutil import html_to_text, truncate

log = logging.getLogger("applypilot.simplify")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


class SimplifyUnavailable(Exception):
    pass


def parse_simplify_html(html: str) -> dict[str, Any]:
    m = _NEXT.search(html or "")
    if not m:
        raise SimplifyUnavailable("no __NEXT_DATA__ on Simplify page")
    data = json.loads(m.group(1))
    posting = (data.get("props") or {}).get("pageProps", {}).get("jobPosting")
    if not isinstance(posting, dict):
        raise SimplifyUnavailable("Simplify page has no jobPosting")
    return normalize_posting(posting)


def normalize_posting(p: dict[str, Any]) -> dict[str, Any]:
    job = p.get("job") or {}
    company = job.get("company") or {}
    news = [
        {"title": n.get("title"), "summary": truncate(n.get("summary") or "", 500), "url": n.get("url"),
         "published_at": n.get("published_at"), "publisher": n.get("published_by")}
        for n in (company.get("news") or [])[:5] if isinstance(n, dict)
    ]
    locations = [
        {"value": loc.get("value"), "country": loc.get("country")}
        for loc in p.get("locations") or [] if isinstance(loc, dict)
    ]
    return {
        "title": p.get("title") or job.get("title"),
        "description_text": html_to_text(p.get("description") or "", 12000),
        "requirements": [str(x) for x in p.get("requirements") or []],
        "responsibilities": [str(x) for x in p.get("responsibilities") or []],
        "desirable": [str(x) for x in p.get("desirable") or []],
        "additional_requirements": [str(x) for x in p.get("additional_requirements") or []],
        "skills": [s.get("name") for s in p.get("skills") or [] if isinstance(s, dict) and s.get("name")],
        "functions": [f.get("title") for f in p.get("functions") or [] if isinstance(f, dict)],
        "degrees_codes": p.get("degrees") or [],
        "seasons": p.get("seasons") or [],
        "sponsors_h1b": p.get("sponsors_h1b"),
        "travel_requirements": p.get("travel_requirements"),
        "min_salary": p.get("min_salary"), "max_salary": p.get("max_salary"),
        "salary_period": p.get("salary_period"),
        "active": p.get("active"),
        "tracked_obj": p.get("tracked_obj"),
        "click_url": p.get("url"),
        "locations": locations,
        "company": {
            "name": company.get("name"),
            "url": company.get("url"),
            "short_description": company.get("short_description"),
            "description": truncate(company.get("description") or "", 2000),
            "linkedin": company.get("linkedin"),
            "values": company.get("values") or [],
            "differentiation": company.get("differentiation") or [],
            "upsides": company.get("upsides") or [],
            "news": news,
            "size": company.get("company_size"),
            "year_founded": company.get("year_founded"),
        },
    }


async def fetch_simplify(simplify_url: str, *, timeout: float = 20,
                         client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Fetch and parse a Simplify job page, then resolve the Apply redirect."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": BROWSER_UA,
                                                                   "Accept-Language": "en-US,en;q=0.8"})
    try:
        resp = await client.get(simplify_url, follow_redirects=True)
        if resp.status_code == 404:
            raise SimplifyUnavailable("Simplify page not found (404)")
        resp.raise_for_status()
        details = parse_simplify_html(resp.text)
        details["simplify_page_url"] = str(resp.url)
        click = details.get("click_url")
        details["apply_url"] = await resolve_click(click, client) if click else None
        return details
    except httpx.HTTPError as exc:
        raise SimplifyUnavailable(f"Simplify fetch failed: {exc}") from exc
    finally:
        if own:
            await client.aclose()


async def resolve_click(click_url: str, client: httpx.AsyncClient, max_hops: int = 5) -> str | None:
    """Follow Simplify's /jobs/click redirect chain until it leaves simplify.jobs."""
    url = click_url
    for _ in range(max_hops):
        resp = await client.get(url, follow_redirects=False)
        location = resp.headers.get("location")
        if resp.status_code in (301, 302, 303, 307, 308) and location:
            url = str(httpx.URL(url).join(location))
            if "simplify.jobs" not in httpx.URL(url).host:
                return url
            continue
        return None if "simplify.jobs" in httpx.URL(url).host else url
    return None


def job_text(job: dict[str, Any]) -> str:
    """All job text used for eligibility rules."""
    d = job.get("details") or {}
    parts = [job.get("title") or "", d.get("description_text") or ""]
    for key in ("requirements", "additional_requirements", "desirable", "responsibilities"):
        parts.extend(d.get(key) or [])
    return "\n".join(p for p in parts if p)
