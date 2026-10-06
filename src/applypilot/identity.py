"""Durable job identity and duplicate-detection keys.

A job is identified by several independent aliases so that small changes in the
repository (URL tracking params, title punctuation, moved listings) do not create a
new job, and two listings for the same underlying ATS posting collapse into one.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot.textutil import company_norm, stable_hash, title_norm

_TRACKING = {"ref", "referrer", "source", "src", "gh_src", "lever-source", "lever-origin", "fbclid", "gclid",
             "mc_cid", "mc_eid", "trk", "trackingid", "utm"}
UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

MULTI_TENANT_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workable.com", "rippling.com",
    "jobvite.com", "bamboohr.com", "recruitee.com", "breezy.hr", "teamtailor.com",
)


def normalize_url(url: str | None) -> str:
    """Canonical URL: https, lowercase host without www, no fragment/tracking params."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return ""
    host = parts.hostname.lower().removeprefix("www.")
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    path = re.sub(r"/+", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def host_of(url: str | None) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def detect_ats(url: str | None) -> str:
    host = host_of(url)
    path = (urlsplit(url or "").path or "").lower()
    table = [
        ("greenhouse", ("greenhouse.io",)), ("lever", ("lever.co",)), ("ashby", ("ashbyhq.com",)),
        ("workday", ("myworkdayjobs.com", "myworkdaysite.com", "workday.com")), ("icims", ("icims.com",)),
        ("smartrecruiters", ("smartrecruiters.com",)), ("workable", ("workable.com",)),
        ("oracle", ("oraclecloud.com",)), ("successfactors", ("successfactors.com", "sapsf.com")),
        ("taleo", ("taleo.net",)), ("jobvite", ("jobvite.com",)), ("rippling", ("rippling.com",)),
        ("bamboohr", ("bamboohr.com",)), ("simplify", ("simplify.jobs",)),
    ]
    for name, hosts in table:
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    if "/careers/" in path or "/jobs/" in path:
        return "custom"
    return "generic"


def ats_job_key(url: str | None, tracked_obj: str | None = None) -> str | None:
    """Platform-specific posting identity, e.g. ``greenhouse:databricks:8847738002``."""
    if tracked_obj:
        m = re.match(r"(\w+):(.+)", tracked_obj)
        if m:
            platform, rest = m.group(1).lower(), m.group(2)
            nums = re.findall(r"[A-Za-z0-9_-]+", rest)
            if platform == "greenhouse" and nums:
                return f"greenhouse:{nums[-1]}"
            return f"{platform}:{rest.lower()}"
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path
    qs = parse_qs(parts.query)
    if "greenhouse.io" in host:
        if "token" in qs:
            return f"greenhouse:{qs['token'][0]}"
        if "gh_jid" in qs:
            return f"greenhouse:{qs['gh_jid'][0]}"
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"greenhouse:{m.group(1)}"
    if "gh_jid" in qs:
        return f"greenhouse:{qs['gh_jid'][0]}"
    if "lever.co" in host:
        m = re.search(UUID_RE, path, re.I)
        if m:
            return f"lever:{m.group(0).lower()}"
    if "ashbyhq.com" in host:
        m = re.search(UUID_RE, path, re.I)
        if m:
            return f"ashby:{m.group(0).lower()}"
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        tenant = host.split(".")[0]
        m = re.search(r"_((?:JR|R|REQ|J)?-?\d{3,}[\w-]*)(?:/|$|\?)", path + "/", re.I)
        if m:
            return f"workday:{tenant}:{m.group(1).lower()}"
    if "icims.com" in host:
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"icims:{host.split('.')[0]}:{m.group(1)}"
    if "smartrecruiters.com" in host:
        m = re.search(r"/([^/]+)/(\d{6,})", path)
        if m:
            return f"smartrecruiters:{m.group(1).lower()}:{m.group(2)}"
    if "workable.com" in host:
        m = re.search(r"/j/([A-Za-z0-9]+)", path)
        if m:
            return f"workable:{m.group(1).upper()}"
    if "simplify.jobs" in host:
        m = re.search(UUID_RE, path, re.I)
        if m:
            return None  # Simplify ids are aliases, not ATS identities
    return None


def simplify_id_from_url(url: str | None) -> str | None:
    if not url or "simplify.jobs" not in url:
        return None
    m = re.search(UUID_RE, url, re.I)
    return m.group(0).lower() if m else None


def domain_key(url: str | None) -> str | None:
    """Unit of per-domain concurrency.

    Multi-tenant ATS hosts are split by tenant (first path segment) so two different
    companies on Greenhouse can run in parallel, while one Workday tenant / one company
    never sees concurrent applications (session and account conflicts).
    """
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return None
    if host in {"127.0.0.1", "localhost"}:
        seg = [p for p in parts.path.split("/") if p]
        return f"{host}:{parts.port or ''}/{seg[0] if seg else ''}"
    if any(host == h or host.endswith("." + h) for h in MULTI_TENANT_HOSTS):
        seg = [p for p in parts.path.split("/") if p and p not in {"embed", "en-US", "jobs", "j", "o"}]
        qs = parse_qs(parts.query)
        tenant = qs.get("for", [None])[0] or (seg[0] if seg else "")
        return f"{host}/{tenant.lower()}"
    return host


def dedupe_key(company: str, title: str) -> str:
    return f"{company_norm(company)}|{title_norm(title)}"


def job_aliases(*, simplify_id: str | None, ats_key: str | None, urls: list[str | None],
                company: str, title: str, locations: list[str]) -> list[tuple[str, str]]:
    """Aliases in resolution priority order (most specific first)."""
    aliases: list[tuple[str, str]] = []
    if simplify_id:
        aliases.append(("simplify", simplify_id))
    if ats_key:
        aliases.append(("ats", ats_key))
    for url in urls:
        norm = normalize_url(url)
        if norm and "simplify.jobs" not in norm:
            aliases.append(("url", norm))
    loc = "|".join(sorted(company_norm(x) for x in locations))
    aliases.append(("ctl", stable_hash(f"{company_norm(company)}|{title_norm(title)}|{loc}", 24)))
    return aliases


def job_id_for(simplify_id: str | None, company: str, title: str, url: str | None) -> str:
    basis = simplify_id or f"{company_norm(company)}|{title_norm(title)}|{normalize_url(url)}"
    return "job_" + stable_hash(basis, 20)


def summarize_duplicate(conflicts: list[dict[str, Any]]) -> str:
    parts = [f"{c.get('company')} – {c.get('title')} ({c.get('status') or c.get('state')})" for c in conflicts[:3]]
    return "; ".join(parts)
