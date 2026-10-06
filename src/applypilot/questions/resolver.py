"""Field resolution pipeline (no model calls).

Order (spec §9, §38):
  1. file fields → resume / cover letter handling
  2. explicit candidate.factual_answers and user-provided answers
  3. catalog → candidate facts / preferences / EEO config / derived facts
  4. high-confidence answer memory (never company-specific prose for another company)
  5. otherwise → a reasoning item for Astra (templates, profile, job context, research)
  6. a required factual answer that cannot be established → NEEDS_USER
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from applypilot.config import AppConfig, lookup
from applypilot.db import Database
from applypilot.models import PageField
from applypilot.questions.catalog import Classification, classify
from applypilot.questions.derived import DERIVED, MissingFact, NeedsReasoning
from applypilot.questions.options import DECLINE, Mapped, real_options, to_field_value
from applypilot.textutil import (
    company_norm,
    normalize_question,
    parse_limits,
    similarity,
    stable_hash,
    word_count,
)


@dataclass
class Resolution:
    field_key: str
    question: str
    normalized: str
    qclass: str
    catalog_id: str | None
    status: str  # resolved | reasoning | needs_user | skip | file
    value: Any = None
    method: str | None = None
    sources: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    missing: list[str] = field(default_factory=list)
    reasoning: dict[str, Any] | None = None
    note: str = ""
    company_specific: bool = False

    def audit(self, f: PageField, page_index: int) -> dict[str, Any]:
        return {
            "field_key": f.key, "page_index": page_index, "question": self.question, "normalized": self.normalized,
            "qclass": self.qclass, "catalog_id": self.catalog_id, "field_type": f.type,
            "options": real_options(f), "required": f.required, "limits": limits_for(f),
            "value": self.value, "method": self.method, "source": {**self.sources, "note": self.note} if self.note else self.sources,
            "confidence": self.confidence,
            "status": {"resolved": "planned", "reasoning": "pending", "needs_user": "needs_user", "skip": "skipped",
                       "file": "planned"}[self.status],
        }


def limits_for(f: PageField) -> dict[str, int]:
    limits = parse_limits(f.label, f.description, f.placeholder)
    if f.max_length:
        limits["max_chars"] = min(limits.get("max_chars", f.max_length), f.max_length)
    return limits


def options_signature(f: PageField) -> str:
    opts = sorted(normalize_question(o) for o in real_options(f))
    return stable_hash(opts, 12) if opts else ""


def fits_limits(value: Any, limits: dict[str, int]) -> bool:
    if not isinstance(value, str):
        return True
    if limits.get("max_chars") and len(value) > limits["max_chars"]:
        return False
    return not (limits.get("max_words") and word_count(value) > limits["max_words"])


@dataclass
class ResolveContext:
    job: dict[str, Any]
    application_id: str
    page_index: int = 0
    resume: str | None = None
    alternates: dict[str, int] = field(default_factory=dict)


class Resolver:
    def __init__(self, cfg: AppConfig, db: Database):
        self.cfg = cfg
        self.db = db
        self.user_catalog = str(cfg.paths.config_dir / "question_catalog.yaml")

    # ------------------------------------------------------------------ helpers
    def _base(self, f: PageField, c: Classification, status: str, **kw: Any) -> Resolution:
        return Resolution(field_key=f.key, question=f.question, normalized=normalize_question(f.question),
                          qclass=c.qclass, catalog_id=c.entry.id if c.entry else None, status=status,
                          company_specific=c.company_specific, **kw)

    def _map(self, f: PageField, c: Classification, candidates: list[Any], *, method: str,
             sources: dict[str, Any], ctx: ResolveContext) -> Resolution:
        alt = ctx.alternates.get(f.key, 0)
        if alt and len(candidates) > 1:
            # The site rejected the previous format: rotate to the next truthful alternative.
            alt %= len(candidates)
            candidates = candidates[alt:] + candidates[:alt]
            sources = {**sources, "alternate_index": alt}
        mapped: Mapped = to_field_value(f, candidates, threshold=self.cfg.settings.answers.catalog_option_threshold)
        if mapped.ok:
            return self._base(f, c, "resolved", value=mapped.value, method=method, sources=sources)
        if f.type in {"select", "radio", "combobox", "checkbox_group"} and real_options(f):
            # The fact is known; only the option wording is ambiguous → Astra maps it, constrained.
            return self._reasoning(f, c, ctx, hints={"known_value": [_jsonable(x) for x in candidates],
                                                     "source": sources, "instruction":
                                                     "Choose the option that states exactly this known fact. "
                                                     "If none does, return needs_user."},
                                   kind_note=f"option mapping: {mapped.reason}")
        if not f.required:
            return self._base(f, c, "skip", note=f"could not format value: {mapped.reason}", sources=sources)
        return self._base(f, c, "needs_user", sources=sources, note=f"value does not fit field: {mapped.reason}",
                          missing=[f"answer for '{f.question}'"])

    def _missing(self, f: PageField, c: Classification, paths: list[str], note: str = "") -> Resolution:
        if not f.required:
            return self._base(f, c, "skip", note=f"optional; fact unknown ({', '.join(paths)})")
        return self._base(f, c, "needs_user", missing=paths, note=note or f"Set {', '.join(paths)} in config")

    def _reasoning(self, f: PageField, c: Classification, ctx: ResolveContext, *, hints: dict[str, Any] | None = None,
                   kind_note: str = "") -> Resolution:
        job = ctx.job
        limits = limits_for(f)
        template = c.entry.template if c.entry else None
        research = c.qclass == "WRITTEN" and (template in {"why_company", "cover_letter"} or c.company_specific)
        payload = {
            "question": f.question, "description": f.description[:600], "field_type": f.type,
            "options": real_options(f), "required": f.required, "limits": limits, "qclass": c.qclass,
            "catalog_id": c.entry.id if c.entry else None, "template": template,
            "company_specific": c.company_specific, "research_recommended": research,
            "company": job.get("company"), "title": job.get("title"), "job_id": job.get("id"),
            "hints": hints or {}, "multiple": f.type == "checkbox_group",
        }
        scope = ""
        if c.qclass == "JOB_SPECIFIC" or c.company_specific:
            scope = job.get("id") or ""
        elif c.qclass == "WRITTEN":
            scope = company_norm(job.get("company"))
        dedupe = stable_hash({"q": normalize_question(f.question), "o": options_signature(f), "c": c.qclass,
                              "s": scope, "l": limits, "h": hints or {}, "p": self.cfg.profile_hash[:12]}, 24)
        payload["dedupe_key"] = dedupe
        return self._base(f, c, "reasoning", reasoning=payload, method="generated",
                          note=kind_note or "needs semantic answer", confidence=0.0)

    # ------------------------------------------------------------------ explicit answers
    def _explicit(self, f: PageField, c: Classification, ctx: ResolveContext) -> Resolution | None:
        cn = company_norm(ctx.job.get("company"))
        for item in self.cfg.candidate.get("factual_answers") or []:
            scope = item.get("company")
            if scope and scope != "*" and company_norm(scope) != cn:
                continue
            if similarity(item.get("question", ""), f.question) >= 0.9:
                res = self._map(f, c, [item.get("value")], method="fact", ctx=ctx,
                                sources={"facts": [{"path": "candidate.factual_answers", "question": item.get("question"),
                                                    "value": item.get("value")}]})
                return res
        return None

    def _user_answer(self, f: PageField, c: Classification, ctx: ResolveContext) -> Resolution | None:
        norm = normalize_question(f.question)
        cn = company_norm(ctx.job.get("company"))
        for row in self.db.user_answers():
            if row["company_norm"] and row["company_norm"] != cn:
                continue
            if row["normalized"] == norm or similarity(row["question"], f.question) >= 0.95:
                return self._map(f, c, [row["value"]], method="user", ctx=ctx,
                                 sources={"user_answer": {"question": row["question"], "saved_at": row["updated_at"]}})
        return None

    # ------------------------------------------------------------------ catalog values
    def _catalog(self, f: PageField, c: Classification, ctx: ResolveContext) -> Resolution | None:
        assert c.entry is not None
        spec = c.entry.spec
        try:
            if "fact" in spec:
                path = spec["fact"]
                value = self.cfg.fact(path)
                if value is None or value == "" or value == []:
                    raise MissingFact([path])
                return self._map(f, c, [value], method="fact", ctx=ctx, sources={"facts": [{"path": path, "value": value}]})
            if "facts" in spec:
                for path in spec["facts"]:
                    value = self.cfg.fact(path)
                    if value not in (None, "", []):
                        return self._map(f, c, [value], method="fact", ctx=ctx,
                                         sources={"facts": [{"path": path, "value": value}]})
                raise MissingFact(list(spec["facts"]))
            if "compose" in spec:
                template = spec["compose"]
                paths = re.findall(r"\{([^}]+)\}", template)
                values = {p: self.cfg.fact(p) for p in paths}
                missing = [p for p, v in values.items() if v in (None, "")]
                if missing:
                    raise MissingFact(missing)
                text = template
                for p, v in values.items():
                    text = text.replace("{" + p + "}", str(v))
                return self._map(f, c, [text], method="fact", ctx=ctx,
                                 sources={"facts": [{"path": p, "value": v} for p, v in values.items()]})
            if "derived" in spec:
                fn = DERIVED[spec["derived"]]
                derived = fn(self.cfg, f, ctx.job)
                return self._map(f, c, derived, method="fact", ctx=ctx,
                                 sources={"derived": spec["derived"], "values": [_jsonable(v) for v in derived]})
            if "pref" in spec:
                path = spec["pref"]
                value = self.cfg.fact(path)
                if value is None or value == "" or value == []:
                    defaults = self.cfg.preferences.application.defaults or {}
                    value = defaults.get(c.entry.id)
                if value is None or value == "":
                    if c.qclass == "CONSENT" and not f.required:
                        return self._base(f, c, "skip", note=f"optional consent; {path} not set")
                    raise MissingFact([path])
                method = "preference" if c.qclass != "CONSENT" else "preference"
                return self._map(f, c, [value], method=method, ctx=ctx, sources={"preferences": [{"path": path, "value": value}]})
            if "eeo" in spec:
                return self._eeo(f, c, spec["eeo"], ctx)
            if "const" in spec:
                return self._map(f, c, [spec["const"]], method="deterministic", ctx=ctx, sources={"catalog": c.entry.id})
        except MissingFact as exc:
            return self._missing(f, c, exc.paths, str(exc))
        except NeedsReasoning as exc:
            return self._reasoning(f, c, ctx, hints=exc.hints, kind_note=str(exc))
        return None

    def _eeo(self, f: PageField, c: Classification, key: str, ctx: ResolveContext) -> Resolution:
        eeo = self.cfg.preferences.eeo
        value = getattr(eeo, key, None)
        path = f"preferences.eeo.{key}"
        if isinstance(value, str) and re.search(r"decline|prefer not|don.?t wish", value, re.I):
            value = DECLINE
        if value is None or value == "":
            policy = eeo.optional_unknown
            if policy == "needs_user" and f.required:
                return self._missing(f, c, [path], f"EEO answer not configured: set {path}")
            if policy == "skip" and not f.required:
                return self._base(f, c, "skip", note=f"{path} not configured; optional EEO left blank")
            if f.type in {"select", "radio", "combobox"} and real_options(f):
                res = self._map(f, c, [DECLINE], method="eeo", ctx=ctx,
                                sources={"eeo": {"path": path, "value": None, "policy": "decline"}})
                if res.status == "resolved":
                    return res
            if not f.required:
                return self._base(f, c, "skip", note=f"{path} not configured")
            return self._missing(f, c, [path], f"Required EEO question and no decline option: set {path}")
        return self._map(f, c, [value], method="eeo", ctx=ctx, sources={"eeo": {"path": path, "value": _jsonable(value)}})

    # ------------------------------------------------------------------ memory
    def _memory(self, f: PageField, c: Classification, ctx: ResolveContext) -> Resolution | None:
        if c.qclass in {"FACTUAL", "EEO", "PREFERENCE", "CONSENT", "FILE"}:
            return None  # source of truth only; memory is never authoritative for facts
        if c.qclass == "JOB_SPECIFIC" or c.company_specific:
            same_company_only = True
        else:
            same_company_only = False
        if c.qclass == "WRITTEN" and not self.cfg.settings.answers.reuse_generic_written and not same_company_only:
            return None
        norm = normalize_question(f.question)
        cn = company_norm(ctx.job.get("company"))
        sig = options_signature(f)
        limits = limits_for(f)
        threshold = self.cfg.settings.answers.reuse_threshold
        max_age = time.time() - self.cfg.settings.answers.max_memory_age_days * 86400
        candidates = self.db.memory_by_normalized(norm)
        if not candidates:
            candidates = [m for m in self.db.memory_candidates(qclass=c.qclass, since=max_age, limit=2000)
                          if similarity(m["question"], f.question) >= threshold]
        for mem in candidates:
            if mem["profile_hash"] != self.cfg.profile_hash:
                continue
            if mem["updated_at"] < max_age:
                continue
            if mem["qclass"] != c.qclass:
                continue
            if same_company_only:
                if mem["company_norm"] != cn:
                    continue
                if c.qclass == "JOB_SPECIFIC" and mem.get("role") != ctx.job.get("title"):
                    continue
            elif mem["company_norm"] and mem["company_norm"] != cn and isinstance(mem["value"], str):
                other = mem["company_norm"]
                if other and other in company_norm(mem["value"]):
                    continue  # prose mentions another company
            if sig and mem["options_sig"] != sig:
                continue
            if not fits_limits(mem["value"], limits):
                continue
            mapped = to_field_value(f, [mem["value"]])
            if not mapped.ok:
                continue
            return self._base(f, c, "resolved", value=mapped.value, method="reused", confidence=0.95,
                              sources={"memory_id": mem["id"], "original_question": mem["question"],
                                       "original_company": mem["company_norm"], "original_method": mem["method"],
                                       "facts_used": mem.get("facts_used"), "template": mem.get("template")})
        return None

    # ------------------------------------------------------------------ entry point
    def resolve(self, f: PageField, ctx: ResolveContext) -> Resolution:
        c = classify(f, company=str(ctx.job.get("company") or ""), user_catalog=self.user_catalog)
        if f.type == "file" or c.qclass == "FILE":
            kind = (c.entry.spec.get("file") if c.entry else None) or ("resume" if re.search(r"resume|\bcv\b", f.question, re.I) else "other")
            return self._base(f, Classification("FILE", c.entry, c.matched_by), "file", value=kind, method="file")
        if f.type == "password":
            return self._base(f, c, "skip", note="password fields are handled by the account flow")
        explicit = self._explicit(f, c, ctx)
        if explicit is not None:
            return explicit
        catalog_res = None
        if c.entry is not None and c.qclass != "WRITTEN" and not c.entry.spec.get("ask"):
            catalog_res = self._catalog(f, c, ctx)
            if catalog_res is not None and catalog_res.status == "resolved":
                return catalog_res
        # Answers the user typed earlier apply only where the source of truth could not decide.
        user = self._user_answer(f, c, ctx)
        if user is not None:
            return user
        if catalog_res is not None:
            return catalog_res
        reused = self._memory(f, c, ctx)
        if reused is not None:
            return reused
        if not f.required:
            wanted = bool(c.entry and c.entry.answer_when_optional)
            if c.qclass == "WRITTEN" and c.entry and c.entry.template == "cover_letter":
                wanted = self.cfg.settings.answers.cover_letter_when_optional
            if not wanted:
                return self._base(f, c, "skip", note="optional question without a source-of-truth answer; skipped to save model calls")
        return self._reasoning(f, c, ctx)


def _jsonable(value: Any) -> Any:
    if value is DECLINE:
        return "decline_to_answer"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def facts_digest(cfg: AppConfig) -> dict[str, Any]:
    """Compact candidate facts sent once per reasoning batch (never secrets)."""
    c = cfg.candidate
    keep = {
        "name": f"{lookup(c, 'identity.first_name') or ''} {lookup(c, 'identity.last_name') or ''}".strip() or None,
        "location": {k: lookup(c, f"address.{k}") for k in ("city", "region", "country")},
        "education": lookup(c, "education") or {},
        "work_authorization": lookup(c, "work_authorization") or {},
        "eligibility": lookup(c, "eligibility") or {},
        "experience": {k: lookup(c, f"experience.{k}") for k in ("current_employer", "current_title", "total_years", "skills")},
        "links": lookup(c, "links") or {},
        "preferences": {k: v for k, v in cfg.preferences.application.model_dump().items()
                        if k in {"willing_to_relocate", "willing_to_travel", "available_start_date", "desired_locations"}},
    }
    return keep
