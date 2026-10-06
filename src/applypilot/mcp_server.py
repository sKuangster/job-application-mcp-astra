"""MCP tools for Astra (ChatGPT) — high-level, typed, token-efficient.

Astra is the brain, not the hands: the local engine drives Firefox and resolves every
deterministic field itself. Astra's loop is:

    start_autopilot(mode="backlog")
    repeat:
        batch = get_reasoning_batch(wait_seconds=25)   # only questions needing judgement
        submit_reasoning(results=[...])                  # many answers in one call
    until get_status().next_step says there is nothing left

Tools are defined once and dispatched either in-process (streamable HTTP mounted in the
backend) or over the local HTTP API (``applypilot mcp`` stdio proxy).
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

Caller = Callable[[str, dict[str, Any]], Awaitable[Any]]

INSTRUCTIONS = """\
ApplyPilot applies to internships on the user's behalf through their real Firefox browser.
Deterministic work (navigation, known profile fields, EEO/preferences, uploads, duplicate checks,
validation, submission) happens locally; you only answer what needs judgement.

Typical session ("apply to every open internship I'm eligible for"):
1. get_status → check config_ready / browser_connected; relay config_missing to the user if needed.
2. start_autopilot(mode="backlog") (or "new_jobs" for only newly added postings, "continuous" for both).
3. Loop: get_reasoning_batch(wait_seconds=25) → answer EVERY item → submit_reasoning(results).
   Stop when the batch is empty and get_status shows queue_remaining == 0 and pending_reasoning == 0.
4. Report applications needing the user (needs_user) — CAPTCHAs, missing facts — with their instructions.
Never invent candidate facts: answer {"item_id", "needs_user": "..."} when the profile lacks a fact.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


async def dispatch(service: Any, op: str, a: dict[str, Any]) -> Any:
    """Single operation table used by MCP tools and the /api/rpc proxy endpoint."""
    if op == "get_status":
        return service.compact_status()
    if op == "start_autopilot":
        return await service.start_autopilot(a.get("mode") or "backlog", bool(a.get("refresh_jobs", True)))
    if op in ("pause_autopilot", "resume_autopilot", "stop_autopilot"):
        await service.control(op.split("_")[0])
        return service.compact_status()
    if op == "sync_jobs":
        return await service.sync(a.get("mode") or "backlog")
    if op == "get_queue":
        items = service.queue(int(a.get("limit") or 25))["items"]
        return {"items": [{k: i[k] for k in ("id", "company", "title", "state", "ats")} for i in items]}
    if op == "get_reasoning_batch":
        return await service.reasoning_batch(a.get("max_items"), float(a.get("wait_seconds", 20)))
    if op == "submit_reasoning":
        return service.submit_reasoning(list(a.get("results") or []))
    if op == "get_candidate_context":
        return service.candidate_context(a.get("sections"))
    if op == "save_company_research":
        return service.save_research(a["company"], a["summary"], a.get("sources") or [], a.get("facts"),
                                     a.get("topic") or "company")
    if op == "search_applications":
        res = service.search(q=a.get("query"), company=a.get("company"), state=a.get("state"),
                             limit=int(a.get("limit") or 20))
        return {"total": res["total"], "items": [{k: i[k] for k in ("id", "company", "title", "state", "state_reason",
                                                                     "updated_at", "resume")} for i in res["items"]]}
    if op == "get_application_log":
        return service.application_log(a["application_id"])
    if op == "application_action":
        return await service.action(a["application_id"], a["action"], a.get("note"))
    if op == "provide_answer":
        return service.answer(a["application_id"], a["field_key"], a.get("value"), bool(a.get("remember")))
    if op == "apply_to_url":
        return service.e.add_manual_job(a["url"], company=a.get("company"), title=a.get("title"))
    if op == "inspect_page":
        return await service.inspect_page(a["application_id"], bool(a.get("include_text")))
    if op == "get_page_screenshot":
        return await service.screenshot(a["application_id"])
    if op == "perform_page_action":
        return await service.page_action(a["application_id"], dict(a.get("action") or {}))
    if op == "get_metrics":
        return service.metrics()
    raise ValueError(f"unknown operation {op}")


