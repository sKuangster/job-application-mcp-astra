"""Secret handling: redaction for logs/audit/MCP responses and token management."""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SECRET_KEYS = {
    "password", "passwd", "pass", "secret", "api_key", "apikey", "authorization", "token",
    "access_token", "refresh_token", "cookie", "cookies", "set-cookie", "session", "otp",
    "verification_code", "code_value", "client_secret", "base64", "file_base64",
}
_SECRET_QUERY = re.compile(r"(token|key|secret|password|code|sig|signature|auth)", re.I)
REDACTED = "[REDACTED]"


def is_secret_key(key: Any) -> bool:
    k = str(key).lower()
    return k in SECRET_KEYS or k.endswith("_password") or k.endswith("_token") or k.endswith("_secret")


def redact(value: Any, *, extra_secrets: tuple[str, ...] = ()) -> Any:
    """Recursively replace secret-looking keys and known secret values."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if is_secret_key(k) and v not in (None, "", False) else redact(v, extra_secrets=extra_secrets))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, extra_secrets=extra_secrets) for v in value]
    if isinstance(value, str) and extra_secrets:
        for secret in extra_secrets:
            if secret and len(secret) >= 4 and secret in value:
                value = value.replace(secret, REDACTED)
    return value


def redact_url(url: str | None) -> str:
    """Strip secret-looking query parameters (e.g. verification tokens) from URLs."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    query = [(k, REDACTED if _SECRET_QUERY.search(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_matches(provided: str | None, expected: str) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def upsert_env_file(path: Path, values: dict[str, str]) -> None:
    """Set KEY=VALUE lines in a .env file, preserving other lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in remaining and not line.lstrip().startswith("#"):
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
