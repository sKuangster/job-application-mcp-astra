from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "config_root"
sys.path.insert(0, str(ROOT / "tests"))


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated project root with the fictional test persona and two resumes."""
    from applypilot.pdfgen import text_to_pdf

    root = tmp_path / "proj"
    shutil.copytree(FIXTURE_ROOT, root)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    for name in ("resume_swe.pdf", "resume_ml.pdf"):
        (root / "resumes" / name).write_bytes(text_to_pdf(f"Testy McFixture — {name}"))
    for var in ("APPLYPILOT_DATA_DIR", "APPLYPILOT_HOST", "APPLYPILOT_PORT", "APPLYPILOT_ACCOUNT_EMAIL",
                "APPLYPILOT_ACCOUNT_PASSWORD", "APPLYPILOT_IMAP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("APPLYPILOT_ROOT", str(root))
    monkeypatch.setenv("APPLYPILOT_TOKEN", "test-token-123")
    monkeypatch.setenv("APPLYPILOT_MCP_TOKEN", "mcp-token-456")
    return root


@pytest.fixture
def cfg(project: Path) -> Any:
    from applypilot.config import load_config

    return load_config(project)


def edit_yaml(root: Path, rel: str, patch: dict[str, Any]) -> None:
    from applypilot.config import deep_merge

    path = root / rel
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    write_yaml(path, deep_merge(data, patch))


@pytest.fixture
def db(cfg: Any) -> Any:
    from applypilot.db import Database

    database = Database(cfg.paths.db_path)
    database.initialize()
    return database


def make_job(db: Any, *, company: str = "Fixture Robotics", title: str = "Software Engineering Intern",
             url: str = "https://job-boards.greenhouse.io/fixture/jobs/111", sid: str | None = None,
             details: dict[str, Any] | None = None, batch: str = "baseline", **extra: Any) -> str:
    from applypilot.ingest.listings import JobRecord

    rec = JobRecord(company=company, title=title, locations=extra.pop("locations", ["Seattle, WA"]),
                    simplify_id=sid, simplify_url=f"https://simplify.jobs/p/{sid}" if sid else None, direct_url=url,
                    sponsorship=extra.pop("sponsorship", "Other"), degrees=extra.pop("degrees", ["Bachelor's"]),
                    terms=["Summer 2027"], category=extra.pop("category", "Software")).to_db()
    aliases = list(rec["_aliases"])
    up = db.upsert_jobs([rec], batch=batch)
    job_id = (up["new_ids"] or [db.find_job_by_aliases(aliases) or rec["id"]])[0]
    if details is not None or extra:
        db.update_job(job_id, **({"details": details} if details is not None else {}), **extra)
    return job_id


def page_field(**kw: Any) -> Any:
    from applypilot.models import PageField

    base = {"id": kw.pop("id", "0:1"), "key": kw.pop("key", "f_" + str(abs(hash(kw.get("label", ""))))[:8])}
    opts = kw.pop("options", None)
    if opts is not None:
        kw["options"] = [{"label": o, "value": o} for o in opts]
    return PageField(**base, **kw)
