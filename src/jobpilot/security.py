"""Local authentication, redaction, and outbound URL checks."""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv

SECRET_KEY = re.compile(r"password|passwd|secret|token|authorization|cookie|otp|verification_code|data_base64", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET_KEY.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        # Known secrets in errors and site text must never reach logs.
        for key, secret in os.environ.items():
            if SECRET_KEY.search(key) and len(secret) >= 6:
                value = value.replace(secret, "[REDACTED]")
    return value


def safe_url(url: str, *, demo: bool = False) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only credential-free HTTP(S) application URLs are supported")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        if not demo:
            raise ValueError("Local application URLs require demo mode")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global and not (demo and address.is_loopback):
        raise ValueError("Private network application URL rejected")
    if parsed.scheme != "https" and not (demo and (host == "localhost" or address and address.is_loopback)):
        raise ValueError("Application URLs must use HTTPS")
    return url


def audit_url(url: str) -> str:
    parsed = urlsplit(url)
    # Verification tokens can be in any query key; URLs in logs omit query and fragment.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def load_token(root: Path, *, create: bool = False) -> str:
    load_dotenv(root / ".env", override=False)
    token = os.environ.get("JOBPILOT_TOKEN", "").strip()
    if not token and create:
        token = secrets.token_urlsafe(32)
        path = root / ".env"
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        old = re.sub(r"(?m)^JOBPILOT_TOKEN=.*\n?", "", old)
        path.write_text(old + f"\nJOBPILOT_TOKEN={token}\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)
        os.environ["JOBPILOT_TOKEN"] = token
    if len(token) < 32:
        raise ValueError("Run jobpilot init; JOBPILOT_TOKEN must contain at least 32 characters")
    return token
