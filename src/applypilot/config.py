"""Configuration loading and validation.

Three layers, all data (no candidate logic is hard-coded):

* ``config/candidate.yaml``   authoritative candidate FACTS (source of truth)
* ``config/preferences.yaml`` user preferences, eligibility rules, EEO answers, resume mapping
* ``config/settings.yaml``    system tuning (concurrency, timeouts, retries, thresholds)

Each file may be overlaid by a git-ignored ``*.local.yaml`` twin so real personal data
never has to be committed. ``profile/profile.md`` (or ``profile.local.md``) holds the
long-form narrative; ``templates/*.md`` hold written-answer guidance.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from applypilot import __version__

# --------------------------------------------------------------------------------------
# Environment / paths


def project_root() -> Path:
    env = os.environ.get("APPLYPILOT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path.cwd().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir() and (candidate / "pyproject.toml").exists():
            return candidate
    # Installed package next to the source tree (editable install).
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config").is_dir():
        return pkg_root
    return here


@dataclass
class Paths:
    root: Path
    data_dir: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "applypilot.sqlite3"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def resumes_dir(self) -> Path:
        return self.root / "resumes"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def profile_dir(self) -> Path:
        return self.root / "profile"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    def ensure(self) -> None:
        for path in (self.data_dir, self.logs_dir, self.artifacts_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_env(root: Path) -> None:
    """Load .env without overriding variables already set in the process."""
    from dotenv import load_dotenv

    env_file = root / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def make_paths(root: Path | None = None) -> Paths:
    root = (root or project_root()).resolve()
    data = os.environ.get("APPLYPILOT_DATA_DIR", "data")
    data_dir = Path(data).expanduser()
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    return Paths(root=root, data_dir=data_dir.resolve())


# --------------------------------------------------------------------------------------
# System settings (config/settings.yaml)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutomationSettings(_Strict):
    max_concurrent_applications: int = Field(2, ge=1, le=16, description="Browser workers")
    max_prep_workers: int = Field(3, ge=1, le=16, description="HTTP-only preparation workers")
    max_concurrent_research_tasks: int = Field(2, ge=1, le=8)
    max_concurrent_model_requests: int = Field(2, ge=1, le=8)
    per_domain_concurrency: int = Field(1, ge=1, le=8)
    max_open_application_tabs: int = Field(4, ge=1, le=32)
    worker_lease_timeout: float = Field(180, ge=20, le=3600)
    heartbeat_interval: float = Field(15, ge=1, le=600)
    retry_limit: int = Field(3, ge=0, le=10, description="Whole-application retries")
    page_retry_limit: int = Field(3, ge=1, le=10, description="Fill/validate attempts per page")
    max_pages_per_application: int = Field(30, ge=3, le=200)
    auto_submit: bool = False
    poll_interval_seconds: float = Field(1800, ge=30, le=86400, description="New-jobs polling")
    backoff_base_seconds: float = Field(30, ge=1, le=3600)
    backoff_max_seconds: float = Field(1800, ge=10, le=86400)
    domain_failure_threshold: int = Field(3, ge=1, le=50)
    domain_cooldown_seconds: float = Field(900, ge=10, le=86400)
    idle_poll_seconds: float = Field(2, ge=0.05, le=60)
    captcha_auto_resume: bool = True
    captcha_check_interval: float = Field(20, ge=2, le=600)
    captcha_wait_timeout: float = Field(86400, ge=60, le=2592000,
                                        description="Attention timeout; CAPTCHA remains recoverable")
    stop_on_config_incomplete: bool = True


class NativeWindowBinding(_Strict):
    handle: int = Field(ge=1)
    title: str = Field(min_length=1)


class WindowsNativeSettings(_Strict):
    enabled: bool = False
    # Explicit extension window_id -> Windows HWND + exact expected caption.
    # Bindings expire with the browser window; no desktop-wide target searching.
    bindings: dict[str, NativeWindowBinding] = Field(default_factory=dict)


class BrowserSettings(_Strict):
    tab_strategy: Literal["window", "tab"] = "window"
    page_load_timeout: float = Field(45, ge=2, le=300)
    ready_quiet_ms: int = Field(700, ge=50, le=10000)
    command_timeout: float = Field(30, ge=2, le=600)
    upload_timeout: float = Field(60, ge=5, le=600)
    screenshot_on_failure: bool = True
    close_tab_on: Literal["terminal", "submitted", "never"] = "terminal"
    bridge_wait_timeout: float = Field(30, ge=1, le=3600)
    focus_new_windows: bool = False
    test_one_mode: Literal["headed", "headless"] = "headed"
    debug_action_delay_ms: int = Field(0, ge=0, le=10000)
    windows_native: WindowsNativeSettings = WindowsNativeSettings()


class OpenAISettings(_Strict):
    enabled: bool = True
    model: str = "gpt-5-mini"
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    max_output_tokens: int = Field(4000, ge=256, le=64000)
    timeout: float = Field(120, ge=5, le=600)
    input_usd_per_million: float = Field(0.25, ge=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(2.0, ge=0, allow_inf_nan=False)


class OllamaSettings(OpenAISettings):
    model: str = "qwen2.5:7b"
    base_url: str = "http://127.0.0.1:11434"
    api_key_env: str = "OLLAMA_API_KEY"
    input_usd_per_million: float = Field(0, ge=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(0, ge=0, allow_inf_nan=False)


class GeminiSettings(OpenAISettings):
    enabled: bool = False
    model: str = "gemini-2.5-flash"
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    api_key_env: str = "GEMINI_API_KEY"
    input_usd_per_million: float = Field(0.30, ge=0, allow_inf_nan=False)
    output_usd_per_million: float = Field(2.50, ge=0, allow_inf_nan=False)


class HybridSettings(_Strict):
    premium_enabled: bool = False
    daily_budget_usd: float = Field(1.0, ge=0, allow_inf_nan=False)
    application_budget_usd: float = Field(0.10, ge=0, allow_inf_nan=False)
    premium_calls_per_application: int = Field(3, ge=0, le=100)
    retries_per_tier: int = Field(2, ge=1, le=10)
    retry_seconds: float = Field(10, ge=0, le=3600)
    health_cooldown_seconds: float = Field(60, ge=1, le=3600)
    question_confidence: float = Field(0.70, ge=0, le=1)
    written_confidence: float = Field(0.80, ge=0, le=1)
    # Page/eligibility decisions have no model confidence field, so validate their shape
    # and always obtain cheap-cloud review after a local answer, when enabled.
    review_local_page_and_eligibility: bool = True


class ReasoningSettings(_Strict):
    provider: Literal["hybrid", "mcp", "openai", "none"] = "hybrid"
    batch_max_items: int = Field(12, ge=1, le=100)
    batch_collect_seconds: float = Field(2.0, ge=0, le=60)
    item_lease_seconds: float = Field(900, ge=30, le=86400)
    park_timeout_seconds: float = Field(172800, ge=60, description="Then NEEDS_USER")
    min_confidence: float = Field(0.5, ge=0, le=1)
    openai: OpenAISettings = OpenAISettings()
    ollama: OllamaSettings = OllamaSettings()
    gemini: GeminiSettings = GeminiSettings()
    hybrid: HybridSettings = HybridSettings()


class AnswerSettings(_Strict):
    reuse_threshold: float = Field(0.93, ge=0.5, le=1.0)
    catalog_option_threshold: float = Field(0.82, ge=0.5, le=1.0)
    max_memory_age_days: int = Field(365, ge=1)
    reuse_generic_written: bool = True
    cover_letter_when_optional: bool = False


class EligibilitySettings(_Strict):
    unknown_fact_policy: Literal["apply", "needs_user"] = "apply"
    use_listing_metadata: bool = True
    review_with_astra: bool = True
    use_default_rules: bool = True


class ResearchSettings(_Strict):
    enabled: bool = True
    cache_ttl_seconds: int = Field(14 * 86400, ge=60)
    max_pages: int = Field(3, ge=0, le=10)
    fetch_timeout: float = Field(15, ge=1, le=120)
    max_bytes: int = Field(2_000_000, ge=10_000)


class LoggingSettings(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    verbose_browser: bool = False
    file_max_bytes: int = Field(20_000_000, ge=100_000)
    file_backups: int = Field(5, ge=0, le=50)


class IngestionSettings(_Strict):
    listings_path: str = ".github/scripts/listings.json"
    readme_path: str = "README.md"
    terms: list[str] = Field(default_factory=lambda: ["Summer 2027"])
    categories: list[str] = Field(default_factory=list, description="Empty = all categories")
    fetch_timeout: float = Field(60, ge=5, le=600)
    simplify_fetch: bool = True


class SystemSettings(_Strict):
    automation: AutomationSettings = AutomationSettings()
    browser: BrowserSettings = BrowserSettings()
    reasoning: ReasoningSettings = ReasoningSettings()
    answers: AnswerSettings = AnswerSettings()
    eligibility: EligibilitySettings = EligibilitySettings()
    research: ResearchSettings = ResearchSettings()
    logging: LoggingSettings = LoggingSettings()
    ingestion: IngestionSettings = IngestionSettings()


# --------------------------------------------------------------------------------------
# Preferences (config/preferences.yaml) – lenient for forward compatibility but typed


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class SourcePrefs(_Lenient):
    repository: str = "https://github.com/SimplifyJobs/Summer2027-Internships"
    branch: str = "dev"
    path: str = "README.md"


class ApplicationPrefs(_Lenient):
    desired_roles: list[str] = Field(default_factory=list)
    excluded_roles: list[str] = Field(default_factory=list)
    excluded_companies: list[str] = Field(default_factory=list)
    desired_locations: list[str] = Field(default_factory=list)
    willing_to_relocate: bool | None = None
    willing_to_travel: bool | None = None
    available_start_date: str | None = None
    expected_salary: str | int | None = None
    salary_currency: str | None = None
    work_country: str | None = None
    accept_privacy_policy: bool | None = None
    certify_truthfulness: bool | None = None
    contact_source: str | None = None
    sms_opt_in: bool | None = False
    phone_device_type: str | None = "Mobile"
    referred: bool | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class EEOPrefs(_Lenient):
    gender: str | None = None
    race: str | None = None
    ethnicity: str | None = None
    hispanic_latino: bool | str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    pronouns: str | None = None
    sexual_orientation: str | None = None
    transgender: str | bool | None = None
    optional_unknown: Literal["skip", "decline", "needs_user"] = "decline"


class ResumePrefs(_Lenient):
    default: str | None = None
    mappings: dict[str, str] = Field(default_factory=dict)
    categories: dict[str, list[str]] = Field(default_factory=dict)
    filename_aliases: dict[str, list[str]] = Field(default_factory=dict)


class ResearchPrefs(_Lenient):
    cache_ttl_seconds: int | None = None
    max_pages: int | None = None
    company_sources: dict[str, list[Any]] = Field(default_factory=dict)


class AccountPrefs(_Lenient):
    create_accounts: bool = True
    email_env: str = "APPLYPILOT_ACCOUNT_EMAIL"
    password_env: str = "APPLYPILOT_ACCOUNT_PASSWORD"
    domains: dict[str, dict[str, str]] = Field(default_factory=dict)


class EligibilityRule(_Lenient):
    id: str
    patterns: list[str] = Field(default_factory=list)
    metadata_key: str | None = None
    metadata_values: list[str] = Field(default_factory=list)
    candidate_path: str
    operator: Literal[
        "equals", "not_equals", "in", "contains", "before", "after", "between", "truthy",
        "candidate_in_metadata",
    ] = "equals"
    expected: Any = None
    country: str | None = None
    mode: Literal["hard", "review"] = "hard"
    check: Literal["graduation_window"] | None = None  # deterministic checker tried before an Astra review
    explanation: str = ""
    enabled: bool = True

    @field_validator("patterns")
    @classmethod
    def _compile(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as exc:  # pragma: no cover - message path
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
        return value


class Preferences(_Lenient):
    source: SourcePrefs = SourcePrefs()
    application: ApplicationPrefs = ApplicationPrefs()
    eeo: EEOPrefs = EEOPrefs()
    resumes: ResumePrefs = ResumePrefs()
    research: ResearchPrefs = ResearchPrefs()
    accounts: AccountPrefs = AccountPrefs()
    answer_memory: dict[str, Any] = Field(default_factory=dict)
    eligibility_rules: list[EligibilityRule] = Field(default_factory=list)
    automation: dict[str, Any] = Field(default_factory=dict)  # legacy location of settings


# --------------------------------------------------------------------------------------


class ConfigError(Exception):
    """Configuration is invalid; message lists every problem with its file and path."""


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return value


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def lookup(data: Any, path: str, default: Any = None) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def _format_validation(source: str, exc: ValidationError) -> list[str]:
    problems = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        problems.append(f"{source}: {loc}: {err['msg']}")
    return problems


_BOOL_FACTS = [
    "education.currently_enrolled",
    "eligibility.age_18_or_older",
    "experience.employment_history_complete",
]
_DATE_FACTS = ["education.graduation_date", "education.start_date"]
_DEGREE_LEVELS = {"high_school", "associate", "bachelor", "master", "mba", "phd", "other"}
REQUIRED_FACTS = [
    "identity.first_name",
    "identity.last_name",
    "identity.email",
    "identity.phone",
    "education.university",
    "education.degree_level",
    "education.graduation_date",
]


def parse_fact_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            parsed = dt.datetime.strptime(text, fmt).date()
            return parsed
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {value!r}; use YYYY-MM-DD or YYYY-MM")


@dataclass
class AppConfig:
    paths: Paths
    candidate: dict[str, Any]
    preferences: Preferences
    settings: SystemSettings
    profile_text: str = ""
    templates: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    profile_hash: str = ""
    token: str = ""
    mcp_token: str = ""
    host: str = "127.0.0.1"
    port: int = 8765

    # -- facts ---------------------------------------------------------------------
    def fact(self, path: str) -> Any:
        """Read ``candidate.x.y`` / ``preferences.x.y`` / bare candidate path."""
        source, _, rest = path.partition(".")
        if source == "candidate":
            return lookup(self.candidate, rest)
        if source == "preferences":
            return lookup(self.preferences.model_dump(), rest)
        return lookup(self.candidate, path)

    @property
    def raw_preferences(self) -> dict[str, Any]:
        return self.preferences.model_dump()

    def readiness(self) -> dict[str, Any]:
        missing = [f"candidate.{p}" for p in REQUIRED_FACTS if lookup(self.candidate, p) in (None, "")]
        us = lookup(self.candidate, "work_authorization.US") or {}
        if isinstance(us, dict) and us.get("authorized") is None:
            missing.append("candidate.work_authorization.US.authorized")
        resumes = list_resume_files(self.paths.resumes_dir)
        if not resumes:
            missing.append("resumes/: no resume files (PDF/DOCX) found")
        return {
            "ready": not missing,
            "missing": missing,
            "warnings": list(self.warnings),
            "resumes": [p.name for p in resumes],
            "auto_submit": self.settings.automation.auto_submit,
            "profile_hash": self.profile_hash,
            "reasoning_provider": self.settings.reasoning.provider,
            "version": __version__,
        }


def list_resume_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".doc"} and not p.name.startswith(".")
    )


def _validate_candidate(candidate: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    paths = list(_BOOL_FACTS)
    auth = candidate.get("work_authorization") or {}
    if not isinstance(auth, dict):
        problems.append("candidate.yaml: work_authorization must map country codes to facts")
        auth = {}
    for code, facts in auth.items():
        if facts is None:
            continue
        if not isinstance(facts, dict):
            problems.append(f"candidate.yaml: work_authorization.{code} must be a mapping")
            continue
        paths += [f"work_authorization.{code}.{k}" for k in (
            "authorized", "requires_sponsorship_now", "requires_sponsorship_future", "citizen",
            "permanent_resident")]
    for path in paths:
        value = lookup(candidate, path)
        if value is not None and not isinstance(value, bool):
            problems.append(f"candidate.yaml: {path} must be true, false or null (got {value!r})")
    for path in _DATE_FACTS:
        try:
            parse_fact_date(lookup(candidate, path))
        except ValueError as exc:
            problems.append(f"candidate.yaml: {path}: {exc}")
    level = lookup(candidate, "education.degree_level")
    if level is not None and str(level).lower() not in _DEGREE_LEVELS:
        problems.append(
            f"candidate.yaml: education.degree_level must be one of {sorted(_DEGREE_LEVELS)} (got {level!r})"
        )
    email = lookup(candidate, "identity.email")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", str(email)):
        problems.append(f"candidate.yaml: identity.email is not a valid address ({email!r})")
    gpa = lookup(candidate, "education.gpa")
    if gpa is not None:
        try:
            float(gpa)
        except (TypeError, ValueError):
            problems.append(f"candidate.yaml: education.gpa must be a number (got {gpa!r})")
    for i, item in enumerate(candidate.get("factual_answers") or []):
        if not isinstance(item, dict) or not item.get("question") or "value" not in item:
            problems.append(f"candidate.yaml: factual_answers[{i}] needs 'question' and 'value'")
    return problems


def _legacy_automation(prefs: dict[str, Any]) -> dict[str, Any]:
    """Older preferences.yaml kept automation knobs there; map them onto settings."""
    legacy = prefs.get("automation") or {}
    out: dict[str, Any] = {"automation": {}, "browser": {}}
    for key, value in legacy.items():
        if key == "page_load_timeout":
            out["browser"][key] = value
        elif key in AutomationSettings.model_fields:
            out["automation"][key] = value
    return out


def load_config(root: Path | None = None, *, strict: bool = True) -> AppConfig:
    paths = make_paths(root)
    load_env(paths.root)
    paths = make_paths(paths.root)  # env may set APPLYPILOT_DATA_DIR
    cfg_dir = paths.config_dir
    problems: list[str] = []
    warnings: list[str] = []

    candidate = deep_merge(read_yaml(cfg_dir / "candidate.yaml"), read_yaml(cfg_dir / "candidate.local.yaml"))
    prefs_raw = deep_merge(read_yaml(cfg_dir / "preferences.yaml"), read_yaml(cfg_dir / "preferences.local.yaml"))
    settings_raw = deep_merge(_legacy_automation(prefs_raw), read_yaml(cfg_dir / "settings.yaml"))
    settings_raw = deep_merge(settings_raw, read_yaml(cfg_dir / "settings.local.yaml"))
    if prefs_raw.get("automation"):
        warnings.append("preferences.yaml: 'automation' is a legacy location; prefer config/settings.yaml")

    problems += _validate_candidate(candidate)
    try:
        preferences = Preferences.model_validate(prefs_raw)
    except ValidationError as exc:
        problems += _format_validation("preferences.yaml", exc)
        preferences = Preferences()
    try:
        settings = SystemSettings.model_validate(settings_raw)
    except ValidationError as exc:
        problems += _format_validation("settings.yaml", exc)
        settings = SystemSettings()
    # Preference-level research overrides.
    if preferences.research.cache_ttl_seconds:
        settings.research.cache_ttl_seconds = int(preferences.research.cache_ttl_seconds)
    if preferences.research.max_pages is not None:
        settings.research.max_pages = int(preferences.research.max_pages)
    threshold = preferences.answer_memory.get("fuzzy_threshold")
    if threshold:
        settings.answers.reuse_threshold = float(threshold)
    if preferences.resumes.default and not (paths.resumes_dir / preferences.resumes.default).exists():
        warnings.append(f"preferences.yaml: resumes.default {preferences.resumes.default!r} not found in resumes/")
    for category, filename in preferences.resumes.mappings.items():
        if not (paths.resumes_dir / filename).exists():
            warnings.append(f"preferences.yaml: resumes.mappings.{category} -> {filename!r} not found in resumes/")

    if problems and strict:
        raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(problems))
    warnings += problems

    profile_path = paths.profile_dir / "profile.local.md"
    if not profile_path.exists():
        profile_path = paths.profile_dir / "profile.md"
    profile_text = profile_path.read_text(encoding="utf-8") if profile_path.exists() else ""
    templates: dict[str, str] = {}
    if paths.templates_dir.is_dir():
        for p in sorted(paths.templates_dir.glob("*.md")):
            if p.name.endswith(".local.md") or p.name.lower() == "readme.md":
                continue
            templates[p.stem] = p.read_text(encoding="utf-8")
        for p in sorted(paths.templates_dir.glob("*.local.md")):
            templates[p.name.removesuffix(".local.md")] = p.read_text(encoding="utf-8")

    digest = hashlib.sha256(
        json.dumps({"c": candidate, "p": prefs_raw, "profile": profile_text, "t": templates},
                   sort_keys=True, default=str).encode()
    ).hexdigest()
    return AppConfig(
        paths=paths, candidate=candidate, preferences=preferences, settings=settings,
        profile_text=profile_text, templates=templates, warnings=warnings, profile_hash=digest,
        token=os.environ.get("APPLYPILOT_TOKEN", ""),
        mcp_token=os.environ.get("APPLYPILOT_MCP_TOKEN", ""),
        host=os.environ.get("APPLYPILOT_HOST", "127.0.0.1"),
        port=int(os.environ.get("APPLYPILOT_PORT", "8765")),
    )
