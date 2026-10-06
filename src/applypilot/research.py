"""Company research: decide when it is needed, gather it once per company, cache it.

Decision layer: research is only gathered for questions about the company itself
(why-company, mission/values/products, cover letters, company-named prompts). Sources:

1. Simplify's company profile (description, differentiation, recent news) — already
   fetched with the job, zero extra latency.
2. The company's OFFICIAL website (homepage + about/mission/values/careers pages) for
   mission, values, products and stated culture.
3. Astra may add externally researched material through ``submit_reasoning`` /
   ``save_company_research`` (it has web search); that is cached the same way.

Concurrency: a per-company DB lock gives single-flight across workers and processes, so
five jobs at one company trigger one research run.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
import uuid
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from applypilot.config import AppConfig
from applypilot.db import Database
from applypilot.textutil import clean, company_norm, truncate

log = logging.getLogger("applypilot.research")
_KEYWORDS = re.compile(r"\b(mission|values?|culture|believe|build|product|platform|customers?|help|"
                       r"vision|innovation|impact|engineering|research|team|founded|serve)\b", re.I)
_PATHS = ["", "/about", "/about-us", "/company", "/mission", "/values", "/careers", "/culture"]


class UnsafeURL(ValueError):
    pass


def research_decision(question: str, qclass: str, template: str | None, company: str) -> bool:
    if qclass != "WRITTEN":
        return False
    if template in {"why_company", "cover_letter"}:
        return True
    q = question.casefold()
    return bool(re.search(r"\b(mission|values|culture|products?|why (us|our)|our company|about us)\b", q) or
                (company and company_norm(company) and company_norm(company) in company_norm(q)))


async def assert_public(url: str) -> None:
    """SSRF guard: only http(s) to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UnsafeURL(f"unsupported URL {url!r}")
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise UnsafeURL(f"{parts.hostname} resolves to non-public address {addr}")


def extract_relevant(html: str, limit: int = 2500) -> tuple[str, list[str], str]:
    soup = BeautifulSoup(html, "html.parser")
    title = clean(soup.title.get_text()) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    sentences: list[str] = []
    meta = soup.find("meta", attrs={"name": "description"})
    content = meta.get("content") if meta is not None and hasattr(meta, "get") else None
    if content:
        sentences.append(clean(str(content)))
    for el in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        text = clean(el.get_text(" "))
        if 40 <= len(text) <= 400 and _KEYWORDS.search(text) and text not in sentences:
            sentences.append(text)
        if sum(len(s) for s in sentences) > limit:
            break
    return title, sentences[:25], truncate(" ".join(sentences), limit)


