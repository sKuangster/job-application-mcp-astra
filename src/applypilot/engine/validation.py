"""The submission gate (spec §20): every check must pass before the final click."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from applypilot.config import AppConfig
from applypilot.db import Database
from applypilot.models import PageField, PageSnapshot
from applypilot.questions.options import to_field_value
from applypilot.questions.resolver import ResolveContext, Resolver
from applypilot.textutil import normalize_text

FACT_METHODS = {"fact", "preference", "eeo"}


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "failures": self.failures, "warnings": self.warnings, "checks": self.checks}


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return actual is expected or normalize_text(actual) in ({"yes", "true"} if expected else {"no", "false"})
    if isinstance(expected, list):
        return sorted(normalize_text(x) for x in actual or []) == sorted(normalize_text(x) for x in expected)
    return normalize_text(actual) == normalize_text(expected)


def submission_gate(*, snap: PageSnapshot, app: dict[str, Any], job: dict[str, Any], cfg: AppConfig, db: Database,
                    resolver: Resolver, resume_name: str | None) -> GateResult:
    res = GateResult(ok=True)

    def check(name: str, passed: bool, failure: str) -> None:
        res.checks[name] = passed
        if not passed:
            res.failures.append(failure)

    # 1. No CAPTCHA, no visible validation errors.
    check("no_captcha", not (snap.captcha.present and snap.captcha.interactive), "a CAPTCHA challenge is visible")
    check("no_errors", not snap.errors and not any(f.error for f in snap.fields),
          "visible validation errors: " + "; ".join([e.text for e in snap.errors][:3] + [f"{f.label}: {f.error}" for f in snap.fields if f.error][:3]))
    # 2. Required fields populated.
    empty_required = [f for f in snap.fields if f.required and not f.disabled and f.is_empty()]
    check("required_filled", not empty_required,
          "required fields empty: " + ", ".join(f.label or f.name or f.key for f in empty_required[:8]))
    # 3. This looks like the final stage (a submit button and no next button).
    submits = snap.final_buttons()
    nexts = snap.buttons_of("next")
    final = bool(submits) and not nexts
    if snap.step and snap.step.total and snap.step.current and snap.step.current < snap.step.total:
        final = False
    check("final_stage", final, "page does not look like the final submission stage")
    # 4. Resume uploaded (when the form has a resume field anywhere in this application).
    answers = {a["field_key"]: a for a in db.answers(app["id"])}
    resume_fields = [f for f in snap.fields if f.type == "file" and (answers.get(f.key, {}).get("value") == "resume"
                                                                       or "resume" in f.label.lower() or "cv" in f.label.lower())]
    for f in resume_fields:
        if f.required or answers.get(f.key):
            check(f"resume_uploaded:{f.key}", bool(f.files),
                  f"resume not attached to '{f.label}'")
            if f.files and resume_name:
                res.checks[f"resume_matches:{f.key}"] = any(resume_name.lower() in x.lower() or x.lower() in resume_name.lower()
                                                            for x in f.files)
                if not res.checks[f"resume_matches:{f.key}"]:
                    res.warnings.append(f"attached file {f.files} differs from selected resume {resume_name}")
    # 5. Unresolved questions.
    unresolved = [a for a in answers.values() if a["status"] in ("pending", "needs_user") and a["required"]]
    check("no_unresolved", not unresolved, "unresolved required questions: " + ", ".join(a["question"][:60] for a in unresolved[:5]))
    # 6. Factual answers still consistent with the source of truth (and with what is on the page).
    ctx = ResolveContext(job=job, application_id=app["id"], resume=resume_name)
    for f in snap.fields:
        ans = answers.get(f.key)
        if not ans or ans["method"] not in FACT_METHODS:
            continue
        fresh = resolver.resolve(f, ctx)
        planned = (ans["source"] or {}).get("planned_value", ans["value"])
        if fresh.status == "resolved" and fresh.method in FACT_METHODS and not _same(planned, fresh.value):
            check(f"consistent:{f.key}", False,
                  f"answer for '{f.label}' ({ans['value']!r}) no longer matches the source of truth ({fresh.value!r})")
        page_value = f.value
        if f.type not in {"file", "password"} and ans["status"] in ("filled", "verified") and not _value_on_page(f, ans["value"]):
            check(f"on_page:{f.key}", False, f"field '{f.label}' shows {page_value!r}, expected {ans['value']!r}")
    # 7. Not previously submitted (authoritative check repeats inside reserve_submission).
    equivalents = [e for e in db.find_equivalent_applications(job, exclude_app=app["id"])
                   if e["state"] in ("SUBMITTED", "SUBMITTING", "SUBMISSION_UNCONFIRMED")]
    check("not_duplicate", not equivalents,
          "equivalent job already submitted: " + ", ".join(f"{e['company']} – {e['title']} ({e['state']})" for e in equivalents[:3]))
    existing = db.submission(app["id"])
    check("no_prior_submission", existing is None or existing["status"] == "ABORTED",
          "this application already has a submission record")
    res.ok = not res.failures
    return res


def _value_on_page(f: PageField, expected: Any) -> bool:
    if f.type == "checkbox":
        return bool(f.value) == bool(expected)
    if f.type == "checkbox_group":
        return _same(f.value or [], expected or [])
    if f.type in {"select", "radio", "combobox"}:
        if f.value is None:
            return False
        if _same(f.value, expected):
            return True
        # Some widgets display the option label while we stored the mapped label: compare via mapping.
        mapped = to_field_value(f, [expected])
        return mapped.ok and _same(f.value, mapped.value)
    if isinstance(expected, str) and isinstance(f.value, str):
        a, b = normalize_text(f.value), normalize_text(expected)
        return a == b or (len(b) > 40 and a.startswith(b[:40]))
    return True
