"""A final submission gate independent of the worker state machine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def same_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        if isinstance(actual, bool):
            return actual is expected
        return str(actual).strip().casefold() in ({"yes", "true", "1"} if expected else {"no", "false", "0"})
    if isinstance(actual, list):
        return actual == expected
    return str(actual).strip().replace("\r\n", "\n") == str(expected).strip().replace("\r\n", "\n")


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    snapshot_id: str = ""
    submit_button: str | None = None


def validate_snapshot(snapshot: dict, *, answers: list[dict], profile_hash: str,
                      resume: dict | None, resolver=None, job: dict | None = None,
                      final: bool = True) -> ValidationResult:
    errors: list[str] = []
    if snapshot.get("captcha"):
        errors.append("CAPTCHA requires user completion")
    errors.extend(str(x) for x in snapshot.get("validation_errors", []))
    if snapshot.get("unsupported_frames"):
        errors.append("An application frame could not be inspected")
    if not snapshot.get("snapshot_id"):
        errors.append("Missing page fingerprint")
    by_id = {}
    for item in answers:
        record = item.get("answer", item)
        if isinstance(record, dict) and record.get("field_id"):
            by_id[record["field_id"]] = record
    for control in snapshot.get("fields", []):
        label = control.get("label") or control["id"]
        value = control.get("value")
        required = control.get("required", False)
        if required and (value is None or value == "" or value == [] or
                         control.get("type") == "checkbox" and value is False):
            errors.append(f"Required field is empty: {label}")
        if control.get("type") == "file":
            if required and (not resume or not resume.get("upload_verified")):
                errors.append(f"Upload was not verified: {label}")
            continue
        record = by_id.get(control["id"])
        if record and record.get("page_url") == snapshot.get("url"):
            if not same_value(value, record.get("value")):
                errors.append(f"Readback differs from recorded answer: {label}")
        if resolver and value not in (None, "", []) and control.get("type") != "password":
            resolution = resolver.resolve_field(control, job)
            if resolution.status == "resolved" and not same_value(value, resolution.value):
                errors.append(f"Source-of-truth mismatch: {label}")
            elif resolution.kind in {"FACTUAL", "EEO", "PREFERENCE"} and resolution.status == "needs_user":
                errors.append(f"No authoritative fact for populated field: {label}")
            elif required and resolution.status not in {"resolved", "optional_skip"} and not record:
                errors.append(f"Required question has no audited answer: {label}")
        max_length = control.get("max_length")
        if max_length and max_length > 0 and len(str(value or "")) > max_length:
            errors.append(f"Character limit exceeded: {label}")
        if control.get("max_words") and len(str(value or "").split()) > control["max_words"]:
            errors.append(f"Word limit exceeded: {label}")
    for item in answers:
        record = item.get("answer", item)
        if isinstance(record, dict) and record.get("profile_hash") != profile_hash:
            errors.append("Candidate configuration changed since answering; revalidate all pages")
            break
    if resume and not resume.get("upload_verified"):
        errors.append("Selected resume upload has not been verified")
    buttons = [b for b in snapshot.get("buttons", []) if b.get("kind") == "submit"]
    if final:
        if len(buttons) != 1 or snapshot.get("stage") in {"account", "login", "verification"}:
            errors.append("Page is not an unambiguous final submission stage")
        if any(b.get("kind") == "next" for b in snapshot.get("buttons", [])):
            errors.append("A next-page button is present; final stage is ambiguous")
    return ValidationResult(not errors, list(dict.fromkeys(errors)), snapshot.get("snapshot_id", ""),
                            buttons[0]["id"] if len(buttons) == 1 else None)
