"""Deterministic graduation-window checks ("graduating between Dec 2027 and June 2028").

Returns a decision only when the sentence states an explicit window; soft language
("preferred", "ideally") or anything unparseable returns None so the rule falls back to
an Astra review. Permissive by design: date tokens are widened to whole months/seasons
and a tolerance is applied before declaring a mismatch.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS["sept"] = 9
_SEASONS = {"spring": (3, 6), "summer": (5, 8), "fall": (9, 12), "autumn": (9, 12), "winter": (12, 3)}
_SOFT = re.compile(r"\b(prefer\w*|ideally|desired|nice to have|plus)\b", re.I)
_GRAD = re.compile(r"(\bgraduat\w*|\bclass of|\bdegree completion|\bcomplet\w* (?:your|their|the|a) (?:degree|program))", re.I)
_MANDATORY = re.compile(r"\b(must|required|requirement|only|acceptable|eligib\w*|need to|minimum|no later|no earlier)\b", re.I)
_TOKEN = re.compile(
    r"(?:(?P<season>spring|summer|fall|autumn|winter)(?:\s*/\s*(?P<season2>spring|summer|fall|autumn|winter))?|"
    r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?)?\s*(?:of\s+|,\s*)?(?:the\s+)?(?P<year>20\d\d)\b",
    re.I,
)
TOLERANCE = dt.timedelta(days=45)


@dataclass
class Window:
    lo: dt.date | None
    hi: dt.date | None
    text: str

    def contains(self, lo: dt.date, hi: dt.date) -> bool:
        w_lo = (self.lo - TOLERANCE) if self.lo else dt.date.min
        w_hi = (self.hi + TOLERANCE) if self.hi else dt.date.max
        return lo <= w_hi and hi >= w_lo


def _month_end(year: int, month: int) -> dt.date:
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def _interval(m: re.Match[str]) -> tuple[dt.date, dt.date]:
    year = int(m.group("year"))
    if m.group("month"):
        key = m.group("month").lower().rstrip(".")
        month = _MONTHS.get(key) or _MONTHS[key[:3]]
        return dt.date(year, month, 1), _month_end(year, month)
    seasons = [s.lower() for s in (m.group("season"), m.group("season2")) if s]
    if seasons:
        los, his = [], []
        for s in seasons:
            a, b = _SEASONS[s]
            if s == "winter":  # "Winter 2027" spans Dec 2026 – Mar 2027 or Dec 2027; stay permissive
                los.append(dt.date(year - 1, 12, 1))
                his.append(_month_end(year, 12))
            else:
                los.append(dt.date(year, a, 1))
                his.append(_month_end(year, b))
        return min(los), max(his)
    return dt.date(year, 1, 1), dt.date(year, 12, 31)


def parse_window(sentence: str) -> Window | None:
    if _SOFT.search(sentence):
        return None
    g = _GRAD.search(sentence)
    if not g:
        return None
    # Keep a few words before the keyword: negations live there ("not graduating before ...").
    prefix_start = max(0, sentence.rfind(" ", 0, max(0, g.start() - 12)) + 1)
    clause = sentence[prefix_start:]
    clause = re.split(r"(?<=[a-z0-9])\.(?:\s|$)|;|\band (?:have|be|are|is)\b", clause, maxsplit=1)[0]
    tokens = list(_TOKEN.finditer(clause))
    if not tokens:
        return None
    intervals = [_interval(t) for t in tokens]
    low = clause.lower()
    lo, hi = min(i[0] for i in intervals), max(i[1] for i in intervals)
    lower_re = r"or later|no earlier th[ae]n|at the earliest|not (?:be )?(?:graduating |graduate )?before|or after|\bafter\b|onward"
    lower_bound = re.search(lower_re, low)
    upper_bound = re.search(r"or earlier|or sooner|no later th[ae]n|at the latest|\bby\b|\bbefore\b|prior to|\buntil\b",
                            re.sub(lower_re, " ", low))
    if lower_bound and not upper_bound:
        return Window(lo, None, clause.strip())
    if upper_bound and not lower_bound:
        return Window(None, hi, clause.strip())
    if lower_bound and upper_bound:
        return None  # mixed phrasing: let Astra read it
    return Window(lo, hi, clause.strip())


def check(text: str, candidate: dt.date | None) -> tuple[bool | None, list[str]]:
    """(eligible?, explanations) over every graduation sentence in ``text``."""
    if candidate is None:
        return None, ["candidate graduation date unknown"]
    c_lo, c_hi = candidate, candidate if candidate.day != 1 else _month_end(candidate.year, candidate.month)
    decisions: list[tuple[bool, bool]] = []  # (within window, mandatory wording)
    notes: list[str] = []
    for sentence in re.split(r"\n|(?<=[.!?])\s+(?=[A-Z•])", text):
        if not _GRAD.search(sentence) or not re.search(r"20\d\d", sentence):
            continue
        window = parse_window(sentence)
        if window is None:
            notes.append(f"unparsed: {sentence.strip()[:160]}")
            continue
        ok = window.contains(c_lo, c_hi)
        decisions.append((ok, bool(_MANDATORY.search(sentence))))
        bounds = f"{window.lo or '…'} – {window.hi or '…'}"
        notes.append(f"{'within' if ok else 'OUTSIDE'} window {bounds}: {window.text[:160]}")
    if not decisions:
        return None, notes
    within = [d for d in decisions if d[0]]
    outside = [d for d in decisions if not d[0]]
    if within and not outside:
        return True, notes
    if outside and not within and any(mandatory for _, mandatory in outside):
        return False, notes  # every stated window excludes the candidate, in mandatory wording
    # Mixed signals or non-mandatory wording: let Astra read it (permissive by default).
    return None, notes
