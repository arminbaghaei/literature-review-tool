"""Paid AI runs: cost estimation, approval, background execution and coverage.

Every run covering several documents creates one job per document plus a finalize job, so progress
and failures are tracked per document and an interrupted run resumes where it stopped (completed
calls are served from the AI call cache and are not billed twice).
"""

from __future__ import annotations

import json
from collections.abc import Callable

from litreview import jobs
from litreview.ai.client import AIError, AIService, BudgetExceeded, estimate_tokens
from litreview.ai.prompts import SYSTEM_RULES
from litreview.analysis import drafting, extraction, qa, summaries, synthesis
from litreview.analysis.common import dumps, matrix_records
from litreview.context import AppContext
from litreview.documents import eligible_documents, get_document, list_documents

RUN_MATRIX = "matrix"
RUN_SUMMARIES = "summaries"
RUN_QA = "qa_exhaustive"
RUN_SYNTHESIS = "synthesis"
RUN_DRAFT = "draft"

RUN_LABELS = {
    RUN_MATRIX: "Matrix extraction",
    RUN_SUMMARIES: "Paper summaries",
    RUN_QA: "Question across papers (exhaustive)",
    RUN_SYNTHESIS: "Compare / themes / conflicts / gaps",
    RUN_DRAFT: "Draft review section",
}

# Tests inject a fake provider here; production uses the Claude provider from the environment.
provider_factory: Callable[[], object] | None = None


def make_ai(ctx: AppContext) -> AIService:
    return AIService(ctx, provider_factory() if provider_factory else None)


class RunError(Exception):
    pass


def _system_tokens() -> int:
    return estimate_tokens(SYSTEM_RULES) + 600  # + schema


