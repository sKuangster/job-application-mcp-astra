"""ATS account credentials and email verification.

Secrets never touch SQLite or logs: the accounts table stores only the *name* of the
environment variable (or keyring entry) holding the password. Credentials resolve per
domain (``preferences.accounts.domains``) and fall back to the default pair
``APPLYPILOT_ACCOUNT_EMAIL`` / ``APPLYPILOT_ACCOUNT_PASSWORD``.

Email verification is pluggable (``EmailProvider``); the bundled IMAP provider works with
Gmail/Outlook app passwords. Without a provider, verification parks the application in
NEEDS_USER instead of losing queue progress.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import email.utils
import imaplib
import logging
import os
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import urlsplit

from applypilot.config import AppConfig
from applypilot.identity import domain_key

log = logging.getLogger("applypilot.accounts")


@dataclass
class Credentials:
    username: str
    password: str
    secret_ref: str

    def __repr__(self) -> str:  # never print the password
        return f"Credentials(username={self.username!r}, secret_ref={self.secret_ref!r})"


def account_key(url: str) -> str:
    """Workday accounts are per tenant; other ATSs per domain key."""
    host = (urlsplit(url).hostname or "").lower()
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        seg = [p for p in urlsplit(url).path.split("/") if p and p not in {"en-US", "en-GB"}]
        return f"{host}/{seg[0] if seg else ''}".lower()
    return domain_key(url) or host


def _secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    try:  # optional OS keyring (Windows Credential Manager)
        import keyring  # type: ignore[import-not-found]

        return keyring.get_password("applypilot", name)
    except Exception:
        return None


def credentials_for(cfg: AppConfig, url: str) -> Credentials | None:
    prefs = cfg.preferences.accounts
    host = (urlsplit(url).hostname or "").lower()
    email_env, password_env = prefs.email_env, prefs.password_env
    for pattern, mapping in prefs.domains.items():
        if host == pattern or host.endswith("." + pattern) or pattern in account_key(url):
            email_env = mapping.get("email_env", email_env)
            password_env = mapping.get("password_env", password_env)
            break
    username = _secret(email_env) or (cfg.candidate.get("identity") or {}).get("email")
    password = _secret(password_env)
    if not username or not password:
        return None
    return Credentials(username=username, password=password, secret_ref=f"env:{password_env}")


# ------------------------------------------------------------------ email verification
@dataclass
class Verification:
    kind: str  # link | code
    value: str
    subject: str
    sender: str
    received_at: float


class EmailProvider(Protocol):
    name: str

    def configured(self) -> bool: ...

    async def find_verification(self, *, since: float, sender_domains: list[str], recipient: str | None,
                                timeout: float) -> Verification | None: ...


_LINK = re.compile(r"https?://[^\s\"'<>]+", re.I)
_CODE = re.compile(r"(?i:code|pin|otp|passcode)\b[^\n]{0,40}?\b(\d{4,8}|(?=[A-Z0-9]*\d)[A-Z0-9]{6,8})\b")
_VERIFY_WORDS = re.compile(r"verif|confirm|activat|validate|one[- ]time|sign[- ]?in code|security code", re.I)


def parse_verification(msg: EmailMessage, *, sender_domains: list[str]) -> Verification | None:
    subject = str(msg.get("subject") or "")
    sender = str(msg.get("from") or "")
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_content() + "\n"
                except Exception:
                    continue
    else:
        try:
            body = msg.get_content()
        except Exception:
            body = ""
    if not _VERIFY_WORDS.search(subject + " " + body[:4000]):
        return None
    if sender_domains:
        sender_l = sender.lower()
        compact = re.sub(r"[^a-z0-9@.]", "", sender_l)
        if not any(d.lower() in sender_l or d.lower() in compact for d in sender_domains):
            return None  # never use another service's code or link
    ts = time.time()
    date = msg.get("date")
    if date:
        try:
            ts = email.utils.parsedate_to_datetime(str(date)).timestamp()
        except (TypeError, ValueError):
            pass
    text = re.sub(r"<[^>]+>", " ", body)
    code = _CODE.search(text)
    links = [link.rstrip(").,;") for link in _LINK.findall(body)]
    verify_links = [link for link in links if re.search(r"verif|confirm|activat|validate|token", link, re.I)]
    if sender_domains:
        verify_links = [link for link in verify_links if any(d in link.lower() for d in sender_domains)]
    if code:
        return Verification("code", code.group(1), subject, sender, ts)
    if verify_links:
        return Verification("link", verify_links[0], subject, sender, ts)
    return None


class ImapEmailProvider:
    name = "imap"

    def __init__(self) -> None:
        self.host = os.environ.get("APPLYPILOT_IMAP_HOST", "")
        self.port = int(os.environ.get("APPLYPILOT_IMAP_PORT", "993") or 993)
        self.user = os.environ.get("APPLYPILOT_IMAP_USER", "")
        self.folder = os.environ.get("APPLYPILOT_IMAP_FOLDER", "INBOX")

    def configured(self) -> bool:
        return bool(self.host and self.user and os.environ.get("APPLYPILOT_IMAP_PASSWORD"))

    async def find_verification(self, *, since: float, sender_domains: list[str], recipient: str | None,
                                timeout: float = 120) -> Verification | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = await asyncio.to_thread(self._scan, since, sender_domains)
            if found:
                return found
            await asyncio.sleep(8)
        return None

    def _scan(self, since: float, sender_domains: list[str]) -> Verification | None:
        password = os.environ.get("APPLYPILOT_IMAP_PASSWORD", "")
        try:
            with imaplib.IMAP4_SSL(self.host, self.port) as conn:
                conn.login(self.user, password)
                conn.select(self.folder, readonly=True)
                date = time.strftime("%d-%b-%Y", time.gmtime(since - 86400))
                status, data = conn.search(None, f'(SINCE "{date}")')
                if status != "OK":
                    return None
                ids = data[0].split()[-30:]
                for msg_id in reversed(ids):
                    status, payload = conn.fetch(msg_id, "(RFC822)")
                    if status != "OK" or not payload or not isinstance(payload[0], tuple):
                        continue
                    msg = email.message_from_bytes(payload[0][1], policy=email.policy.default)
                    assert isinstance(msg, EmailMessage)
                    found = parse_verification(msg, sender_domains=sender_domains)
                    if found and found.received_at >= since - 120:
                        return found
        except (imaplib.IMAP4.error, OSError) as exc:
            log.warning("IMAP verification check failed: %s", type(exc).__name__)
        return None


class NullEmailProvider:
    name = "none"

    def configured(self) -> bool:
        return False

    async def find_verification(self, **_: Any) -> Verification | None:
        return None


def email_provider() -> EmailProvider:
    imap = ImapEmailProvider()
    return imap if imap.configured() else NullEmailProvider()
