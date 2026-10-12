"""Typed views of the browser snapshot (docs/PROTOCOL.md §3)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Option(_M):
    label: str = ""
    value: str | None = None
    selected: bool = False


class PageField(_M):
    id: str
    key: str
    frame: int = 0
    type: str = "text"
    label: str = ""
    name: str = ""
    description: str = ""
    placeholder: str = ""
    section: str = ""
    required: bool = False
    disabled: bool = False
    value: Any = None
    options: list[Option] | None = None
    options_dynamic: bool = False
    multiple: bool = False
    max_length: int | None = None
    min_length: int | None = None
    autocomplete: str | None = None
    accept: str | None = None
    files: list[str] = Field(default_factory=list)
    error: str | None = None
    hint: str | None = None

    @property
    def question(self) -> str:
        return self.label or self.description or self.placeholder or self.name

    def option_labels(self) -> list[str]:
        return [o.label for o in self.options or [] if o.label]

    def is_empty(self) -> bool:
        v = self.value
        if self.type == "file":
            return not self.files
        if self.type == "checkbox":
            return v is not True
        if v is None or v == "" or v == [] or v is False:
            return True
        if isinstance(v, str) and self.options:
            low = v.strip().casefold()
            return low in {"select...", "select", "please select", "choose", "--", "select one", "choose one", ""}
        return False


class Button(_M):
    id: str
    text: str = ""
    kind: str = "other"
    disabled: bool = False
    frame: int = 0


class PageError(_M):
    text: str = ""
    field_id: str | None = None


class Captcha(_M):
    present: bool = False
    library_detected: bool = False
    widget_present: bool = False
    challenge_blocking: bool = False
    kind: str | None = None
    interactive: bool = False
    detail: str | None = None
    evidence: list[str] = Field(default_factory=list)


class Confirmation(_M):
    detected: bool = False
    text: str | None = None


class Step(_M):
    current: int | None = None
    total: int | None = None
    label: str | None = None


class PageSnapshot(_M):
    url: str = ""
    title: str = ""
    frames: int = 1
    ats: str | None = None
    page_kind: str = "unknown"
    heading: str = ""
    step: Step | None = None
    fields: list[PageField] = Field(default_factory=list)
    buttons: list[Button] = Field(default_factory=list)
    errors: list[PageError] = Field(default_factory=list)
    captcha: Captcha = Captcha()
    confirmation: Confirmation = Confirmation()
    signals: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None
    fingerprint: str = ""

    def buttons_of(self, *kinds: str) -> list[Button]:
        return [b for b in self.buttons if b.kind in kinds and not b.disabled]

    def has_filled_fields(self) -> bool:
        return any(not f.is_empty() for f in self.fields if f.type not in ("file", "password") and not f.disabled)

    def final_buttons(self) -> list[Button]:
        """Buttons that would send the application. An "Apply"-style button on a filled form is final too:
        it must go through the submission gate, never be clicked as navigation."""
        submits = self.buttons_of("submit")
        if submits:
            return submits
        return self.buttons_of("apply") if self.has_filled_fields() else []

    def field(self, key_or_id: str) -> PageField | None:
        for f in self.fields:
            if f.key == key_or_id or f.id == key_or_id:
                return f
        return None

    def compact(self) -> dict[str, Any]:
        """Token-efficient summary for MCP responses and audit events."""
        return {
            "url": self.url, "title": self.title, "ats": self.ats, "page_kind": self.page_kind,
            "heading": self.heading, "step": self.step.model_dump() if self.step else None,
            "fields": [
                {"key": f.key, "type": f.type, "label": f.label[:160], "required": f.required,
                 "filled": not f.is_empty(), "options": f.option_labels()[:25] or None, "error": f.error}
                for f in self.fields
            ],
            "buttons": [{"id": b.id, "text": b.text[:60], "kind": b.kind} for b in self.buttons],
            "errors": [e.text[:200] for e in self.errors],
            "captcha": self.captcha.model_dump() if self.captcha.present else None,
            "confirmation": self.confirmation.text if self.confirmation.detected else None,
        }
