"""Read-only SimplifyJobs discovery with conditional requests and robust tables."""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

DEFAULT_SOURCE = "https://github.com/SimplifyJobs/Summer2027-Internships"
DEFAULT_RAW = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md"


@dataclass
class FetchResult:
    jobs: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    source_url: str = DEFAULT_SOURCE
    fetched_url: str = DEFAULT_RAW

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_url(url: str) -> str:
    parsed = urlparse(html.unescape(url.strip()))
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "referrer", "trackingid"}]
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", urlencode(sorted(query)), ""))


def _simplify(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in {"simplify.jobs", "www.simplify.jobs"} and parsed.path.startswith(("/p/", "/jobs/"))


def _clean(value: str) -> str:
    value = re.sub(r"<br\s*/?>", ", ", value, flags=re.I)
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*~`]", "", value)
    return " ".join(value.split()).strip(" 🔥🎓🛂🇺🇸")


def _urls(value: str) -> list[str]:
    soup = BeautifulSoup(value, "html.parser")
    urls = [str(a.get("href")) for a in soup.find_all("a", href=True)]
    urls.extend(re.findall(r"\]\((https?://[^\s)]+)(?:\s+[^)]*)?\)", value))
    return list(dict.fromkeys(html.unescape(u) for u in urls if u.startswith(("https://", "http://"))))


def _record(company: str, title: str, location: str, application: str, source_url: str,
            raw: str, company_cell: str = "") -> dict[str, Any] | None:
    urls = _urls(application)
    simplify = next((u for u in urls if _simplify(u)), None)
    direct = next((u for u in urls if not _simplify(u)), None)
    closed = "🔒" in application or bool(re.search(r"\b(?:closed|no longer available)\b", _clean(application), re.I))
    closed = closed or "<del" in raw or "<s>" in raw or "~~" in title
    if not company or not title or not (simplify or direct or closed):
        return None
    selected = simplify or direct
    if selected:
        selected = normalize_url(selected)
    simplify = normalize_url(simplify) if simplify else None
    direct = normalize_url(direct) if direct else None
    identifier = urlparse(simplify).path.split("/")[2] if simplify else None
    company, title, location = _clean(company), _clean(title), _clean(location)
    identity = f"simplify:{identifier}" if identifier else f"{company.casefold()}|{title.casefold()}|{location.casefold()}|{selected or 'closed'}"
    company_urls = _urls(company_cell)
    return {"company": company, "title": title, "location": location,
            "url": selected, "source_url": source_url, "simplify_url": simplify,
            "application_url": selected, "direct_url": direct,
            "simplify_id": identifier,
            "source_identity": hashlib.sha256(identity.encode()).hexdigest(),
            "closed": closed, "is_open": not closed, "description": "",
            "metadata": {"company_url": company_urls[0] if company_urls else None,
                         "source": "SimplifyJobs", "direct_url": direct,
                         "follow_simplify_first": bool(simplify)}}


def parse_readme(text: str, source_url: str = DEFAULT_SOURCE) -> list[dict[str, Any]]:
    """Parse current HTML tables and older Markdown tables without duplicate rows."""
    records = []
    soup = BeautifulSoup(text, "html.parser")
    for table in soup.find_all("table"):
        headers = [_clean(str(th)).lower() for th in table.find_all("th")]
        if not headers or "company" not in headers or not any(h in headers for h in ("role", "position", "title")):
            continue
        company_index = headers.index("company")
        role_index = next(headers.index(h) for h in ("role", "position", "title") if h in headers)
        location_index = next((headers.index(h) for h in ("location", "locations") if h in headers), 2)
        application_index = next((headers.index(h) for h in ("application", "application link", "apply", "link") if h in headers), 3)
        last_company, last_company_cell = "", ""
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) <= max(company_index, role_index, location_index, application_index):
                continue
            company_cell = str(cells[company_index])
            company = _clean(company_cell)
            if company in {"↳", "↪", "", "〃", '"'}:
                company, company_cell = last_company, last_company_cell
            else:
                last_company, last_company_cell = company, company_cell
            record = _record(company, str(cells[role_index]), str(cells[location_index]),
                             str(cells[application_index]), source_url, str(row), company_cell)
            if record:
                records.append(record)
    # Markdown tables are parsed separately. HTML td lines never begin with '|'.
    last_company, last_company_cell = "", ""
    headers: list[str] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
        cells = [cell.replace("\\|", "|").strip() for cell in cells]
        if len(cells) < 4:
            continue
        normalized = [_clean(c).lower() for c in cells]
        if "company" in normalized and any(x in normalized for x in ("role", "position", "title")):
            headers = normalized
            last_company, last_company_cell = "", ""
            continue
        if not headers or all(re.fullmatch(r"[:\-\s]*", cell) for cell in cells):
            continue
        def index(names: tuple[str, ...], default: int) -> int:
            return next((headers.index(n) for n in names if n in headers), default)
        ci, ri, li, ai = (index(("company",), 0), index(("role", "position", "title"), 1),
                          index(("location", "locations"), 2), index(("application", "application link", "apply", "link"), 3))
        if len(cells) <= max(ci, ri, li, ai):
            continue
        company_cell, company = cells[ci], _clean(cells[ci])
        if company in {"↳", "↪", "", "〃", '"'}:
            company, company_cell = last_company, last_company_cell
        else:
            last_company, last_company_cell = company, company_cell
        record = _record(company, cells[ri], cells[li], cells[ai], source_url, line, company_cell)
        if record:
            records.append(record)
    # A role can appear in several categories in the README. Keep the open form.
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record["source_identity"]
        if key not in unique or not record["closed"]:
            unique[key] = record
    return list(unique.values())


def raw_source_url(source_url: str = DEFAULT_SOURCE, branch: str = "dev", path: str = "README.md") -> str:
    parsed = urlparse(source_url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Repository source must be a public HTTPS GitHub URL")
    if parsed.hostname == "raw.githubusercontent.com":
        if len(parsed.path.strip("/").split("/")) < 4:
            raise ValueError("Raw GitHub source must identify owner/repository/branch/path")
        return source_url
    if parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Only GitHub repository sources are supported")
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError("Repository source needs owner/repository")
    if len(parts) >= 5 and parts[2] == "blob":
        branch, path = parts[3], "/".join(parts[4:])
    if any(value in {"", ".", ".."} for value in (parts[0], parts[1], branch)) or ".." in path.split("/"):
        raise ValueError("Invalid repository path")
    return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1].removesuffix('.git')}/{branch}/{path}"


async def fetch_jobs(source_url: str = DEFAULT_SOURCE, etag: str | None = None,
                     last_modified: str | None = None, *, branch: str = "dev", path: str = "README.md",
                     client: httpx.AsyncClient | None = None) -> FetchResult:
    raw_url = raw_source_url(source_url, branch, path)
    headers = {"Accept": "text/plain", "User-Agent": "jobpilot/1.0 (read-only repository discovery)"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=45, follow_redirects=False)
    try:
        response = await client.get(raw_url, headers=headers)
        if response.status_code == 304:
            return FetchResult(etag=etag, last_modified=last_modified, not_modified=True,
                               source_url=source_url, fetched_url=raw_url)
        response.raise_for_status()
        if len(response.content) > 15_000_000:
            raise ValueError("Repository README exceeds 15 MB size limit")
        jobs = parse_readme(response.text, source_url)
        if not jobs:
            raise ValueError("No job rows were recognized; preserve the existing queue and inspect source format")
        return FetchResult(jobs=jobs, etag=response.headers.get("etag"),
                           last_modified=response.headers.get("last-modified"),
                           source_url=source_url, fetched_url=raw_url)
    finally:
        if owned:
            await client.aclose()
