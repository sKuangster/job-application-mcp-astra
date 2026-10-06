"""Validated local configuration and immutable source-of-truth snapshots."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class AutomationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_concurrent_applications: int = Field(default=2, ge=1, le=16)
    max_concurrent_research_tasks: int = Field(default=2, ge=1, le=8)
    max_concurrent_model_requests: int = Field(default=1, ge=1, le=8)
    page_load_timeout: float = Field(default=45, ge=2, le=300)
    worker_lease_timeout: float = Field(default=120, ge=20, le=3600)
    retry_limit: int = Field(default=3, ge=0, le=10)
    per_domain_concurrency: int = Field(default=1, ge=1, le=4)
    auto_submit: bool = False
    poll_interval_seconds: float = Field(default=300, ge=30, le=86400)


def lookup(data: dict[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted fact path; false and zero remain meaningful facts."""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return value


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class Settings:
    root: Path
    candidate: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    profile_text: str = ""
    templates: dict[str, str] = field(default_factory=dict)
    automation: AutomationSettings = field(default_factory=AutomationSettings)
    profile_hash: str = ""

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()
        payload = json.dumps({"candidate": self.candidate, "preferences": self.preferences,
                              "profile": self.profile_text, "templates": self.templates},
                             sort_keys=True, default=str)
        self.profile_hash = hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def load(cls, root: str | Path) -> "Settings":
        root = Path(root).resolve()
        candidate = _merge(_read_yaml(root / "config" / "candidate.yaml"),
                           _read_yaml(root / "config" / "candidate.local.yaml"))
        preferences = _merge(_read_yaml(root / "config" / "preferences.yaml"),
                             _read_yaml(root / "config" / "preferences.local.yaml"))
        profile = root / "profile" / "profile.md"
        if (root / "profile" / "profile.local.md").exists():
            profile = root / "profile" / "profile.local.md"
        templates = {p.stem: p.read_text(encoding="utf-8")
                     for p in sorted((root / "templates").glob("*.md"))
                     if not p.stem.endswith(".local")}
        templates.update({p.name.removesuffix(".local.md"): p.read_text(encoding="utf-8")
                          for p in sorted((root / "templates").glob("*.local.md"))})
        automation = AutomationSettings.model_validate(preferences.get("automation", {}))
        settings = cls(root=root, candidate=candidate, preferences=preferences,
                       profile_text=profile.read_text(encoding="utf-8") if profile.exists() else "",
                       templates=templates, automation=automation)
        settings.validate_facts()
        return settings

    def fact(self, path: str) -> Any:
        source, _, field_path = path.partition(".")
        if source == "candidate":
            return lookup(self.candidate, field_path)
        if source == "preferences":
            return lookup(self.preferences, field_path)
        return lookup(self.candidate, path)

    def validate_facts(self) -> None:
        """Reject ambiguous booleans and malformed configured rules early."""
        boolean_paths = ["education.currently_enrolled", "eligibility.age_18_or_older"]
        countries = self.candidate.get("work_authorization") or {}
        if not isinstance(countries, dict):
            raise ValueError("candidate.work_authorization must map country codes to facts")
        for code, facts in countries.items():
            if facts is None:
                continue
            if not isinstance(facts, dict):
                raise ValueError(f"work_authorization.{code} must be a mapping")
            boolean_paths.extend(f"work_authorization.{code}.{name}" for name in
                                 ("authorized", "requires_sponsorship_now", "requires_sponsorship_future",
                                  "citizen", "permanent_resident"))
        for path in boolean_paths:
            value = lookup(self.candidate, path)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"candidate.{path} must be true, false, or null, not {value!r}")
        import re
        for rule in self.preferences.get("eligibility_rules") or []:
            if not isinstance(rule, dict) or not rule.get("candidate_path"):
                raise ValueError("Each eligibility rule needs candidate_path")
            for pattern in rule.get("patterns", []):
                re.compile(pattern, re.I)
            if rule.get("operator", "equals") not in {"equals", "in", "contains", "before", "after", "between"}:
                raise ValueError(f"Unsupported eligibility rule operator: {rule.get('operator')}")

    def readiness(self) -> dict[str, Any]:
        required = ["identity.first_name", "identity.last_name", "identity.email"]
        missing = [f"candidate.{path}" for path in required if lookup(self.candidate, path) in (None, "")]
        return {"ready": not missing, "missing": missing, "profile_hash": self.profile_hash,
                "auto_submit": self.automation.auto_submit}
