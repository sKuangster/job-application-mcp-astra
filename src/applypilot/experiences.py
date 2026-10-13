"""Local experience retrieval and a progressively confirmed anecdote bank.

Suggestions retain exact source excerpts. Only explicit approval changes their
confirmation status. Candidate profile/resume documents remain separate evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import yaml

from applypilot.config import AppConfig
from applypilot.db import Database

THEMES = {
    "problem-solving": r"debug|solv|challenge|problem|fix",
    "leadership": r"\bled\b|leader|coordinat|organiz",
    "collaboration": r"team|collaborat|together|partner",
    "conflict": r"conflict|disagree|resolv|negotiat",
    "persistence": r"persist|retry|overcame|obstacle",
    "learning": r"learn|stud|experiment|adapt",
    "initiative": r"initiat|started|propos|independent",
    "ownership": r"built|implement|design|maintain|own",
}
STOP = {"the", "and", "with", "your", "this", "that", "have", "what", "would", "company", "role", "work", "about"}


def terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#.-]{2,}", text.casefold()) if w not in STOP}


def themes(text: str) -> list[str]:
    return [name for name, pattern in THEMES.items() if re.search(pattern, text, re.I)]


class ExperienceBank:
    def __init__(self, cfg: AppConfig, db: Database):
        self.cfg, self.db = cfg, db

    def documents(self) -> list[dict[str, Any]]:
        docs = []
        heading = "Profile"
        for part in re.split(r"\n\s*\n", self.cfg.profile_text):
            part = part.strip()
            if part.startswith("#"):
                lines = part.splitlines()
                heading = lines[0].lstrip("# ")
                part = "\n".join(lines[1:]).strip()
            if len(part) < 35 or re.match(r"record |describe |for each |this file is|do not invent", part, re.I):
                continue
            docs.append(self._document(heading, part[:1500], "profile", self.cfg.profile_hash))
        employers = self.cfg.candidate.get("experience", {}).get("employers") or []
        for employer in employers:
            if isinstance(employer, dict):
                docs.append(self._document(str(employer.get("company") or "Employment"),
                                           json.dumps(employer, ensure_ascii=False), "candidate.experience.employers", self.cfg.profile_hash))
        # PDF extraction is local only. Scanned or malformed PDFs supply no invented evidence.
        try:
            from pypdf import PdfReader
        except ImportError:
            return docs
        for path in sorted(self.cfg.paths.resumes_dir.glob("*.pdf"))[:5]:
            if path.stat().st_size > 2_000_000:
                continue
            try:
                reader = PdfReader(path)
                text = "\n".join(page.extract_text() or "" for page in reader.pages[:5])[:12000]
                docs.append(self._document("Resume", text, "resume", path.name))
            except Exception:
                continue
        return docs

    @staticmethod
    def _document(title: str, core: str, kind: str, locator: str) -> dict[str, Any]:
        key = hashlib.sha256((kind + locator + core).encode()).hexdigest()[:24]
        return {"id": "experience_" + key, "title": title[:150], "core": core, "themes": themes(core),
                "source": {"kind": kind, "locator": locator}, "status": "documented"}

    def import_local(self) -> None:
        path = (self.cfg.paths.root / self.cfg.settings.reasoning.writing.experience_file).resolve()
        if not path.is_file() or path.stat().st_size > 200_000:
            return
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list):
            raise ValueError("experience bank YAML must be an array")
        for item in raw[:100]:
            if not isinstance(item, dict) or item.get("status") not in {"suggested", "confirmed", "rejected"}:
                raise ValueError("each experience needs an explicit confirmation status")
            key = str(item.get("id") or "experience_" + hashlib.sha256(str(item.get("core") or "").strip().encode()).hexdigest()[:24])
            # Import is additive: a stale suggested YAML record must not undo
            # explicit confirmation/rejection made in the local bank.
            if not self.db.query("SELECT id FROM experiences WHERE id=?", (key,)):
                self.store(item)

    def store(self, item: dict[str, Any]) -> str:
        core = str(item.get("core") or "").strip()
        if not core or len(core) > 6000:
            raise ValueError("experience core must contain 1-6000 characters")
        key = str(item.get("id") or "experience_" + hashlib.sha256(core.encode()).hexdigest()[:24])
        status = str(item.get("status") or "suggested")
        if status not in {"suggested", "confirmed", "rejected"}:
            raise ValueError("invalid experience status")
        self.db.write(lambda c: c.execute(
            "INSERT INTO experiences(id,title,core,themes,source,status,updated_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title,core=excluded.core,themes=excluded.themes,"
            "source=excluded.source,status=excluded.status,updated_at=excluded.updated_at",
            (key, str(item.get("title") or "Experience")[:150], core, json.dumps(item.get("themes") or themes(core)),
             json.dumps(item.get("source") or {"kind": "user"}), status, self.db.clock())))
        return key

    def suggest(self) -> list[dict[str, Any]]:
        for document in self.documents():
            existing = self.db.query("SELECT id FROM experiences WHERE id=?", (document["id"],))
            if not existing:
                self.store({**document, "core": document["core"][:1500], "status": "suggested"})
        return self.entries()

    def entries(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM experiences ORDER BY updated_at DESC")
        for row in rows:
            row["themes"], row["source"] = json.loads(row["themes"]), json.loads(row["source"])
        return rows

    def confirm(self, key: str, *, approved: bool) -> None:
        if not self.db.query("SELECT id FROM experiences WHERE id=?", (key,)):
            raise KeyError("unknown experience")
        self.db.write(lambda c: c.execute("UPDATE experiences SET status=?,updated_at=? WHERE id=?",
                                         ("confirmed" if approved else "rejected", self.db.clock(), key)))

    def retrieve(self, query: str, *, anecdote: bool = False) -> list[dict[str, Any]]:
        self.import_local()
        corpus = [row for row in self.entries() if row["status"] == "confirmed"] + self.documents()
        wanted = terms(query)
        wanted_themes = set(themes(query))
        ranked = []
        for doc in corpus:
            if anecdote and not re.search(r"\bbuilt\b|\bled\b|\bdebugged\b|\bresolved\b|\bimplemented\b|\bcollaborated\b|\borganized\b", doc["core"], re.I):
                continue
            score = len(wanted & terms(doc["core"])) + 3 * len(wanted_themes & set(doc["themes"]))
            if score or (anecdote and doc["status"] == "confirmed"):
                ranked.append({**doc, "relevance": score})
        ranked.sort(key=lambda d: (-d["relevance"], d["id"]))
        seen: set[str] = set()
        result = []
        for doc in ranked:
            if doc["core"] in seen:
                continue
            seen.add(doc["core"])
            result.append({**doc, "core": doc["core"][:1500]})
        return result[:self.cfg.settings.reasoning.writing.max_evidence_items]
