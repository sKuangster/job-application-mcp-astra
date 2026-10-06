"""Permissive, data-driven eligibility.

Only *structural* incompatibilities (citizenship, authorization, degree level, enrollment,
graduation window, explicit geography) can make a job INELIGIBLE, and only when the
candidate's source-of-truth fact contradicts an explicit requirement. Unknown facts and
anything ambiguous default to applying. Ambiguous-but-structural rules (``mode: review``)
are batched to Astra instead of guessed.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from applypilot.config import AppConfig, EligibilityRule, lookup, parse_fact_date
from applypilot.gradwindow import check as check_graduation
from applypilot.ingest.simplify_page import job_text
from applypilot.textutil import truncate

DEFAULT_RULES_PATH = Path(__file__).with_name("data") / "default_rules.yaml"

_DEGREE_ALIASES = {
    "bachelor": {"bachelor's", "bachelor", "bachelors", "bs", "ba", "undergraduate"},
    "master": {"master's", "master", "masters", "ms", "ma", "meng"},
    "phd": {"phd", "ph.d", "doctorate", "doctoral"},
    "mba": {"mba"},
    "associate": {"associate's", "associate", "associates"},
    "high_school": {"high school"},
}
_COUNTRY_NAMES = {
    "united states": "US", "usa": "US", "us": "US", "u.s.": "US", "canada": "CA", "united kingdom": "GB",
    "uk": "GB", "england": "GB", "germany": "DE", "france": "FR", "india": "IN", "ireland": "IE",
    "netherlands": "NL", "singapore": "SG", "australia": "AU", "japan": "JP", "china": "CN",
    "switzerland": "CH", "israel": "IL", "mexico": "MX", "poland": "PL", "spain": "ES",
}
_US_STATES = set(
    ["AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR"]
)
_CA_PROVINCES = set(["ON", "QC", "BC", "AB", "MB", "SK", "NS", "NB", "NL", "PE", "YT", "NT", "NU"])


@dataclass
class RuleOutcome:
    rule_id: str
    mode: str
    applied: bool
    passed: bool | None  # None = candidate fact unknown
    candidate_path: str
    candidate_value: Any
    expected: Any
    evidence: str
    explanation: str


@dataclass
class EligibilityResult:
    decision: str  # eligible | ineligible | review | skip_preference
    reasons: list[str] = field(default_factory=list)
    outcomes: list[RuleOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    review: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision, "reasons": self.reasons, "warnings": self.warnings,
            "outcomes": [asdict(o) for o in self.outcomes if o.applied], "review": self.review,
        }


def load_rules(cfg: AppConfig) -> list[EligibilityRule]:
    rules: dict[str, EligibilityRule] = {}
    if cfg.settings.eligibility.use_default_rules and DEFAULT_RULES_PATH.exists():
        for raw in yaml.safe_load(DEFAULT_RULES_PATH.read_text(encoding="utf-8")) or []:
            rule = EligibilityRule.model_validate(raw)
            rules[rule.id] = rule
    for rule in cfg.preferences.eligibility_rules:
        rules[rule.id] = rule
    out = [r for r in rules.values() if r.enabled]
    if not cfg.settings.eligibility.use_listing_metadata:
        out = [r for r in out if not r.metadata_key]
    if not cfg.settings.eligibility.review_with_astra:
        out = [r for r in out if r.mode != "review"]
    return out


def job_countries(job: dict[str, Any]) -> set[str]:
    countries: set[str] = set()
    for loc in (job.get("details") or {}).get("locations") or []:
        name = str(loc.get("country") or "").casefold()
        if name in _COUNTRY_NAMES:
            countries.add(_COUNTRY_NAMES[name])
    for raw in job.get("locations") or []:
        text = str(raw)
        low = text.casefold()
        for name, code in _COUNTRY_NAMES.items():
            if re.search(rf"\b{re.escape(name)}\b", low) and len(name) > 2:
                countries.add(code)
        m = re.search(r",\s*([A-Z]{2})\b", text)
        if m:
            if m.group(1) in _US_STATES:
                countries.add("US")
            elif m.group(1) in _CA_PROVINCES:
                countries.add("CA")
        if re.search(r"\b(UK|USA|US)\b", text):
            countries.add("GB" if "UK" in text else "US")
    return countries


def derived_facts(cfg: AppConfig) -> dict[str, Any]:
    us = lookup(cfg.candidate, "work_authorization.US") or {}
    now, future = us.get("requires_sponsorship_now"), us.get("requires_sponsorship_future")
    if now is True or future is True:
        requires = True
    elif now is False and future is False:
        requires = False
    else:
        requires = None
    return {"requires_us_sponsorship": requires}


def candidate_value(cfg: AppConfig, path: str) -> Any:
    if path.startswith("derived."):
        return derived_facts(cfg).get(path.removeprefix("derived."))
    return cfg.fact(path)


def _degree_canon(value: Any) -> str | None:
    text = str(value or "").casefold().strip()
    for canon, aliases in _DEGREE_ALIASES.items():
        if text == canon or text in aliases:
            return canon
    return None


def _as_date(value: Any) -> dt.date | None:
    try:
        return parse_fact_date(value)
    except ValueError:
        return None


def compare(actual: Any, operator: str, expected: Any, metadata: Any = None) -> bool:
    if operator == "truthy":
        return bool(actual)
    if operator == "equals":
        if isinstance(expected, str) and isinstance(actual, str):
            return actual.casefold() == expected.casefold()
        return bool(actual == expected)
    if operator == "not_equals":
        return not compare(actual, "equals", expected)
    if operator == "in":
        return any(compare(actual, "equals", e) for e in (expected or []))
    if operator == "contains":
        if isinstance(actual, (list, tuple, set)):
            return any(compare(a, "equals", expected) for a in actual)
        return str(expected).casefold() in str(actual).casefold()
    if operator in {"before", "after", "between"}:
        a = _as_date(actual)
        if a is None:
            return False
        if operator == "before":
            return a <= (_as_date(expected) or dt.date.max)
        if operator == "after":
            return a >= (_as_date(expected) or dt.date.min)
        lo, hi = (expected or [None, None])[:2]
        return (_as_date(lo) or dt.date.min) <= a <= (_as_date(hi) or dt.date.max)
    if operator == "candidate_in_metadata":
        canon = _degree_canon(actual)
        listed = {_degree_canon(x) for x in (metadata or [])}
        listed.discard(None)
        if not listed:
            return True
        if canon is None:
            return str(actual).casefold() in {str(x).casefold() for x in metadata or []}
        return canon in listed
    raise ValueError(f"unknown operator {operator}")


def _evidence(text: str, match: re.Match[str]) -> str:
    start = text.rfind("\n", 0, match.start()) + 1
    end = text.find("\n", match.end())
    return truncate(text[start:end if end != -1 else None].strip(), 400)


def evaluate(job: dict[str, Any], cfg: AppConfig, *, rules: list[EligibilityRule] | None = None) -> EligibilityResult:
    rules = rules if rules is not None else load_rules(cfg)
    text = job_text(job)
    countries = job_countries(job)
    result = EligibilityResult(decision="eligible")

    # Preference filters (not eligibility, but recorded the same way).
    prefs = cfg.preferences.application
    company_low, title_low = str(job.get("company", "")).casefold(), str(job.get("title", "")).casefold()
    if any(c.casefold() == company_low for c in prefs.excluded_companies):
        result.decision = "skip_preference"
        result.reasons.append("Company is in preferences.application.excluded_companies")
        return result
    if prefs.excluded_roles and any(re.search(p, title_low, re.I) for p in prefs.excluded_roles):
        result.decision = "skip_preference"
        result.reasons.append("Title matches preferences.application.excluded_roles")
        return result
    if prefs.desired_roles and not any(re.search(p, title_low, re.I) for p in prefs.desired_roles):
        result.decision = "skip_preference"
        result.reasons.append("Title matches none of preferences.application.desired_roles")
        return result

    for rule in rules:
        if rule.country and countries and rule.country not in countries:
            continue
        if rule.country and not countries and rule.country != "US":
            continue  # unknown location: only US-scoped rules (dominant case) still apply
        evidence = ""
        triggered = True
        meta_value = None
        if rule.metadata_key:
            meta_value = job.get(rule.metadata_key)
            if rule.metadata_values:
                triggered = str(meta_value) in rule.metadata_values
                evidence = f"listing {rule.metadata_key} = {meta_value}"
            else:
                triggered = bool(meta_value)
                evidence = f"listing {rule.metadata_key} = {meta_value}"
        if triggered and rule.patterns:
            match = None
            for pattern in rule.patterns:
                match = re.search(pattern, text)
                if match:
                    break
            triggered = match is not None
            if match:
                evidence = (evidence + " | " if evidence else "") + _evidence(text, match)
        if not triggered:
            continue
        actual = candidate_value(cfg, rule.candidate_path)
        outcome = RuleOutcome(rule.id, rule.mode, True, None, rule.candidate_path, actual, rule.expected,
                              evidence, rule.explanation)
        result.outcomes.append(outcome)
        if actual is None:
            result.warnings.append(f"{rule.id}: candidate fact {rule.candidate_path} is unknown ({rule.explanation})")
            if cfg.settings.eligibility.unknown_fact_policy == "needs_user" and rule.mode == "hard":
                result.decision = "needs_user"
                result.reasons.append(f"Set {rule.candidate_path} so '{rule.id}' can be evaluated")
            continue
        if rule.check == "graduation_window":
            decided, notes = check_graduation(text, _as_date(actual))
            outcome.evidence = (evidence + " | " + " | ".join(notes))[:800]
            if decided is True:
                outcome.passed = True
                continue
            if decided is False:
                outcome.passed = False
                result.decision = "ineligible"
                result.reasons.append(f"{rule.id}: candidate graduation {actual} is outside the stated window ({'; '.join(notes)[:300]})")
                continue
        if rule.mode == "review":
            outcome.passed = None
            result.review.append({
                "rule_id": rule.id, "evidence": evidence, "candidate_path": rule.candidate_path,
                "candidate_value": actual, "explanation": rule.explanation,
            })
            continue
        outcome.passed = compare(actual, rule.operator, rule.expected, meta_value)
        if not outcome.passed:
            result.decision = "ineligible"
            result.reasons.append(f"{rule.id}: {rule.explanation} (evidence: {evidence}; candidate "
                                  f"{rule.candidate_path} = {actual!r})")
    if result.decision == "eligible" and result.review:
        result.decision = "review"
    if result.decision == "eligible" and not result.reasons:
        result.reasons.append("No structural restriction contradicts the candidate's facts")
    return result
