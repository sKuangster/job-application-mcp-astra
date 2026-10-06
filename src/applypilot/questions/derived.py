"""Facts computed from the candidate source of truth (never guessed).

Each resolver returns a prioritized list of candidate values (the option mapper picks
the first that fits the field) or raises ``MissingFact`` naming the exact config path
the user must fill in.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from applypilot.config import AppConfig, lookup, parse_fact_date
from applypilot.eligibility import job_countries
from applypilot.models import PageField
from applypilot.questions.options import US_STATES
from applypilot.textutil import company_norm


class MissingFact(Exception):
    def __init__(self, paths: list[str], message: str = ""):
        super().__init__(message or f"missing candidate fact: {', '.join(paths)}")
        self.paths = paths


class NeedsReasoning(Exception):
    """The fact is known but its application to this question needs semantic judgement."""

    def __init__(self, message: str, hints: dict[str, Any] | None = None):
        super().__init__(message)
        self.hints = hints or {}


# Named non-US countries are checked first; US needs an explicit country token (never the pronoun "us").
COUNTRY_PATTERNS = [
    ("CA", re.compile(r"\bcanad", re.I)),
    ("GB", re.compile(r"\b(united kingdom|britain|england)\b|\bUK\b")),
    ("IN", re.compile(r"\bindia\b", re.I)), ("DE", re.compile(r"\bgermany\b", re.I)),
    ("IE", re.compile(r"\bireland\b", re.I)), ("SG", re.compile(r"\bsingapore\b", re.I)),
    ("AU", re.compile(r"\baustralia\b", re.I)), ("NL", re.compile(r"\bnetherlands\b", re.I)),
    ("FR", re.compile(r"\bfrance\b", re.I)), ("IL", re.compile(r"\bisrael\b", re.I)),
    ("US", re.compile(r"\bunited states\b|\bu\.s\.(a\.)?|\bUSA?\b|\bu\.s\b", re.I)),
]
_US_STRICT = re.compile(r"\bunited states\b|\bu\.s\.|\bUSA?\b")
COUNTRY_NAMES = {"US": "United States", "CA": "Canada", "GB": "United Kingdom", "IN": "India", "DE": "Germany",
                 "IE": "Ireland", "SG": "Singapore", "AU": "Australia", "NL": "Netherlands", "FR": "France",
                 "IL": "Israel"}


def _need(cfg: AppConfig, path: str) -> Any:
    value = lookup(cfg.candidate, path)
    if value is None or value == "":
        raise MissingFact([f"candidate.{path}"])
    return value


def question_country(field: PageField, job: dict[str, Any], cfg: AppConfig) -> str:
    for text in (field.label, field.description):
        for code, pattern in COUNTRY_PATTERNS:
            if code == "US":
                if _US_STRICT.search(text or "") or re.search(r"\bunited states\b|\bu\.s\.", text or "", re.I):
                    return code
            elif pattern.search(text or ""):
                return code
    countries = job_countries(job)
    if len(countries) == 1:
        return next(iter(countries))
    pref = cfg.preferences.application.work_country
    if pref:
        return str(pref).upper()
    if not countries:
        return "US"  # Simplify US-centric listings with no parsable location
    raise NeedsReasoning("job spans several countries and the question does not name one",
                         {"job_countries": sorted(countries)})


def _auth(cfg: AppConfig, country: str, key: str) -> Any:
    return lookup(cfg.candidate, f"work_authorization.{country}.{key}")


def work_authorized(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    country = question_country(field, job, cfg)
    value = _auth(cfg, country, "authorized")
    if value is None:
        raise MissingFact([f"candidate.work_authorization.{country}.authorized"])
    return [bool(value)]


def requires_sponsorship(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    country = question_country(field, job, cfg)
    now, future = _auth(cfg, country, "requires_sponsorship_now"), _auth(cfg, country, "requires_sponsorship_future")
    options = [o.label for o in field.options or [] if o.label]
    timing = [o for o in options if re.search(r"\b(future|currently|now|later|at this time)\b", o, re.I)]
    if len(options) > 2 and timing:
        # Options distinguish "now" vs "in the future": a single boolean cannot pick safely.
        raise NeedsReasoning("sponsorship options distinguish now vs future",
                             {"requires_sponsorship_now": now, "requires_sponsorship_future": future, "country": country})
    future_re = re.compile(r"\bfuture\b|\bwill you\b|\bat any time\b|\bever\b|\bnow or\b", re.I)
    now_re = re.compile(r"\bcurrently\b|\bnow\b|\bat this time\b|\bpresently\b", re.I)
    label = field.label or ""
    if future_re.search(label) or now_re.search(label):
        asks_future = bool(future_re.search(label))
        asks_now_only = not asks_future and bool(now_re.search(label))
    else:
        desc = field.description or ""
        asks_future = bool(future_re.search(desc))
        asks_now_only = not asks_future and bool(now_re.search(desc))
    paths = [f"candidate.work_authorization.{country}.requires_sponsorship_now",
             f"candidate.work_authorization.{country}.requires_sponsorship_future"]
    if asks_now_only:
        if now is None:
            raise MissingFact(paths[:1])
        return [bool(now)]
    if now is True or future is True:
        return [True]
    if now is False and future is False:
        return [False]
    raise MissingFact(paths)


def authorized_without_sponsorship(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    auth = work_authorized(cfg, field, job)[0]
    if not auth:
        return [False]
    needs = requires_sponsorship(cfg, field, job)[0]
    return [bool(auth and not needs)]


def is_citizen(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    country = question_country(field, job, cfg)
    value = _auth(cfg, country, "citizen")
    if value is None:
        cits = lookup(cfg.candidate, "eligibility.citizenships")
        if isinstance(cits, list) and cits:
            names = {str(c).casefold() for c in cits}
            return [country.casefold() in names or COUNTRY_NAMES.get(country, "").casefold() in names]
        raise MissingFact([f"candidate.work_authorization.{country}.citizen"])
    return [bool(value)]


def citizen_or_pr(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    country = question_country(field, job, cfg)
    citizen, pr = _auth(cfg, country, "citizen"), _auth(cfg, country, "permanent_resident")
    if citizen is True or pr is True:
        return [True]
    if citizen is False and pr is False:
        return [False]
    raise MissingFact([f"candidate.work_authorization.{country}.citizen",
                       f"candidate.work_authorization.{country}.permanent_resident"])


def permanent_resident(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    country = question_country(field, job, cfg)
    value = _auth(cfg, country, "permanent_resident")
    if value is None:
        raise MissingFact([f"candidate.work_authorization.{country}.permanent_resident"])
    return [bool(value)]


def us_person(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    explicit = lookup(cfg.candidate, "eligibility.us_person")
    if explicit is not None:
        return [bool(explicit)]
    citizen, pr = _auth(cfg, "US", "citizen"), _auth(cfg, "US", "permanent_resident")
    if citizen is True or pr is True:
        return [True]
    if citizen is False and pr is False:
        raise MissingFact(["candidate.eligibility.us_person"],
                          "U.S.-person status also covers asylees/refugees; set candidate.eligibility.us_person")
    raise MissingFact(["candidate.work_authorization.US.citizen", "candidate.work_authorization.US.permanent_resident"])


def citizenships(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    cits = lookup(cfg.candidate, "eligibility.citizenships")
    if not cits:
        raise MissingFact(["candidate.eligibility.citizenships"])
    cits = [COUNTRY_NAMES.get(str(c).upper(), str(c)) for c in cits]
    if field.type in {"select", "combobox", "radio"}:
        return list(cits)
    return [", ".join(cits)]


def phone(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    """User's own format first; alternates are tried if the site rejects a format."""
    raw = str(_need(cfg, "identity.phone")).strip()
    digits = re.sub(r"\D", "", raw)
    cc = str(lookup(cfg.candidate, "identity.phone_country_code") or ("1" if len(digits) == 11 and digits.startswith("1") else "")).lstrip("+")
    national = digits[len(cc):] if cc and digits.startswith(cc) and len(digits) > 10 else digits
    hint = f"{field.placeholder} {field.description} {field.label} {field.hint or ''}".lower()
    pretty = f"({national[:3]}) {national[3:6]}-{national[6:]}" if len(national) == 10 else national
    dashed = f"{national[:3]}-{national[3:6]}-{national[6:]}" if len(national) == 10 else national
    intl = f"+{cc} {dashed}" if cc else raw
    if "workday" in hint or "national" in hint or "without country code" in hint:
        return [national, dashed, raw]
    out = [raw, national, dashed, intl, pretty]
    return list(dict.fromkeys(x for x in out if x))


