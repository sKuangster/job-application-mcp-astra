"""Deterministic resume selection.

1. Classify the job into a category using ordered keyword rules from
   ``preferences.resumes.categories`` (title first, then Simplify category/functions).
2. Map the category to a file: explicit ``mappings`` > filename aliases > default.
3. Fall back to ``resumes.default`` (or the only resume present).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from applypilot.config import AppConfig, list_resume_files

DEFAULT_CATEGORIES: dict[str, list[str]] = {
    "quant": ["quantitative", "quant ", "quant-", "trading", "trader"],
    "ml": ["machine learning", "deep learning", "artificial intelligence", " ai ", " ml ", "ai/ml", "ml/ai",
           "computer vision", "nlp", "llm", "applied scientist", "research scientist"],
    "data": ["data scien", "data analy", "data engineer", "analytics", "business intelligence", "statistic"],
    "hardware": ["hardware", "electrical", "embedded", "firmware", "fpga", "asic", "silicon", "circuit"],
    "product": ["product manager", "product management", "associate product", " apm"],
    "swe": ["software", "developer", "backend", "back-end", "frontend", "front-end", "full stack", "fullstack",
            "full-stack", "platform", "infrastructure", "devops", "site reliability", "sre", "mobile", "ios",
            "android", "web", "security engineer", "systems engineer", "engineer"],
}
DEFAULT_ALIASES: dict[str, list[str]] = {
    "quant": ["quant", "trading"], "ml": ["ml", "ai", "machine_learning", "machinelearning"],
    "data": ["data", "analytics", "ds"], "hardware": ["hardware", "hw", "embedded", "firmware", "ee"],
    "product": ["product", "pm"], "swe": ["swe", "software", "sde", "developer", "dev", "se"],
}
_LISTING_CATEGORY_HINTS = {
    "quant": "quant", "quantitative finance": "quant", "hardware": "hardware", "hardware engineering": "hardware",
    "product": "product", "product management": "product", "software": "swe", "software engineering": "swe",
    "ai/ml/data": "ml", "data science, ai & machine learning": "ml",
}


@dataclass
class ResumeChoice:
    filename: str | None
    path: str | None
    category: str | None
    method: str  # mapping | alias | default | only_file | none
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_job(job: dict[str, Any], cfg: AppConfig) -> tuple[str | None, str]:
    categories = cfg.preferences.resumes.categories or DEFAULT_CATEGORIES
    title = f" {str(job.get('title') or '').casefold()} "
    for category, keywords in categories.items():
        for kw in keywords:
            if kw.casefold() in title:
                return category, f"title contains {kw.strip()!r}"
    listing = str(job.get("category") or "").casefold()
    hinted = _LISTING_CATEGORY_HINTS.get(listing)
    if hinted and hinted in categories:
        # AI/ML/Data listings: decide data vs ml by title text when possible
        return hinted, f"Simplify category {job.get('category')!r}"
    functions = " ".join((job.get("details") or {}).get("functions") or []).casefold()
    for category, keywords in categories.items():
        if any(kw.casefold().strip() in functions for kw in keywords if kw.strip()):
            return category, f"Simplify functions {functions!r}"
    return None, "no category keyword matched"


def _tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", Path(name).stem.casefold().replace("-", "_")))


def select_resume(job: dict[str, Any], cfg: AppConfig) -> ResumeChoice:
    files = list_resume_files(cfg.paths.resumes_dir)
    by_name = {f.name: f for f in files}
    category, why = classify_job(job, cfg)
    prefs = cfg.preferences.resumes
    if category and category in prefs.mappings and prefs.mappings[category] in by_name:
        f = by_name[prefs.mappings[category]]
        return ResumeChoice(f.name, str(f), category, "mapping",
                            f"category {category} ({why}) → resumes.mappings.{category}")
    if category:
        aliases = [a.casefold() for a in (prefs.filename_aliases.get(category) or DEFAULT_ALIASES.get(category, [category]))]
        matches = [f for f in files if _tokens(f.name) & set(aliases)]
        if len(matches) == 1:
            return ResumeChoice(matches[0].name, str(matches[0]), category, "alias",
                                f"category {category} ({why}) → filename alias match")
        if len(matches) > 1:
            # Prefer the shortest/most specific filename deterministically.
            best = sorted(matches, key=lambda f: (len(_tokens(f.name)), f.name))[0]
            return ResumeChoice(best.name, str(best), category, "alias",
                                f"category {category} ({why}) → {len(matches)} alias matches, chose {best.name}")
    if prefs.default and prefs.default in by_name:
        f = by_name[prefs.default]
        return ResumeChoice(f.name, str(f), category, "default",
                            f"no category-specific resume ({why}) → resumes.default")
    if len(files) == 1:
        return ResumeChoice(files[0].name, str(files[0]), category, "only_file", "only one resume available")
    if files:
        generic = [f for f in files if _tokens(f.name) & {"general", "default", "main", "resume", "cv"}]
        pick = sorted(generic or files, key=lambda f: f.name)[0]
        return ResumeChoice(pick.name, str(pick), category, "default",
                            "no mapping/alias and no resumes.default configured; picked deterministic fallback")
    return ResumeChoice(None, None, category, "none", "no resume files in resumes/")
