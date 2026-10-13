"""Bounded public-document retrieval: validate every redirect and pin DNS results.

No cookies, proxy environment, scripts, browser automation or private-IP access.
Inspected quotations, rather than search snippets or generated summaries, are facts.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from applypilot.textutil import clean

INJECTION = re.compile(r"ignore (?:all |the |any )?(?:previous|prior|system) instructions|system prompt|"
                       r"send (?:your |the )?(?:credentials|password|api key)|\[INST\]|<\|(?:system|im_start)\|>", re.I)


def validate_url(url: str) -> None:
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or parts.port not in {None, 80, 443}):
        raise ValueError("unsafe public research URL")
    if any(key.casefold() in {"token", "password", "api_key", "access_token", "state", "code"} for key, _ in parse_qsl(parts.query)):
        raise ValueError("credential-bearing research URL")
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        if parts.hostname.casefold() == "localhost" or parts.hostname.casefold().endswith((".local", ".localhost")):
            raise ValueError("private research hostname") from None
    else:
        if not address.is_global:
            raise ValueError("private research address")


async def public_ip(url: str) -> str:
    validate_url(url)
    parts = urlsplit(url)
    addresses = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global for info in addresses):
        raise ValueError("research hostname does not resolve exclusively to public addresses")
    return str(addresses[0][4][0])


def document_text(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = clean(soup.title.get_text()) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    for element in soup.select('[hidden], [aria-hidden="true"]'):
        element.decompose()
    quotes = []
    for element in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        text = clean(element.get_text(" "))
        if 35 <= len(text) <= 800 and not INJECTION.search(text) and text not in quotes:
            quotes.append(text)
        if len(quotes) >= 30:
            break
    return title[:200], quotes


class PublicFetcher:
    def __init__(self, *, timeout: float = 15, max_bytes: int = 2_000_000, client_factory: Any = None):
        self.timeout, self.max_bytes, self.client_factory = timeout, max_bytes, client_factory

    async def fetch(self, url: str) -> dict[str, Any]:
        factory = self.client_factory or (lambda: httpx.AsyncClient(timeout=self.timeout, trust_env=False))
        async with asyncio.timeout(self.timeout), factory() as client:
            for _ in range(4):
                validate_url(url)
                logical = httpx.URL(url)
                address = await public_ip(url) if self.client_factory is None else logical.host
                target = logical.copy_with(host=address)
                async with client.stream("GET", target, headers={"Host": logical.netloc.decode(),
                                          "User-Agent": "ApplyPilot/1.0 public-research"},
                                          extensions={"sni_hostname": logical.host}, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    if "text/html" not in response.headers.get("content-type", ""):
                        raise ValueError("research document is not HTML")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self.max_bytes:
                            raise ValueError("research document exceeds size limit")
                    text = raw.decode(response.encoding or "utf-8", errors="replace")
                    title, quotes = document_text(text)
                    return {"url": str(logical), "title": title, "quotes": quotes,
                            "inspected": True, "content_hash": hashlib.sha256(raw).hexdigest(),
                            "prompt_injection_detected": bool(INJECTION.search(text))}
        raise ValueError("research redirect limit exceeded")