def estimate(ctx: AppContext, project_id: str, kind: str, params: dict, doc_ids: list[str] | None) -> dict:
    """Estimate calls, tokens and cost. Output tokens include an allowance for model thinking."""
    ai = make_ai(ctx)
    docs = eligible_documents(ctx, project_id, doc_ids)
    calls = in_tok = out_tok = 0
    notes: list[str] = []
    if kind == RUN_MATRIX:
        for d in docs:
            for req in extraction.build_requests(ctx, project_id, d):
                calls += 1
                in_tok += estimate_tokens(req.user) + _system_tokens()
                out_tok += req.expected_output_tokens
    elif kind == RUN_SUMMARIES:
        for d in docs:
            try:
                req = summaries.build_request(ctx, project_id, d)
            except summaries.SummaryError as exc:
                notes.append(str(exc))
                continue
            calls += 1
            in_tok += estimate_tokens(req.user) + _system_tokens()
            out_tok += req.expected_output_tokens
    elif kind == RUN_QA:
        for d in docs:
            req, _ = qa.doc_request(ctx, project_id, d, params["question"])
            calls += 1
            in_tok += estimate_tokens(req.user) + _system_tokens()
            out_tok += req.expected_output_tokens
        calls += 1
        in_tok += 1500 * len(docs) // 3 + _system_tokens()
        out_tok += 4000
    elif kind in (RUN_SYNTHESIS, RUN_DRAFT):
        records, missing = matrix_records(ctx, project_id, docs)
        if missing:
            notes.append(
                f"{len(missing)} document(s) have no matrix extraction and will be listed as not covered."
            )
        rec_tokens = estimate_tokens(dumps(records))
        groups = max(1, -(-rec_tokens // ctx.settings.ai_synthesis_token_budget))
        calls = groups
        in_tok = rec_tokens + groups * (_system_tokens() + 1500)
        out_tok = 8000 * groups
    else:
        raise RunError(f"Unknown run kind {kind}")
    cost = ai.estimate_cost(in_tok, out_tok) if calls else 0.0
    return {
        "documents": len(docs),
        "calls": calls,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "estimated_cost_usd": round(cost, 4),
        "model": ai.model,
        "notes": notes,
    }


def start_run(
    ctx: AppContext,
    project_id: str,
    kind: str,
    params: dict,
    doc_ids: list[str] | None,
    approved_budget_usd: float,
    estimated_cost_usd: float,
) -> str:
    ai = make_ai(ctx)
    if not ai.available():
        raise RunError("AI is not configured: set ANTHROPIC_API_KEY (see README).")
    if approved_budget_usd <= 0:
        raise RunError("Approve a budget greater than zero to start a paid run.")
    remaining = ai.project_budget(project_id) - ai.project_spent(project_id)
    if approved_budget_usd > remaining + 1e-9:
        raise RunError(f"Approved budget exceeds the project's remaining AI budget (${remaining:.2f}).")
    docs = eligible_documents(ctx, project_id, doc_ids)
    if not docs:
        raise RunError("No eligible documents in scope (documents must be processed and included).")
    all_docs = list_documents(ctx, project_id)
    in_scope = set(doc_ids) if doc_ids else {d.id for d in all_docs}
    excluded = [
        f"{d.label} ({d.status if d.status not in ('completed', 'partial') else 'excluded'})"
        for d in all_docs
        if d.id in in_scope and not d.eligible
    ]
    params = {**params, "doc_ids": [d.id for d in docs], "excluded": excluded}
    per_doc = {
        RUN_MATRIX: jobs.JOB_AI_EXTRACT,
        RUN_SUMMARIES: jobs.JOB_AI_SUMMARY,
        RUN_QA: jobs.JOB_AI_QA_DOC,
    }
    job_doc_ids = [d.id for d in docs] if kind in per_doc else []
    return jobs.create_run(
        ctx,
        project_id,
        kind,
        params,
        job_doc_ids,
        per_doc.get(kind, jobs.JOB_AI_EXTRACT),
        estimated_cost_usd,
        approved_budget_usd,
        finalize=True,
    )


def handle_ai_job(ctx: AppContext, job: jobs.Job) -> None:
    run = jobs.get_run(ctx, job.run_id) if job.run_id else None
    if run is None:
        jobs.finish(ctx, job.id, "failed", error="Run record missing")
        return
    if run["status"] in ("cancelled", "stopped_budget"):
        jobs.finish(ctx, job.id, "cancelled", error=f"Run {run['status']}")
        return
    ai = make_ai(ctx)
    try:
        if job.kind == jobs.JOB_RUN_FINALIZE:
            result = _finalize(ai, job, run)
            jobs.finish(ctx, job.id, "done", result=result)
            return
        doc = get_document(ctx, job.project_id, job.doc_id or "")
        if doc is None:
            jobs.finish(ctx, job.id, "failed", error="Document no longer exists")
            return
        if job.kind == jobs.JOB_AI_EXTRACT:
            result = extraction.extract_document(ai, job.project_id, doc, run_id=run["id"])
        elif job.kind == jobs.JOB_AI_SUMMARY:
            result = summaries.generate_summary(ai, job.project_id, doc, run_id=run["id"])
        elif job.kind == jobs.JOB_AI_QA_DOC:
            result = qa.run_doc_question(ai, job.project_id, doc, run["params"]["question"], run["id"])
        else:
            raise RunError(f"Unknown job kind {job.kind}")
        jobs.finish(ctx, job.id, "done", result=result)
    except BudgetExceeded as exc:
        jobs.finish(ctx, job.id, "failed", error=exc.user_message)
        _stop_for_budget(ctx, run, exc.user_message)
    except AIError as exc:
        if exc.retryable and job.attempts < jobs.MAX_AUTO_ATTEMPTS:
            ctx.db.execute(
                "UPDATE jobs SET status = 'queued', error = ? WHERE id = ?", (exc.user_message, job.id)
            )
        else:
            jobs.finish(ctx, job.id, "failed", error=exc.user_message)
            if job.kind == jobs.JOB_RUN_FINALIZE:
                jobs.set_run_status(ctx, run["id"], "failed", error=exc.user_message)
    except (summaries.SummaryError, ValueError, RunError) as exc:
        jobs.finish(ctx, job.id, "failed", error=str(exc))
        if job.kind == jobs.JOB_RUN_FINALIZE:
            jobs.set_run_status(ctx, run["id"], "failed", error=str(exc))


def _stop_for_budget(ctx: AppContext, run: dict, message: str) -> None:
    ctx.db.execute(
        "UPDATE jobs SET status = 'failed', error = ? WHERE run_id = ? AND status IN ('queued', 'waiting')",
        (f"Not processed: {message}", run["id"]),
    )
    jobs.set_run_status(ctx, run["id"], "stopped_budget", error=message)


def _per_doc_summary(ctx: AppContext, run_id: str) -> dict:
    rows = ctx.db.query(
        "SELECT j.status, j.error, d.label FROM jobs j LEFT JOIN documents d ON d.id = j.doc_id"
        " WHERE j.run_id = ? AND j.kind != ? ORDER BY d.label",
        (run_id, jobs.JOB_RUN_FINALIZE),
    )
    counts: dict[str, int] = {}
    failures = []
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        if r["status"] != "done":
            failures.append({"doc_label": r["label"], "status": r["status"], "error": r["error"]})
    return {"counts": counts, "failures": failures, "total": len(rows)}


def _finalize(ai: AIService, job: jobs.Job, run: dict) -> dict:
    ctx = ai.ctx
    kind = run["kind"]
    summary = _per_doc_summary(ctx, run["id"])
    if kind == RUN_QA:
        summary.update(qa.finalize_exhaustive(ai, job.project_id, run))
    elif kind == RUN_SYNTHESIS:
        summary.update(synthesis.run_synthesis(ai, job.project_id, run))
    elif kind == RUN_DRAFT:
        summary.update(drafting.run_draft(ai, job.project_id, run))
    status = "completed" if not summary["failures"] else "completed_with_failures"
    jobs.set_run_status(ctx, run["id"], status, result=summary)
    return summary


def run_progress(ctx: AppContext, run_id: str) -> dict:
    rows = ctx.db.query(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE run_id = ? AND kind != ? GROUP BY status",
        (run_id, jobs.JOB_RUN_FINALIZE),
    )
    counts = {r["status"]: r["n"] for r in rows}
    final = ctx.db.query_one(
        "SELECT status, error FROM jobs WHERE run_id = ? AND kind = ?", (run_id, jobs.JOB_RUN_FINALIZE)
    )
    return {"counts": counts, "total": sum(counts.values()), "finalize": dict(final) if final else None}


def get_output(ctx: AppContext, project_id: str, output_id: str) -> dict | None:
    row = ctx.db.query_one("SELECT * FROM outputs WHERE id = ? AND project_id = ?", (output_id, project_id))
    if row is None:
        return None
    d = dict(row)
    for key in ("request", "content_json", "coverage", "citation_report"):
        d[key] = json.loads(d[key]) if d[key] else None
    return d


def list_outputs(ctx: AppContext, project_id: str, kind: str | None = None) -> list[dict]:
    if kind:
        rows = ctx.db.query(
            "SELECT id, kind, title, source, model, created_at, updated_at FROM outputs WHERE project_id = ? AND kind = ?"
            " ORDER BY created_at DESC",
            (project_id, kind),
        )
    else:
        rows = ctx.db.query(
            "SELECT id, kind, title, source, model, created_at, updated_at FROM outputs WHERE project_id = ?"
            " ORDER BY created_at DESC",
            (project_id,),
        )
    return [dict(r) for r in rows]
