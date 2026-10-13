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
import hashlib
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
from applypilot.evidence import PublicFetcher
from applypilot.perplexica import PerplexicaAdapter
from applypilot.textutil import clean, company_norm, stable_hash, truncate
from applypilot.writing import research_plan

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
    def __init__(self, db: Database, cfg: AppConfig, *, client_factory: Any = None, perplexica_client_factory: Any = None):
        self.db = db
        self.cfg = cfg
        self._sem = asyncio.Semaphore(max(1, cfg.settings.automation.max_concurrent_research_tasks))
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self.owner = f"research-{uuid.uuid4().hex[:8]}"
        self.closing = False
        self.client_factory = client_factory
        self.perplexica = PerplexicaAdapter(cfg.settings.research.perplexica, client_factory=perplexica_client_factory)
        self.fetcher = PublicFetcher(timeout=cfg.settings.research.fetch_timeout,
                                     max_bytes=cfg.settings.research.max_bytes, client_factory=client_factory)
        self.metrics = {"runs": 0, "cache_hits": 0, "pages_fetched": 0, "failures": 0, "requests": 0}

    def cached(self, company: str) -> list[dict[str, Any]]:
        return [r for r in self.db.research(company_norm(company), fresh_only=True)
                if r["topic"] in {"company", "profile", "official"}]

    def in_flight(self, company: str) -> bool:
        task = self._inflight.get(company_norm(company))
        return bool(task and not task.done())

    def context_for(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        company = job.get("company") or ""
        return self.cached(company) + self.scoped(company, job, "role") + self.scoped(company, job, "technology")

    async def shutdown(self) -> None:
        self.closing = True
        tasks = [task for task in self._inflight.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def schedule(self, company: str, job: dict[str, Any]) -> asyncio.Task[Any] | None:
        """Start research in the background (non-blocking for browser workers)."""
        if not self.cfg.settings.research.enabled or self.closing:
            return None
        key = company_norm(company)
        if self.cached(company):
            self.metrics["cache_hits"] += 1
            return None
        task = self._inflight.get(key)
        if task and not task.done():
            return task
        task = asyncio.create_task(self.ensure_scope(company, job, "company"))
        self._inflight[key] = task
        return task

    def scope_key(self, company: str, job: dict[str, Any], scope: str) -> str:
        return scope if scope == "company" else f"{scope}:{stable_hash({'role': job.get('id') or job.get('title'), 'company': company_norm(company)}, 20)}"

    def scoped(self, company: str, job: dict[str, Any], scope: str) -> list[dict[str, Any]]:
        return self.cached(company) if scope == "company" else self.db.research(
            company_norm(company), topic=self.scope_key(company, job, scope), fresh_only=True)

    @staticmethod
    def sufficient(rows: list[dict[str, Any]]) -> bool:
        return any(r["facts"] and any(s.get("inspected") or s.get("source_type") == "stored_job_posting"
                                     for s in r["sources"]) for r in rows)

    async def prepare(self, item: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        plan = research_plan(item.get("question", ""), item.get("qclass", ""), item.get("template"), job.get("company", ""))
        if not self.cfg.settings.research.enabled or self.cfg.settings.research.provider == "none":
            return {**plan, "sufficient": True, "disabled": True, "evidence": []}
        rows = []
        sufficient = True
        for scope in plan["scopes"]:
            gathered = await self.ensure_scope(job.get("company", ""), job, scope)
            rows.extend(gathered)
            sufficient &= self.sufficient(gathered)
        evidence = []
        for row in rows:
            for source in row["sources"]:
                if not source.get("inspected") and source.get("source_type") != "stored_job_posting":
                    continue
                for quote in source.get("quotes", [])[:5]:
                    evidence.append({"id": "source_" + hashlib.sha256((source["url"] + quote).encode()).hexdigest()[:20],
                                     "quote": quote, "url": source["url"], "title": source.get("title"),
                                     "retrieved_at": source.get("retrieved_at"), "source_type": source.get("source_type"),
                                     "company_reported": source.get("company_reported"), "research_id": row["id"]})
        uncertainties = ["Company culture and values are company-reported, not independently verified employee experience."] if any(
            e.get("company_reported") for e in evidence) else []
        years = {match.group(1) for e in evidence for match in re.finditer(r"founded (?:in )?(\d{4})", e["quote"], re.I)}
        if len(years) > 1:
            uncertainties.append("Sources disagree on founding year; omit that claim until resolved.")
        return {**plan, "sufficient": bool(sufficient), "evidence": evidence[:18], "uncertainty": uncertainties}

    async def ensure_scope(self, company: str, job: dict[str, Any], scope: str) -> list[dict[str, Any]]:
        topic = self.scope_key(company, job, scope)
        task_key = f"research:{company_norm(company)}:{topic}"
        existing = self.scoped(company, job, scope)
        if self.sufficient(existing):
            self.metrics["cache_hits"] += 1
            return existing
        owner = self.owner + ":" + uuid.uuid4().hex[:8]
        ttl = self.cfg.settings.research.perplexica.timeout + self.cfg.settings.research.max_pages * self.cfg.settings.research.fetch_timeout + 30
        if not self.db.acquire_lock(task_key, owner, ttl=ttl):
            # Caller records a durable retry; do not duplicate another process's work.
            return existing
        attempts = 0
        try:
            existing = self.scoped(company, job, scope)
            if self.sufficient(existing):
                return existing
            states = self.db.query("SELECT * FROM research_tasks WHERE task_key=?", (task_key,))
            state: dict[str, Any] = states[0] if states else {"attempts": 0, "available_at": 0, "state": "PENDING"}
            now = self.db.clock()
            if state["available_at"] > now:
                return existing
            # Expired successful cache gets a new bounded refresh budget.
            attempts = 0 if state["state"] == "DONE" else state["attempts"]
            if attempts >= self.cfg.settings.research.max_requests_per_scope:
                return existing
            attempts += 1
            self._research_state(task_key, company, scope, "RUNNING", attempts, now, {})
            async with self._sem:
                self.metrics["runs"] += 1
                documents = await self._scope_documents(company, job, scope)
            if not documents:
                raise ValueError("no inspected relevant documents")
            facts = [quote for doc in documents for quote in doc["quotes"]][:25]
            ttl = {"company": self.cfg.settings.research.cache_ttl_seconds, "role": self.cfg.settings.research.role_ttl_seconds,
                   "technology": self.cfg.settings.research.technology_ttl_seconds}[scope]
            self.db.save_research(company=company, company_norm=company_norm(company), topic=topic,
                                  summary=truncate("\n".join(facts), 4000), facts=facts, sources=documents,
                                  origin="perplexica_verified" if any(d.get("discovery") == "perplexica" for d in documents) else "official_site", ttl=ttl)
            self._research_state(task_key, company, scope, "DONE", attempts, 0,
                                 {"source_count": len(documents), "sufficient": True})
            return self.scoped(company, job, scope)
        except Exception as exc:
            self.metrics["failures"] += 1
            retry = self.cfg.settings.research.retry_seconds * 2 ** (attempts - 1)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                header = exc.response.headers.get("retry-after", "")
                if header.isdigit():
                    retry = max(retry, min(86400, int(header)))
            self._research_state(task_key, company, scope, "FAILED", attempts,
                                 self.db.clock() + retry,
                                 {"error": type(exc).__name__, "sufficient": False})
            log.warning("research scope %s failed: %s", scope, type(exc).__name__)
            return self.scoped(company, job, scope)
        finally:
            self.db.release_lock(task_key, owner)

    def _research_state(self, key: str, company: str, scope: str, state: str, attempts: int,
                        available_at: float, diagnostics: dict[str, Any]) -> None:
        import json

        self.db.write(lambda c: c.execute("INSERT INTO research_tasks VALUES (?,?,?,?,?,?,?,?) "
                                         "ON CONFLICT(task_key) DO UPDATE SET state=excluded.state,attempts=excluded.attempts,"
                                         "available_at=excluded.available_at,updated_at=excluded.updated_at,diagnostics=excluded.diagnostics",
                                         (key, company, scope, state, attempts, available_at, self.db.clock(), json.dumps(diagnostics))))

    async def _scope_documents(self, company: str, job: dict[str, Any], scope: str) -> list[dict[str, Any]]:
        details = job.get("details") or {}
        profile = details.get("company") or {}
        if scope == "role" and len(details.get("description_text") or "") >= 80:
            quote = truncate(details["description_text"], 3500)
            return [{"url": job.get("apply_url") or job.get("direct_url") or "stored-job:" + str(job.get("id", "")),
                     "title": job.get("title"), "quotes": [quote], "source_type": "stored_job_posting",
                     "reliability": "employer_or_listing_reported", "company_reported": True,
                     "retrieved_at": job.get("details_fetched_at") or job.get("first_seen") or self.db.clock(), "inspected": False}]
        dimensions = research_plan("Why here?", "WRITTEN", "why_company", company)["dimensions"]
        dimension = dimensions.get(scope, "engineering blogs, technology, technical projects and relevant recent developments")
        query = f"{company[:150]} {str(job.get('title') or '')[:150] if scope != 'company' else ''}: {dimension}. Prefer original sources."
        configured = self.cfg.preferences.research.company_sources.get(company) or []
        urls = [s["url"] if isinstance(s, dict) else str(s) for s in configured]
        if profile.get("url"):
            urls.append(profile["url"])
        discovered_urls: set[str] = set()
        if self.cfg.settings.research.provider == "perplexica":
            self.metrics["requests"] += 1
            try:
                discovered = await self.perplexica.search(query)
                discovered_urls = {s["url"] for s in discovered}
                urls = [s["url"] for s in discovered] + urls
                self.db.meta_set("research_provider:perplexica", {"state": "ready", "last_search": self.db.clock(), "source_count": len(discovered)})
            except Exception as exc:
                self.db.meta_set("research_provider:perplexica", {"state": "unavailable", "last_search": self.db.clock(), "error": type(exc).__name__})
                # Narrow official-document fallback; never use model knowledge or snippets as research.
                if not urls:
                    raise
        documents: list[dict[str, Any]] = []
        expected = terms_for_research(company, job, scope)
        for url in list(dict.fromkeys(urls))[:self.cfg.settings.research.max_pages]:
            if len(documents) >= self.cfg.settings.research.max_pages:
                break
            try:
                document = await self.fetcher.fetch(url)
                quotes = [q for q in document["quotes"] if expected & set(re.findall(r"[a-z]{3,}", q.casefold()))]
                if not quotes:
                    continue
                official_host = urlsplit(str(profile.get("url") or "")).hostname
                host = urlsplit(document["url"]).hostname or ""
                official = bool(official_host and (host == official_host or host.endswith("." + official_host)))
                document.update(quotes=quotes, source_type="company_document" if official else "external_document",
                                reliability="company_reported" if official else "publisher_reported",
                                discovery="perplexica" if url in discovered_urls else "configured_source",
                                company_reported=official, official=official, retrieved_at=self.db.clock())
                documents.append(document)
                self.metrics["pages_fetched"] += 1
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    raise
                continue
            except (httpx.HTTPError, ValueError, TimeoutError, OSError):
                continue
        return documents

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
                    document = await self.fetcher.fetch(url)
                    self.metrics["pages_fetched"] += 1
                    title, sentences = document["title"], document["quotes"]
                    if sentences:
                        collected.append((document["url"], title, sentences))
                except (httpx.HTTPError, ValueError, OSError, TimeoutError, socket.gaierror) as exc:
                    log.debug("research fetch %s failed: %s", url, exc)
        finally:
            if own:
                await client.aclose()
        if collected:
            facts = [s for _, _, sents in collected for s in sents][:20]
            summary = truncate("\n".join(f"[{title or url}] " + " ".join(sents[:4]) for url, title, sents in collected), 4000)
            sources = [{"url": url, "title": title, "official": True, "retrieved_at": now, "inspected": True,
                        "quotes": sentences, "source_type": "company_document", "company_reported": True}
                       for url, title, sentences in collected]
            self.db.save_research(company=company, company_norm=key, topic="official", summary=summary, facts=facts,
                                  sources=sources, origin="official_site", ttl=ttl)


def terms_for_research(company: str, job: dict[str, Any], scope: str) -> set[str]:
    text = company + " " + (str(job.get("title") or "") if scope != "company" else "")
    return set(re.findall(r"[a-z]{3,}", text.casefold())) | {"mission", "engineering", "products", "platform", "mentorship", "values"}