def build_mcp(service: Any = None, *, caller: Caller | None = None) -> FastMCP:
    if caller is None:
        async def caller(op: str, args: dict[str, Any]) -> Any:
            return await dispatch(service, op, args)

    mcp = FastMCP(
        "ApplyPilot", instructions=INSTRUCTIONS, stateless_http=True, json_response=True,
        # Auth is enforced by the backend's MCPAuth wrapper (token header or capability URL),
        # which also makes tunnelled hosts (for ChatGPT) safe to accept.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(annotations=READ)
    async def get_status() -> dict[str, Any]:
        """Autopilot status: control state, queue size, today's counts, busy workers, applications needing the
        user, pending reasoning, config readiness, and a `next_step` hint."""
        return await caller("get_status", {})

    @mcp.tool(annotations=WRITE)
    async def start_autopilot(
        mode: Annotated[Literal["backlog", "new_jobs", "continuous"], Field(description=(
            "backlog = every open, never-processed eligible job; new_jobs = only jobs added after the first sync; "
            "continuous = backlog, then keep polling for new jobs"))] = "backlog",
        refresh_jobs: Annotated[bool, Field(description="Sync the SimplifyJobs repository first")] = True,
    ) -> dict[str, Any]:
        """Sync jobs (optional) and start the worker pool. Idempotent: calling again just keeps it running."""
        return await caller("start_autopilot", {"mode": mode, "refresh_jobs": refresh_jobs})

    @mcp.tool(annotations=WRITE)
    async def pause_autopilot() -> dict[str, Any]:
        """Stop claiming new work; in-flight steps finish at a safe point; nothing new is submitted."""
        return await caller("pause_autopilot", {})

    @mcp.tool(annotations=WRITE)
    async def resume_autopilot() -> dict[str, Any]:
        """Resume a paused autopilot from persisted state."""
        return await caller("resume_autopilot", {})

    @mcp.tool(annotations=WRITE)
    async def stop_autopilot() -> dict[str, Any]:
        """Stop all workers safely (state persisted, leases released)."""
        return await caller("stop_autopilot", {})

    @mcp.tool(annotations=WRITE)
    async def sync_jobs(mode: Literal["backlog", "new_jobs"] = "backlog") -> dict[str, Any]:
        """Fetch the SimplifyJobs listings and queue unprocessed jobs for the given mode. Returns counts."""
        return await caller("sync_jobs", {"mode": mode})

    @mcp.tool(annotations=READ)
    async def get_queue(limit: Annotated[int, Field(ge=1, le=200)] = 25) -> dict[str, Any]:
        """Next applications in the queue (compact)."""
        return await caller("get_queue", {"limit": limit})

    @mcp.tool(annotations=READ)
    async def get_reasoning_batch(
        wait_seconds: Annotated[float, Field(ge=0, le=55, description="Long-poll until work arrives")] = 20,
        max_items: Annotated[int | None, Field(ge=1, le=50)] = None,
    ) -> dict[str, Any]:
        """Questions only you can answer, batched across all running applications: novel screening questions,
        personalized written answers (with templates, profile, job description and cached company research),
        eligibility reviews and unrecognized pages. Answer every item, then call submit_reasoning once."""
        return await caller("get_reasoning_batch", {"wait_seconds": wait_seconds, "max_items": max_items})

    @mcp.tool(annotations=WRITE)
    async def submit_reasoning(
        results: Annotated[list[dict[str, Any]], Field(description=(
            'One entry per item: {"item_id", "value", "fact_paths": [...], "template": "...", "confidence": 0-1} | '
            '{"item_id", "needs_user": "what is missing"} | eligibility: {"item_id", "eligible": bool, "reason"} | '
            'page: {"item_id", "action": {"type": "click_button"|"click_text"|"click_point"|"skip_job"|"needs_user"|"wait", ...}}. '
            'Optionally add {"research": {"company", "summary", "facts": [...], "sources": [{"url", "title", "official"}]}}.'))],
    ) -> dict[str, Any]:
        """Deliver answers. Each answer is validated (options, length limits, no invented credentials);
        rejected items come back with a reason and can be resubmitted. Accepted answers resume their applications."""
        return await caller("submit_reasoning", {"results": results})

    @mcp.tool(annotations=READ)
    async def get_candidate_context(
        sections: list[Literal["profile", "templates", "preferences"]] | None = None,
    ) -> dict[str, Any]:
        """Candidate facts plus the long-form profile and/or writing templates (source of truth)."""
        return await caller("get_candidate_context", {"sections": sections})

    @mcp.tool(annotations=WRITE)
    async def save_company_research(company: str, summary: str, sources: list[dict[str, Any]],
                                    facts: list[str] | None = None, topic: str = "company") -> dict[str, Any]:
        """Cache company research (with source URLs) so later applications to this company reuse it."""
        return await caller("save_company_research", {"company": company, "summary": summary, "sources": sources,
                                                      "facts": facts, "topic": topic})

    @mcp.tool(annotations=READ)
    async def search_applications(query: str | None = None, company: str | None = None,
                                  state: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """Find applications by text/company/state (state may be comma-separated, e.g. 'FAILED,NEEDS_USER')."""
        return await caller("search_applications", {"query": query, "company": company, "state": state, "limit": limit})

    @mcp.tool(annotations=READ)
    async def get_application_log(application_id: str) -> dict[str, Any]:
        """Exactly how one application was handled: eligibility, resume, every question/answer with its
        source (candidate.yaml path, template, memory, generated), timeline and submission evidence."""
        return await caller("get_application_log", {"application_id": application_id})

    @mcp.tool(annotations=WRITE)
    async def application_action(
        application_id: str,
        action: Literal["resume", "retry", "skip", "submit", "open", "confirm_submitted"],
        note: str | None = None,
    ) -> dict[str, Any]:
        """resume (after the user fixed a CAPTCHA/login), retry a failed one, skip, submit (approve a
        READY_TO_SUBMIT application when auto-submit is off — still passes the validation gate), open (focus
        its Firefox window), confirm_submitted (user verified an unconfirmed submission)."""
        return await caller("application_action", {"application_id": application_id, "action": action, "note": note})

    @mcp.tool(annotations=WRITE)
    async def provide_answer(application_id: str, field_key: str, value: Any, remember: bool = False) -> dict[str, Any]:
        """Supply a fact the user told you for a NEEDS_USER question (remember=true reuses it for future jobs).
        Only use values the USER provided."""
        return await caller("provide_answer", {"application_id": application_id, "field_key": field_key, "value": value,
                                               "remember": remember})

    @mcp.tool(annotations=WRITE)
    async def apply_to_url(url: str, company: str | None = None, title: str | None = None) -> dict[str, Any]:
        """Queue one specific posting (Simplify link preferred) at top priority."""
        return await caller("apply_to_url", {"url": url, "company": company, "title": title})

    @mcp.tool(annotations=READ)
    async def inspect_page(application_id: str, include_text: bool = False) -> dict[str, Any]:
        """Structured view of an application's current page (fields, buttons, errors) — no HTML dump."""
        return await caller("inspect_page", {"application_id": application_id, "include_text": include_text})

    @mcp.tool(annotations=READ)
    async def get_page_screenshot(application_id: str) -> Image:
        """Screenshot of an application's window (visual fallback only, when the structured view is insufficient)."""
        res = await caller("get_page_screenshot", {"application_id": application_id})
        data_url = (res or {}).get("data_url") or ""
        if "," not in data_url:
            raise ValueError("screenshot unavailable")
        header, b64 = data_url.split(",", 1)
        return Image(data=base64.b64decode(b64), format="png" if "png" in header else "jpeg")

    @mcp.tool(annotations=WRITE)
    async def perform_page_action(application_id: str, action: dict[str, Any]) -> dict[str, Any]:
        """Visual/DOM fallback on a PARKED application: {"type": "click_point", "x", "y"} |
        {"type": "click_text", "text"} | {"type": "click_button", "button_id"}. Then call application_action(resume)."""
        return await caller("perform_page_action", {"application_id": application_id, "action": action})

    @mcp.tool(annotations=READ)
    async def get_metrics() -> dict[str, Any]:
        """Throughput and cost metrics: time per application, page waits, reasoning batches/items, research,
        answers by method, retries."""
        return await caller("get_metrics", {})

    return mcp


def build_stdio_proxy(base_url: str, token: str) -> FastMCP:
    """stdio MCP server that forwards every tool to the running backend's /api/rpc."""
    import httpx

    async def caller(op: str, args: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(f"{base_url.rstrip('/')}/api/rpc/{op}", json=args,
                                     headers={"X-ApplyPilot-Token": token})
            if resp.status_code >= 400:
                raise RuntimeError(f"backend error {resp.status_code}: {resp.text[:500]}")
            return resp.json()

    return build_mcp(caller=caller)