def phone_country_code(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    explicit = lookup(cfg.candidate, "identity.phone_country_code")
    raw = str(_need(cfg, "identity.phone"))
    code = str(explicit) if explicit else (re.match(r"\+(\d{1,3})", raw.strip()).group(1)  # type: ignore[union-attr]
                                           if raw.strip().startswith("+") else None)
    if not code:
        raise MissingFact(["candidate.identity.phone_country_code"])
    code = code.lstrip("+")
    country = lookup(cfg.candidate, "address.country") or ""
    return [f"+{code}", f"{country} (+{code})", f"{country} +{code}", code, str(country)]


def region(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    value = str(_need(cfg, "address.region"))
    out = [value]
    up = value.strip().upper()
    if up in US_STATES:
        out.append(US_STATES[up])
    for code, name in US_STATES.items():
        if name.casefold() == value.strip().casefold():
            out.append(code)
    return out


def country(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    value = str(_need(cfg, "address.country"))
    out = [value]
    code = value.upper()
    if code in COUNTRY_NAMES:
        out.insert(0, COUNTRY_NAMES[code])
    return out


def location(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    city = _need(cfg, "address.city")
    reg = lookup(cfg.candidate, "address.region")
    ctry = lookup(cfg.candidate, "address.country")
    out = []
    if reg:
        out.append(f"{city}, {reg}")
        full = US_STATES.get(str(reg).upper())
        if full:
            out.append(f"{city}, {full}")
        if ctry:
            out.append(f"{city}, {reg}, {ctry}")
    out.append(str(city))
    return out


def gpa(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    value = _need(cfg, "education.gpa")
    text = f"{float(value):.2f}".rstrip("0").rstrip(".") if isinstance(value, (int, float)) else str(value)
    scale = lookup(cfg.candidate, "education.gpa_scale")
    out = [text]
    if scale and field.type not in {"number"}:
        out.append(f"{text}/{scale}")
    return out


def _grad(cfg: AppConfig) -> dt.date:
    value = _need(cfg, "education.graduation_date")
    parsed = parse_fact_date(value)
    assert parsed is not None
    return parsed


def graduation_date(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    return [_grad(cfg)]


def graduation_year(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    return [str(_grad(cfg).year)]


def graduation_month(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    d = _grad(cfg)
    return [d.strftime("%B"), d.strftime("%b"), d.strftime("%m"), str(d.month)]


def education_start_date(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    value = parse_fact_date(_need(cfg, "education.start_date"))
    return [value]


_LEVEL_DISPLAY = {
    "bachelor": ["Bachelor's Degree", "Bachelor's", "Bachelors", "Undergraduate", "BS", "BA"],
    "master": ["Master's Degree", "Master's", "Masters", "Graduate", "MS"],
    "phd": ["PhD", "Doctorate", "Doctoral Degree", "Ph.D."],
    "mba": ["MBA", "Master of Business Administration"],
    "associate": ["Associate's Degree", "Associate's", "Associate"],
    "high_school": ["High School", "High School Diploma"],
}


def degree_level(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    level = str(_need(cfg, "education.degree_level")).lower()
    return list(_LEVEL_DISPLAY.get(level, [level]))


def degree_name(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    degree = lookup(cfg.candidate, "education.degree")
    level = lookup(cfg.candidate, "education.degree_level")
    if not degree and not level:
        raise MissingFact(["candidate.education.degree"])
    out: list[Any] = []
    if field.type in {"select", "combobox", "radio"}:
        if level:
            out += _LEVEL_DISPLAY.get(str(level).lower(), [])
        if degree:
            out.append(degree)
    else:
        if degree:
            out.append(degree)
        if level:
            out += _LEVEL_DISPLAY.get(str(level).lower(), [])[:1]
    return out


def previously_employed(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    employers = lookup(cfg.candidate, "experience.employers")
    complete = lookup(cfg.candidate, "experience.employment_history_complete")
    target = company_norm(job.get("company"))
    names = []
    for e in employers or []:
        names.append(company_norm(e.get("company") if isinstance(e, dict) else e))
    if target and any(n and n == target for n in names):
        return [True]
    if complete is True:
        return [False]
    raise MissingFact(["candidate.experience.employment_history_complete"],
                      "Cannot confirm prior employment: list all employers in candidate.experience.employers and set "
                      "employment_history_complete: true")


def today(cfg: AppConfig, field: PageField, job: dict[str, Any]) -> list[Any]:
    """Signature/acknowledgement dates are the date of signing."""
    return [dt.date.today()]


DERIVED: dict[str, Callable[[AppConfig, PageField, dict[str, Any]], list[Any]]] = {
    "work_authorized": work_authorized, "requires_sponsorship": requires_sponsorship,
    "authorized_without_sponsorship": authorized_without_sponsorship, "is_citizen": is_citizen,
    "permanent_resident": permanent_resident, "us_person": us_person, "citizenships": citizenships,
    "phone": phone, "phone_country_code": phone_country_code, "region": region, "country": country,
    "location": location, "gpa": gpa, "graduation_date": graduation_date, "graduation_year": graduation_year,
    "graduation_month": graduation_month, "education_start_date": education_start_date,
    "degree_level": degree_level, "degree_name": degree_name, "previously_employed": previously_employed,
    "today": today, "citizen_or_pr": citizen_or_pr,
}
