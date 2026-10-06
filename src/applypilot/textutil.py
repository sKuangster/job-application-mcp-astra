"""Deterministic text normalization and fuzzy matching (no model calls)."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

_CONTRACTIONS = {
    "you're": "you are", "you've": "you have", "don't": "do not", "won't": "will not",
    "can't": "cannot", "isn't": "is not", "aren't": "are not", "i'm": "i am",
    "what's": "what is", "doesn't": "does not", "haven't": "have not", "wouldn't": "would not",
}
# Words that carry no meaning for question identity.
# Words that change meaning ("currently", "will", "us", "this", "now", "future") are deliberately kept.
_FILLER = {
    "please", "kindly", "the", "a", "an", "your", "you", "our", "we", "to", "of", "in",
    "for", "and", "or", "is", "are", "be", "do", "does", "that", "any", "if", "with",
    "on", "at", "by", "as", "it", "its", "would", "can", "could", "should", "me",
    "required", "optional", "answer", "question", "select", "one", "below", "following",
}
_SYNONYMS = {
    "u.s.": "us", "u.s": "us", "usa": "us", "america": "us", "states": "us",
    "organisation": "organization", "favourite": "favorite", "cv": "resume",
    "résumé": "resume", "visa": "sponsorship", "h1b": "sponsorship", "h-1b": "sponsorship",
    "sponsor": "sponsorship", "sponsored": "sponsorship", "authorised": "authorized",
    "legally": "", "eligible": "authorized",
}
# Only LEGAL-FORM suffixes are dropped: "Fixture Labs" and "Fixture Corp" must stay different companies.
_COMPANY_SUFFIXES = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|ag|s\.?a|pbc|lp|llp)\b\.?$",
    re.I,
)


def nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", "" if value is None else str(value))


def clean(value: Any) -> str:
    """Collapse whitespace and strip required-markers."""
    text = re.sub(r"\s+", " ", nfkc(value)).strip()
    text = re.sub(r"\s*(\*|\(required\)|\(optional\))\s*$", "", text, flags=re.I)
    return text.strip()


def words(value: Any) -> list[str]:
    text = nfkc(value).casefold()
    for short, long in _CONTRACTIONS.items():
        text = text.replace(short, long)
    return re.findall(r"[a-z0-9]+(?:[.'-][a-z0-9]+)*", text)


def normalize_text(value: Any) -> str:
    return " ".join(re.findall(r"\w+", nfkc(value).casefold()))


def normalize_question(value: Any) -> str:
    """Canonical form used as the identity of a question across applications."""
    tokens = []
    for word in words(value):
        word = _SYNONYMS.get(word, word)
        for part in word.split():
            if part and part not in _FILLER:
                tokens.append(part.strip(".'-"))
    return " ".join(t for t in tokens if t)


def token_set(value: Any) -> set[str]:
    return set(normalize_question(value).split())


def similarity(a: Any, b: Any) -> float:
    """Blend of token-set Jaccard and sequence similarity, in [0, 1]."""
    na, nb = normalize_question(a), normalize_question(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    sa, sb = set(na.split()), set(nb.split())
    jaccard = len(sa & sb) / len(sa | sb)
    seq = SequenceMatcher(None, na, nb).ratio()
    return round(0.55 * jaccard + 0.45 * seq, 4)


def company_norm(value: Any) -> str:
    text = " ".join(re.findall(r"[a-z0-9.]+", nfkc(value).casefold().replace("&", " and ").replace(",", " ")))
    for _ in range(2):  # "Acme Holdings, Inc." → "acme holdings"
        text = _COMPANY_SUFFIXES.sub("", text).strip()
    return " ".join(re.findall(r"[a-z0-9]+", text))


_TITLE_NOISE = re.compile(
    r"\b(intern(ship)?s?|co-?op|summer|fall|winter|spring|20\d\d|\d{4}|student|program|"
    r"undergrad(uate)?|\(.*?\)|-|–|—)\b",
    re.I,
)


def title_norm(value: Any) -> str:
    """Role identity for duplicate detection: drop season/year/intern noise."""
    text = nfkc(value).casefold()
    text = re.sub(r"\(.*?\)", " ", text)
    text = _TITLE_NOISE.sub(" ", text)
    text = text.replace("swe", "software engineer").replace("sde", "software engineer")
    text = re.sub(r"\bengineering\b", "engineer", text)
    return " ".join(re.findall(r"[a-z0-9+#]+", text))


def stable_hash(value: Any, length: int = 16) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def truncate(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def word_count(value: str) -> int:
    return len(re.findall(r"\S+", value or ""))


def parse_limits(*texts: Any) -> dict[str, int]:
    """Extract stated word/character limits from label/description text."""
    joined = " ".join(nfkc(t) for t in texts if t)
    out: dict[str, int] = {}
    m = re.search(r"(\d[\d,]*)\s*(?:words?|word limit)", joined, re.I) or re.search(
        r"(?:max(?:imum)?|up to|no more than|limit(?:ed)? to|under)\s*(\d[\d,]*)\s*words?", joined, re.I
    )
    if m:
        out["max_words"] = int(m.group(1).replace(",", ""))
    m = re.search(r"(\d[\d,]*)\s*(?:characters?|chars?)", joined, re.I)
    if m:
        out["max_chars"] = int(m.group(1).replace(",", ""))
    return out


def html_to_text(html: str, limit: int | None = None) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    blocks = []
    for el in soup.find_all(["p", "li", "h1", "h2", "h3", "h4", "div"]):
        if el.find(["p", "li", "div"]):
            continue
        text = clean(el.get_text(" "))
        if text:
            blocks.append(("• " if el.name == "li" else "") + text)
    text = "\n".join(blocks) if blocks else clean(soup.get_text(" "))
    return truncate(text, limit) if limit else text