class ResearchService:
    def __init__(self, db: Database, cfg: AppConfig, *, client_factory: Any = None):
        self.db = db
        self.cfg = cfg
        self._sem = asyncio.Semaphore(max(1, cfg.settings.automation.max_concurrent_research_tasks))
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self.owner = f"research-{uuid.uuid4().hex[:8]}"
        self.client_factory = client_factory
        self.metrics = {"runs": 0, "cache_hits": 0, "pages_fetched": 0, "failures": 0}

    def cached(self, company: str) -> list[dict[str, Any]]:
        return self.db.research(company_norm(company), fresh_only=True)

    def in_flight(self, company: str) -> bool:
        task = self._inflight.get(company_norm(company))
        return bool(task and not task.done())

    def schedule(self, company: str, job: dict[str, Any]) -> asyncio.Task[Any] | None:
        """Start research in the background (non-blocking for browser workers)."""
        if not self.cfg.settings.research.enabled:
            return None
        key = company_norm(company)
        if self.cached(company):
            self.metrics["cache_hits"] += 1
            return None
        task = self._inflight.get(key)
        if task and not task.done():
            return task
        task = asyncio.create_task(self.ensure(company, job))
        self._inflight[key] = task
        return task

    async def ensure(self, company: str, job: dict[str, Any]) -> list[dict[str, Any]]:
        key = company_norm(company)
        existing = self.cached(company)
        if existing:
            return existing
        lock = f"research:{key}"
        # Single-flight across processes: wait (bounded) for another holder to finish.
        for _ in range(60):
            if self.db.acquire_lock(lock, self.owner, ttl=120):
                break
            await asyncio.sleep(1)
            existing = self.cached(company)
            if existing:
                return existing
        else:
            return self.cached(company)
        try:
            existing = self.cached(company)
            if existing:
                return existing
            async with self._sem:
                self.metrics["runs"] += 1
                await self._run(company, job)
            return self.cached(company)
        except Exception as exc:  # research is best-effort; never fail an application on it
            self.metrics["failures"] += 1
            log.warning("research for %s failed: %s", company, exc)
            return self.cached(company)
        finally:
            self.db.release_lock(lock, self.owner)

    async def _run(self, company: str, job: dict[str, Any]) -> None:
        details = job.get("details") or {}
        profile = details.get("company") or {}
        ttl = self.cfg.settings.research.cache_ttl_seconds
        key = company_norm(company)
        now = time.time()
        if profile.get("description") or profile.get("short_description"):
            facts = [x for x in [profile.get("short_description"), *(profile.get("differentiation") or [])[:5]] if x]
            news = [f"{n.get('title')} ({n.get('publisher')}, {str(n.get('published_at'))[:10]})"
                    for n in (profile.get("news") or [])[:3] if n.get("title")]
            summary = truncate(profile.get("description") or profile.get("short_description") or "", 2000)
            if news:
                summary += "\nRecent news: " + "; ".join(news)
            sources = [{"url": details.get("simplify_page_url") or job.get("simplify_url"), "title": "Simplify company profile",
                        "official": False, "retrieved_at": now}]
            sources += [{"url": n.get("url"), "title": n.get("title"), "official": False, "retrieved_at": now}
                        for n in (profile.get("news") or [])[:3] if n.get("url")]
            self.db.save_research(company=company, company_norm=key, topic="profile", summary=summary,
                                  facts=[str(f) for f in facts], sources=sources, origin="simplify", ttl=ttl)
        urls: list[str] = []
        configured = self.cfg.preferences.research.company_sources.get(company) or []
        for src in configured:
            urls.append(src["url"] if isinstance(src, dict) else str(src))
        website = profile.get("url")
        if website and self.cfg.settings.research.max_pages > 0:
            urls += [urljoin(website.rstrip("/") + "/", p.lstrip("/")) for p in _PATHS]
        if not urls:
            return
        collected: list[tuple[str, str, list[str]]] = []
        own = self.client_factory is None
        client = self.client_factory() if self.client_factory else httpx.AsyncClient(
            timeout=self.cfg.settings.research.fetch_timeout, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"})
        try:
            seen: set[str] = set()
            for url in urls:
                if len(collected) >= self.cfg.settings.research.max_pages:
                    break
                if url in seen:
                    continue
                seen.add(url)
                try:
                    if own:
                        await assert_public(url)
                    resp = await client.get(url)
                    if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
                        continue
                    if len(resp.content) > self.cfg.settings.research.max_bytes:
                        continue
                    self.metrics["pages_fetched"] += 1
                    title, sentences, _ = extract_relevant(resp.text)
                    if sentences:
                        collected.append((str(resp.url), title, sentences))
                except (httpx.HTTPError, UnsafeURL, OSError, socket.gaierror) as exc:
                    log.debug("research fetch %s failed: %s", url, exc)
        finally:
            if own:
                await client.aclose()
        if collected:
            facts = [s for _, _, sents in collected for s in sents][:20]
            summary = truncate("\n".join(f"[{title or url}] " + " ".join(sents[:4]) for url, title, sents in collected), 4000)
            sources = [{"url": url, "title": title, "official": True, "retrieved_at": now} for url, title, _ in collected]
            self.db.save_research(company=company, company_norm=key, topic="official", summary=summary, facts=facts,
                                  sources=sources, origin="official_site", ttl=ttl)
