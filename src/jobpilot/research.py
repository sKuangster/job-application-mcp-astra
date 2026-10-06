"""Bounded, cached company research with DNS-pinned public URL fetching.

Research is retrieval, not automatic factual truth: source text is explicitly
untrusted and returned with URLs/timestamps for Astra's contextual reasoning.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import socket
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from .intelligence import normalize_question


class UnsafeURL(ValueError):
    pass


def research_decision(question: str, cached: dict[str, Any] | None = None) -> dict[str, Any]:
    question = normalize_question(question)
    official_terms = ("company", "mission", "values", "products", "culture", "strategy", "programs", "why us")
    external_terms = ("industry", "market", "competitor", "news", "reputation")
    official = any(term in question for term in official_terms)
    external = any(term in question for term in external_terms)
    return {"needed": bool((official or external) and not cached),
            "prefer_official": official and not external,
            "reason": "Cached company context available" if cached else
                      "Company-specific detail will improve this response" if official or external else
                      "Candidate/job facts are sufficient; research would not improve this question"}


async def public_url_target(url: str) -> tuple[str, str]:
    """Validate all DNS answers, then pin the network request to a checked address."""
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeURL("Research URLs must be public HTTP(S) addresses without credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("Invalid URL port") from exc
    if port not in {80, 443}:
        raise UnsafeURL("Research only permits standard public web ports")
    host = parsed.hostname.rstrip(".")
    if host.lower() == "localhost" or host.lower().endswith((".localhost", ".local", ".internal")):
        raise UnsafeURL("Local hostnames are forbidden")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [str(literal)]
    except ValueError:
        try:
            info = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURL("Research hostname could not be resolved") from exc
        addresses = list(dict.fromkeys(result[4][0] for result in info))
    if not addresses:
        raise UnsafeURL("Research hostname has no addresses")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global):
            raise UnsafeURL("Private, loopback, link-local, multicast, and reserved targets are forbidden")
    # Prefer IPv4 for compatibility with desktops lacking working IPv6.
    address = next((a for a in addresses if ":" not in a), addresses[0])
    target_host = f"[{address}]" if ":" in address else address
    target = urlunparse((parsed.scheme, f"{target_host}:{port}", parsed.path or "/", "", parsed.query, ""))
    return target, host


async def fetch_public_page(url: str, *, max_bytes: int = 2_000_000,
                            max_chars: int = 18000, timeout: float = 20,
                            transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    original = url
    for _ in range(5):
        target, host = await public_url_target(url)
        # Direct IP + SNI prevents DNS rebinding between validation and connection.
        # A fresh client per redirect also prevents pooling a TLS connection across
        # distinct hostnames which happen to resolve to the same IP.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                                     trust_env=False, transport=transport) as client:
            async with client.stream("GET", target, headers={"Host": host,
                    "User-Agent": "jobpilot/1.0 (company research)", "Accept": "text/html,text/plain"},
                    extensions={"sni_hostname": host}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Research redirect has no destination")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if not any(t in content_type for t in ("text/html", "text/plain", "application/xhtml+xml")):
                    raise ValueError("Research accepts HTML/text pages only")
                chunks, total = [], 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("Research page exceeds download size limit")
                    chunks.append(chunk)
                text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        soup = BeautifulSoup(text, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        for element in soup(["script", "style", "noscript", "nav", "footer", "header", "form"]):
            element.decompose()
        content = soup.find("main") or soup.find("article") or soup
        extracted = " ".join(content.get_text(" ", strip=True).split())[:max_chars]
        return {"url": url, "requested_url": original, "title": title, "text": extracted,
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "untrusted_source_content": True}
    raise UnsafeURL("Too many research redirects")


class ResearchService:
    def __init__(self, db: Any, max_concurrent: int = 2, ttl_seconds: int = 604800,
                 max_pages: int = 3, fetcher: Any = None):
        self.db = db
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.ttl_seconds, self.max_pages = ttl_seconds, max_pages
        self.fetcher = fetcher or fetch_public_page
        self._inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self.calls = 0
        self.cache_hits = 0

    @staticmethod
    def key(company: str, topic: str) -> str:
        return hashlib.sha256(f"{normalize_question(company)}|{normalize_question(topic)}".encode()).hexdigest()

    def cached(self, company: str, topic: str = "company") -> dict[str, Any] | None:
        return self.db.cache_get("research", self.key(company, topic))

    async def get(self, company: str, urls: list[str | dict[str, Any]], topic: str = "company",
                  force: bool = False) -> dict[str, Any]:
        key = self.key(company, topic)
        cached = self.db.cache_get("research", key)
        if cached and not force:
            self.cache_hits += 1
            return {**cached, "cache_hit": True}
        if not urls:
            return {"company": company, "topic": topic, "sources": [], "needs_sources": True,
                    "reason": "Configure research.company_sources or provide appropriate source URLs through Astra"}
        # All application workers in this process share exactly one task. Shielding
        # means a cancelled worker cannot cancel research other workers still need.
        if key not in self._inflight:
            task = asyncio.create_task(self._retrieve(company, urls, topic, key, force))
            self._inflight[key] = task
            task.add_done_callback(lambda completed, k=key: self._inflight.pop(k, None))
        return await asyncio.shield(self._inflight[key])

    async def _retrieve(self, company: str, urls: list[str | dict[str, Any]], topic: str,
                        key: str, force: bool) -> dict[str, Any]:
        owner = str(uuid.uuid4())
        # Namespace differs for an explicit refresh so the normal cache may remain
        # readable while one refresher works. Persistent leases cover processes.
        lease_key = key + (":refresh" if force else "")
        deadline = time.monotonic() + 65
        while not self.db.cache_acquire("research", lease_key, owner, lease_seconds=90):
            cached = self.db.cache_get("research", key)
            if cached and not force:
                self.cache_hits += 1
                return {**cached, "cache_hit": True}
            if time.monotonic() > deadline:
                raise TimeoutError("Another research worker still owns this company; retry later")
            await asyncio.sleep(0.15)
        try:
            async with self.semaphore:
                self.calls += 1
                sources = [dict(source) if isinstance(source, dict) else {"url": source, "official": False}
                           for source in urls]
                policy = research_decision(topic)
                if policy["prefer_official"]:
                    sources.sort(key=lambda source: not source.get("official", False))
                sources = list({source["url"]: source for source in sources if source.get("url")}.values())[:self.max_pages]
                pages, errors = [], []
                for source in sources:
                    try:
                        page = await self.fetcher(source["url"])
                        pages.append({**page, "official": bool(source.get("official"))})
                    except (httpx.HTTPError, ValueError, OSError, TimeoutError) as exc:
                        errors.append({"url": source["url"], "error": str(exc)[:300]})
                result = {"company": company, "topic": topic, "sources": pages, "errors": errors,
                          "retrieved_at": datetime.now(timezone.utc).isoformat(),
                          "cache_hit": False, "source_policy": policy,
                          "needs_sources": not pages,
                          "content_hash": hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest()}
                if pages:
                    if force:
                        self.db.cache_set("research", key, result, ttl_seconds=self.ttl_seconds)
                        self.db.cache_release("research", lease_key, owner)
                    elif not self.db.cache_complete("research", key, owner, result, ttl_seconds=self.ttl_seconds):
                        raise RuntimeError("Research lease expired before cache completion")
                else:
                    self.db.cache_release("research", lease_key, owner)
                return result
        except BaseException:
            self.db.cache_release("research", lease_key, owner)
            raise
