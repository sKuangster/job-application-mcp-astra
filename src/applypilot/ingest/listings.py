"""SimplifyJobs repository ingestion.

Primary source: ``.github/scripts/listings.json`` — the structured file the repository's
own tooling generates its README from (stable UUIDs, active flag, terms, sponsorship,
degree metadata). Fallback: the rendered README HTML tables. Both are fetched from
raw.githubusercontent.com with ETag caching; nothing is scraped visually.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from applypilot.identity import (
    ats_job_key,
    detect_ats,
    job_aliases,
    job_id_for,
    simplify_id_from_url,
)
from applypilot.textutil import clean, company_norm, stable_hash, title_norm

log = logging.getLogger("applypilot.ingest")
USER_AGENT = "ApplyPilot/1.0 (+local job application assistant)"


@dataclass
class JobRecord:
    company: str
    title: str
    locations: list[str]
    simplify_id: str | None
    simplify_url: str | None
    direct_url: str | None
    active: bool = True
    category: str | None = None
    terms: list[str] = field(default_factory=list)
    sponsorship: str | None = None
    degrees: list[str] = field(default_factory=list)
    date_posted: float | None = None
    date_updated: float | None = None
    source: str = "simplify_listings"

    def content_hash(self) -> str:
        # Fields whose change is "material" (re-check eligibility when they change).
        return stable_hash({
            "company": self.company, "title": self.title, "locations": sorted(self.locations),
            "active": self.active, "sponsorship": self.sponsorship, "degrees": sorted(self.degrees),
            "terms": sorted(self.terms), "url": self.direct_url,
        })

    def to_db(self) -> dict[str, Any]:
        ats_key = ats_job_key(self.direct_url)
        return {
            "id": job_id_for(self.simplify_id, self.company, self.title, self.direct_url),
            "simplify_id": self.simplify_id,
            "company": self.company,
            "company_norm": company_norm(self.company),
            "title": self.title,
            "title_norm": title_norm(self.title),
            "locations": self.locations,
            "category": self.category,
            "terms": self.terms,
            "sponsorship": self.sponsorship,
            "degrees": self.degrees,
            "active": self.active,
            "simplify_url": self.simplify_url,
            "direct_url": self.direct_url,
            "ats": detect_ats(self.direct_url),
            "ats_job_key": ats_key,
            "date_posted": self.date_posted,
            "date_updated": self.date_updated,
            "content_hash": self.content_hash(),
            "source": self.source,
            "_aliases": job_aliases(simplify_id=self.simplify_id, ats_key=ats_key, urls=[self.direct_url],
                                    company=self.company, title=self.title, locations=self.locations),
        }


def simplify_url_for(simplify_id: str) -> str:
    return f"https://simplify.jobs/p/{simplify_id}"


def parse_listings(data: list[dict[str, Any]], *, terms: list[str] | None = None,
                   categories: list[str] | None = None) -> list[JobRecord]:
    """Parse listings.json entries; keeps inactive ones (so closures are recorded)."""
    out: list[JobRecord] = []
    wanted_terms = {t.casefold() for t in (terms or [])}
    wanted_cats = {c.casefold() for c in (categories or [])}
    for item in data:
        if not isinstance(item, dict) or not item.get("company_name") or not item.get("title"):
            continue
        if item.get("is_visible") is False:
            continue
        item_terms = [str(t) for t in item.get("terms") or []]
        if wanted_terms and not any(t.casefold() in wanted_terms for t in item_terms):
            continue
        category = item.get("category")
        if wanted_cats and str(category or "").casefold() not in wanted_cats:
            continue
        sid = str(item.get("id") or "").lower() or None
        if sid and not re.fullmatch(r"[0-9a-f-]{36}", sid):
            sid = None
        url = item.get("url") or None
        is_simplify_source = str(item.get("source") or "").lower() == "simplify"
        out.append(JobRecord(
            company=clean(item["company_name"]),
            title=clean(item["title"]),
            locations=[clean(x) for x in item.get("locations") or [] if clean(x)],
            simplify_id=sid,
            # Listings contributed by Simplify have a Simplify job page; community-added ones
            # may not, but /p/<id> resolves for every listing id in the repository.
            simplify_url=simplify_url_for(sid) if sid else None,
            direct_url=url,
            active=bool(item.get("active", True)),
            category=category,
            terms=item_terms,
            sponsorship=item.get("sponsorship"),
            degrees=[str(d) for d in item.get("degrees") or []],
            date_posted=_num(item.get("date_posted")),
            date_updated=_num(item.get("date_updated")),
            source="simplify_listings" if is_simplify_source else f"listings:{item.get('source')}",
        ))
    return out


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _strip_tracking(url: str) -> str:
    return re.sub(r"[?&]utm_source=Simplify&ref=Simplify$|[?&]utm_source=Simplify$", "", url)


def parse_readme(markdown: str) -> list[JobRecord]:
    """Fallback parser for the README's HTML tables (and legacy markdown tables)."""
    records: list[JobRecord] = []
    soup = BeautifulSoup(markdown, "html.parser")
    last_company = ""
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        company = clean(cells[0].get_text(" "))
        company = re.sub(r"^[^\w]+", "", company).strip()  # emoji markers (🔥 etc.)
        if company in {"↳", ""}:
            company = last_company
        else:
            last_company = company
        title = clean(cells[1].get_text(" "))
        for br in cells[2].find_all("br"):
            br.replace_with("\n")
        locations = [
            clean(x) for x in cells[2].get_text("\n").split("\n")
            if clean(x) and not re.fullmatch(r"\d+\s+locations?", clean(x), re.I)
        ]
        links = [a.get("href") for a in cells[3].find_all("a") if a.get("href")]
        closed = "🔒" in cells[3].get_text() or not links
        simplify_link = next((h for h in links if "simplify.jobs/p/" in h), None)
        direct = next((h for h in links if "simplify.jobs" not in h), None)
        sid = simplify_id_from_url(simplify_link)
        records.append(JobRecord(
            company=company, title=title, locations=locations, simplify_id=sid,
            simplify_url=simplify_url_for(sid) if sid else None,
            direct_url=_strip_tracking(direct) if direct else None, active=not closed,
            source="readme",
        ))
    return records


def raw_url(repository: str, branch: str, path: str) -> str:
    parts = urlsplit(repository)
    owner_repo = parts.path.strip("/").removesuffix(".git")
    return f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{path.lstrip('/')}"


@dataclass
class FetchResult:
    records: list[JobRecord]
    source: str
    etag: str | None
    not_modified: bool = False


async def fetch_jobs(*, repository: str, branch: str, listings_path: str, readme_path: str,
                     terms: list[str], categories: list[str], etag: str | None = None,
                     timeout: float = 60, client: httpx.AsyncClient | None = None) -> FetchResult:
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    try:
        url = raw_url(repository, branch, listings_path)
        headers = {"If-None-Match": etag} if etag else {}
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 304:
                return FetchResult([], url, etag, not_modified=True)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                raise ValueError("listings.json is not a list")
            return FetchResult(parse_listings(data, terms=terms, categories=categories), url, resp.headers.get("etag"))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("listings.json unavailable (%s); falling back to README", exc)
        url = raw_url(repository, branch, readme_path)
        resp = await client.get(url)
        resp.raise_for_status()
        return FetchResult(parse_readme(resp.text), url, resp.headers.get("etag"))
    finally:
        if own:
            await client.aclose()


def today_start(now: float) -> float:
    d = dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()
