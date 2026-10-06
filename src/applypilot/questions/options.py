"""Deterministic mapping of a resolved value onto a field's options and input format."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from applypilot.models import PageField
from applypilot.textutil import normalize_text, similarity

DECLINE = "__DECLINE__"  # sentinel: choose the "prefer not to answer" option

_YES = re.compile(r"^\s*(yes|y|true|i am\b(?! not)|i do\b(?! not)|i have\b(?! not)|i will\b(?! not)|i can\b(?! ?not)|"
                  r"i agree|i acknowledge|i accept|i consent|i understand|agree|accept|authorized|confirm)", re.I)
_NO = re.compile(r"^\s*(no|n|false|i am not|i'm not|i do not|i don't|i have not|i haven't|i will not|i won't|"
                 r"i cannot|i can't|not\b|none|disagree|decline to accept)", re.I)
_DECLINE = re.compile(r"(decline|don'?t wish|do not wish|prefer not|not to (answer|disclose|say|self[- ]identify|specify)|"
                      r"choose not|rather not|not (wish|want) to|no answer|undisclosed|i don'?t want)", re.I)
_PLACEHOLDER = re.compile(r"^\s*(select|choose|please select|--|—|pick)\b.*|^\s*$", re.I)

SYNONYMS: dict[str, list[str]] = {
    "united states": ["united states of america", "usa", "us", "u.s.", "u.s.a.", "america"],
    "united kingdom": ["uk", "great britain", "england", "gb"],
    "canada": ["ca", "can"],
    "bachelor": ["bachelor's", "bachelors", "bachelor's degree", "bachelor of science", "bachelor of arts", "bs", "ba",
                 "b.s.", "b.a.", "undergraduate", "4 year degree", "four-year degree"],
    "master": ["master's", "masters", "master's degree", "ms", "m.s.", "ma", "meng", "graduate"],
    "phd": ["ph.d.", "ph.d", "doctorate", "doctoral", "doctor of philosophy"],
    "associate": ["associate's", "associate degree", "associates"],
    "male": ["man", "m"], "female": ["woman", "f"],
    "non-binary": ["nonbinary", "non binary", "genderqueer"],
    "mobile": ["cell", "cell phone", "mobile phone", "personal mobile"],
    "not a veteran": ["i am not a protected veteran", "no, i am not a veteran", "i am not a veteran", "not a protected veteran"],
    "no disability": ["no, i do not have a disability", "i do not have a disability", "no, i don't have a disability"],
}
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia", "PR": "Puerto Rico",
}
for _code, _name in US_STATES.items():
    SYNONYMS.setdefault(_name.casefold(), []).append(_code.casefold())


@dataclass
class Mapped:
    ok: bool
    value: Any = None
    reason: str = ""
    ambiguous: list[str] | None = None


def _variants(text: str) -> set[str]:
    base = normalize_text(text)
    out = {base}
    for canon, alts in SYNONYMS.items():
        group = {normalize_text(canon), *(normalize_text(a) for a in alts)}
        if base in group:
            out |= group
    return out


def real_options(field: PageField) -> list[str]:
    return [label for label in field.option_labels() if not _PLACEHOLDER.match(label)]


def match_option(value: Any, options: list[str], *, threshold: float = 0.82) -> Mapped:
    """Choose exactly one option for ``value``; ambiguity is reported, never guessed."""
    if not options:
        return Mapped(False, reason="no options")
    if value is DECLINE or value == DECLINE:
        hits = [o for o in options if _DECLINE.search(o)]
        if len(hits) >= 1:
            return Mapped(True, hits[0])
        return Mapped(False, reason="no decline option", ambiguous=options)
    if isinstance(value, bool):
        yes = [o for o in options if _YES.match(o) and not _NO.match(o)]
        no = [o for o in options if _NO.match(o)]
        pool = yes if value else no
        if len(pool) == 1:
            return Mapped(True, pool[0])
        if len(pool) > 1:
            # e.g. "Yes" vs "Yes, H-1B": prefer the bare answer if present
            bare = [o for o in pool if normalize_text(o) in {"yes", "no", "true", "false"}]
            if len(bare) == 1:
                return Mapped(True, bare[0])
            return Mapped(False, reason="several options match", ambiguous=pool)
        return Mapped(False, reason=f"no {'yes' if value else 'no'}-like option", ambiguous=options)
    text = str(value).strip()
    if not text:
        return Mapped(False, reason="empty value")
    norm_opts = {o: normalize_text(o) for o in options}
    # 1. exact
    exact = [o for o, n in norm_opts.items() if n == normalize_text(text)]
    if len(exact) == 1:
        return Mapped(True, exact[0])
    # 2. synonyms
    variants = _variants(text)
    syn = [o for o, n in norm_opts.items() if n in variants]
    if len(syn) == 1:
        return Mapped(True, syn[0])
    # 3. containment either way (unique)
    nt = normalize_text(text)
    contain = [o for o, n in norm_opts.items() if n and (re.search(rf"\b{re.escape(nt)}\b", n) or re.search(rf"\b{re.escape(n)}\b", nt))]
    if len(contain) == 1:
        return Mapped(True, contain[0])
    # 4. fuzzy with margin
    scored = sorted(((max(similarity(v, n) for v in variants), o) for o, n in norm_opts.items()), reverse=True)
    if scored and scored[0][0] >= threshold and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
        return Mapped(True, scored[0][1])
    candidates = contain or syn or [o for s, o in scored[:5] if s >= 0.5]
    return Mapped(False, reason="no confident option match", ambiguous=candidates or options)


def match_any(values: list[Any], options: list[str], *, threshold: float = 0.82) -> Mapped:
    last = Mapped(False, reason="no candidates")
    for v in values:
        last = match_option(v, options, threshold=threshold)
        if last.ok:
            return last
    return last


def format_date_for(field: PageField, value: dt.date, *, granularity: str = "day") -> str:
    hint = f"{field.placeholder} {field.label} {field.description}".lower()
    if field.type == "date":
        return value.isoformat()
    if field.type == "month":
        return value.strftime("%Y-%m")
    if "dd/mm/yyyy" in hint:
        return value.strftime("%d/%m/%Y")
    if "yyyy-mm-dd" in hint:
        return value.isoformat()
    if re.search(r"(?<![d/])mm ?/ ?yyyy", hint) or granularity == "month" and "dd" not in hint:
        return value.strftime("%m/%Y")
    if hint.strip() == "yyyy" or "year" in field.label.lower() and "month" not in field.label.lower():
        return str(value.year)
    return value.strftime("%m/%d/%Y")


def to_field_value(field: PageField, candidates: list[Any], *, threshold: float = 0.82) -> Mapped:
    """Convert prioritized candidate values into what ``fill`` expects for this field."""
    candidates = [c for c in candidates if c is not None and c != ""]
    if not candidates:
        return Mapped(False, reason="no value")
    ftype = field.type
    if ftype in {"select", "radio", "combobox"}:
        options = real_options(field)
        if not options:
            if ftype == "combobox" or field.options_dynamic:
                first = candidates[0]
                if isinstance(first, bool):
                    first = "Yes" if first else "No"
                return Mapped(True, first if not isinstance(first, dt.date) else first.isoformat())
            return Mapped(False, reason="no options available")
        expanded: list[Any] = []
        for c in candidates:
            if isinstance(c, dt.date):
                expanded += [c.strftime("%B %Y"), str(c.year), c.strftime("%B"), c.strftime("%m")]
            else:
                expanded.append(c)
        return match_any(expanded, options, threshold=threshold)
    if ftype == "checkbox":
        first = candidates[0]
        if isinstance(first, bool):
            return Mapped(True, first)
        if first is DECLINE:
            return Mapped(False, reason="decline on checkbox")
        return Mapped(False, reason="checkbox needs a boolean")
    if ftype == "checkbox_group":
        options = real_options(field)
        wanted = candidates[0] if isinstance(candidates[0], list) else candidates
        picked = []
        for w in wanted:
            m = match_option(w, options, threshold=threshold)
            if not m.ok:
                return Mapped(False, reason=f"no option for {w!r}", ambiguous=options)
            picked.append(m.value)
        return Mapped(True, picked)
    first = candidates[0]
    if first is DECLINE:
        return Mapped(False, reason="decline needs a choice field")
    if isinstance(first, bool):
        return Mapped(True, "Yes" if first else "No")
    if isinstance(first, dt.date):
        return Mapped(True, first.isoformat() if ftype == "date" else format_date_for(field, first))
    if isinstance(first, (int, float)) and ftype in {"number"}:
        return Mapped(True, str(first))
    text = str(first)
    if field.max_length and len(text) > field.max_length:
        return Mapped(False, reason=f"value exceeds max_length {field.max_length}")
    return Mapped(True, text)
