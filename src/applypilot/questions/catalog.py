"""Question classification against the data-driven catalog (data/question_catalog.yaml)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from applypilot.models import PageField
from applypilot.textutil import clean, company_norm

CATALOG_PATH = Path(__file__).resolve().parents[1] / "data" / "question_catalog.yaml"
QCLASSES = ("FACTUAL", "PREFERENCE", "EEO", "WRITTEN", "JOB_SPECIFIC", "CONSENT", "FILE", "UNKNOWN")


@dataclass
class CatalogEntry:
    id: str
    qclass: str
    label: list[str] = field(default_factory=list)  # raw patterns; may contain {company}
    name: list[re.Pattern[str]] = field(default_factory=list)
    exclude: list[re.Pattern[str]] = field(default_factory=list)
    autocomplete: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    spec: dict[str, Any] = field(default_factory=dict)
    answer_when_optional: bool = False

    @property
    def template(self) -> str | None:
        return self.spec.get("written")


@dataclass
class Classification:
    qclass: str
    entry: CatalogEntry | None
    matched_by: str
    company_specific: bool = False


def _compile(patterns: list[str] | None) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I) for p in patterns or []]


@lru_cache(maxsize=4096)
def _pattern(raw: str, company: str) -> re.Pattern[str]:
    """Compile a label pattern, substituting {company} with the job's company name."""
    if "{company}" in raw:
        words = company.split()
        sub = r"\W*".join(re.escape(w) for w in words) if words else r"(?!x)x"
        raw = raw.replace("{company}", f"(?:{sub})")
    return re.compile(raw, re.I)


_COMPANY_REF = re.compile(r"\b(this|our|the) (company|organi[sz]ation|firm|team|employer)\b|\bwith us\b|\bfor us\b|"
                          r"\bat us\b|\bjoin us\b|\bworks? here\b|\bhere at\b|\b(?:applied|interviewed) (?:to|with|at|here)\b|"
                          r"\bknow anyone (?:who )?(?:currently )?works?\b|\bapplied (?:to|for) (?:a |any )?(?:position|role|job)s? (?:at|with)\b", re.I)


def refers_to_company(text: str, company: str = "") -> bool:
    cn = company_norm(company)
    return bool(_COMPANY_REF.search(text) or (cn and cn in company_norm(text)))


@lru_cache(maxsize=4)
def load_catalog(path: str = str(CATALOG_PATH), extra: str | None = None) -> tuple[CatalogEntry, ...]:
    raw: list[dict[str, Any]] = []
    if extra and Path(extra).exists():
        # User catalog entries (config/question_catalog.yaml) take precedence.
        raw += yaml.safe_load(Path(extra).read_text(encoding="utf-8")) or []
    raw += yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    entries = []
    spec_keys = ("fact", "facts", "pref", "eeo", "derived", "compose", "written", "file", "ask", "const")
    for item in raw:
        if item.get("qclass") not in QCLASSES:
            raise ValueError(f"catalog entry {item.get('id')}: invalid qclass {item.get('qclass')}")
        entries.append(CatalogEntry(
            id=item["id"], qclass=item["qclass"], label=list(item.get("label") or []), name=_compile(item.get("name")),
            exclude=_compile(item.get("exclude")), autocomplete=[a.lower() for a in item.get("autocomplete") or []],
            types=item.get("types") or [], spec={k: item[k] for k in spec_keys if k in item},
            answer_when_optional=bool(item.get("answer_when_optional")),
        ))
    return tuple(entries)


def label_text(f: PageField) -> str:
    text = clean(f.label) or clean(f.placeholder) or clean(f.description)
    return text.casefold()


_JOB_SPECIFIC_HINTS = re.compile(
    r"(experience (with|in|using)|familiar(ity)? with|proficien|knowledge of|comfortable (with|working)|"
    r"able to (commute|work (on-?site|in[- ]office|hybrid))|on-?site|in[- ]office|hybrid|commut|"
    r"available (for|to work)|availability|summer 20\d\d|(\d+)[- ]weeks?|full[- ]time|part[- ]time|"
    r"which (office|location|team)|preferred (office|location|team)|interested in (the following|which)|"
    r"internship (dates|duration|term)|semester|coursework|skills?\b|languages? (do you|are you)|"
    r"rate your|years of experience (with|in|using))", re.I)


def classify(f: PageField, *, company: str = "", user_catalog: str | None = None) -> Classification:
    c = _classify(f, company=company, user_catalog=user_catalog)
    if not c.company_specific and c.qclass not in ("FILE",) and refers_to_company(label_text(f), company):
        c.company_specific = True  # answers about "this company / us / here" never transfer between companies
    return c


_SHORT_FACT_CLASSES = {"FACTUAL", "PREFERENCE", "EEO", "CONSENT"}


def _type_ok(e: CatalogEntry, f: PageField) -> bool:
    if e.types:
        return f.type in e.types
    # Short facts never answer an essay box ("State your reason for applying...").
    return not (e.qclass in _SHORT_FACT_CLASSES and f.type == "textarea")


def _classify(f: PageField, *, company: str = "", user_catalog: str | None = None) -> Classification:
    entries = load_catalog(extra=user_catalog)
    text = label_text(f)
    cnorm = company_norm(company)
    if f.type == "password":
        return Classification("FACTUAL", None, "password")
    # pass 1: autocomplete
    if f.autocomplete:
        ac = f.autocomplete.lower().split()[-1]
        for e in entries:
            if ac in e.autocomplete and _type_ok(e, f):
                return Classification(e.qclass, e, f"autocomplete:{ac}")
    # pass 2: label regexes
    if text:
        for e in entries:
            if not _type_ok(e, f):
                continue
            if any(_pattern(p, cnorm).search(text) for p in e.label) and not any(p.search(text) for p in e.exclude):
                return Classification(e.qclass, e, "label", company_specific=_company_specific(text, company, e))
    # dynamic: "Why <Company>?"
    cn = company_norm(company)
    if cn and text.startswith("why") and cn in company_norm(text):
        entry = next((e for e in entries if e.id == "why_company"), None)
        return Classification("WRITTEN", entry, "company-name", company_specific=True)
    # pass 3: name/id attribute
    name = (f.name or "").lower()
    if name and not re.fullmatch(r"(question|field|input|custom)?[_-]?\d+", name):
        for e in entries:
            if not _type_ok(e, f):
                continue
            if any(p.search(name) for p in e.name) and not any(p.search(text) for p in e.exclude):
                return Classification(e.qclass, e, f"name:{name}")
    if f.type == "file":
        return Classification("FILE", None, "type:file")
    if _JOB_SPECIFIC_HINTS.search(text) or (cn and cn in company_norm(text)):
        return Classification("JOB_SPECIFIC", None, "heuristic", company_specific=bool(cn and cn in company_norm(text)))
    if f.type == "textarea":
        return Classification("WRITTEN", None, "type:textarea", company_specific=bool(cn and cn in company_norm(text)))
    return Classification("UNKNOWN", None, "none")


def _company_specific(text: str, company: str, entry: CatalogEntry) -> bool:
    if entry.id in {"why_company", "why_role", "cover_letter_text"}:
        return True
    cn = company_norm(company)
    return bool(cn and cn in company_norm(text))
