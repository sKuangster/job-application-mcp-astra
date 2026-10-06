"""Environment-backed ATS credentials and narrowly scoped verification email support."""
from __future__ import annotations

import asyncio
import email
import imaplib
import os
import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


@dataclass(repr=False)
class Verification:
    code: str | None = None
    url: str | None = None
    message_id: str | None = None


class EmailProvider(Protocol):
    async def find(self, *, recipient: str, senders: list[str], domains: list[str],
                   since: float) -> Verification | None: ...


def parse_verification(raw: bytes, *, recipient: str, senders: list[str],
                       domains: list[str], since: float) -> Verification | None:
    message = email.message_from_bytes(raw)
    sender = email.utils.parseaddr(message.get("From", ""))[1].lower()
    allowed = any(sender == s.lower() or (s.startswith("@") and sender.endswith(s.lower())) for s in senders)
    targets = [a.lower() for _, a in email.utils.getaddresses(
        [message.get("To", ""), message.get("Delivered-To", "")])]
    if not allowed or recipient.lower() not in targets:
        return None
    try:
        sent = parsedate_to_datetime(message.get("Date", "")).timestamp()
    except (TypeError, ValueError):
        return None
    if sent < since - 60 or sent > time.time() + 300:
        return None
    texts = []
    for part in message.walk():
        if part.get_content_type() in {"text/plain", "text/html"} and part.get_content_disposition() != "attachment":
            content = part.get_payload(decode=True) or b""
            texts.append(content.decode(part.get_content_charset() or "utf-8", errors="replace"))
    text = "\n".join(texts)[:100_000]
    if not re.search(r"verif|confirm|one.time|security code", message.get("Subject", "") + text, re.I):
        return None
    soup = BeautifulSoup(text, "html.parser")
    links = [a.get("href", "") for a in soup.find_all("a")] + re.findall(r"https://[^\s<>\"']+", text)
    urls = []
    for link in links:
        p = urlsplit(link.rstrip(".,)"))
        if (p.scheme == "https" and p.hostname in domains and not p.username and not p.password
                and re.search(r"verif|confirm|activate", p.path + p.query, re.I)):
            urls.append(p.geturl())
    codes = re.findall(r"(?:code|OTP)[\s:–-]*(\d{4,8})\b", soup.get_text(" "), re.I)
    # Ambiguous mail is left for the user. Never follow arbitrary links from inbox text.
    if len(set(urls)) == 1:
        return Verification(url=urls[0], message_id=message.get("Message-ID"))
    if len(set(codes)) == 1:
        return Verification(code=codes[0], message_id=message.get("Message-ID"))
    return None


class ImapEmailProvider:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.seen: set[str] = set()

    @property
    def configured(self) -> bool:
        return all(os.environ.get(k) for k in (
            "JOBPILOT_IMAP_HOST", "JOBPILOT_IMAP_USER", "JOBPILOT_IMAP_PASSWORD"))

    async def find(self, **kwargs) -> Verification | None:
        if not self.configured:
            return None
        async with self.lock:
            return await asyncio.to_thread(self._find, **kwargs)

    def _find(self, **kwargs) -> Verification | None:
        with imaplib.IMAP4_SSL(os.environ["JOBPILOT_IMAP_HOST"],
                              int(os.environ.get("JOBPILOT_IMAP_PORT", "993")), timeout=15) as inbox:
            inbox.login(os.environ["JOBPILOT_IMAP_USER"], os.environ["JOBPILOT_IMAP_PASSWORD"])
            inbox.select(os.environ.get("JOBPILOT_IMAP_FOLDER", "INBOX"), readonly=True)
            status, data = inbox.search(None, "ALL")
            if status != "OK":
                return None
            for number in reversed(data[0].split()[-30:]):
                status, parts = inbox.fetch(number, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                for part in parts:
                    if isinstance(part, tuple):
                        result = parse_verification(part[1], **kwargs)
                        if result and result.message_id and result.message_id not in self.seen:
                            self.seen.add(result.message_id)
                            return result
        return None


def credentials(preferences: dict, url: str) -> tuple[str, str] | None:
    domain = urlsplit(url).hostname
    mapping = preferences.get("accounts", {}).get("domains", {}).get(domain, {})
    if not mapping or not mapping.get("allow_automation", False):
        return None
    user = os.environ.get(mapping.get("email_env", ""), "")
    password = os.environ.get(mapping.get("password_env", ""), "")
    return (user, password) if user and password else None
