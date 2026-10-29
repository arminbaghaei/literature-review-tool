"""Questions about one paper, selected papers or the whole collection.

Two modes:

- **Focused** (synchronous): keyword retrieval of the most relevant passages, one AI call. The
  answer states that it is based on top passages only and is not exhaustive.
- **Exhaustive** (background run): every eligible document in scope is examined individually
  (full text when it fits, otherwise its most relevant passages, which is reported as partial
  coverage). A coverage row is recorded for every document before the answer is synthesised.
"""

from __future__ import annotations

import json

from litreview import jobs
from litreview.ai.client import AIRequest, AIService, estimate_tokens
from litreview.ai.prompts import (
    QA_DOC_VERSION,
    QA_REDUCE_VERSION,
    QA_VERSION,
    SYSTEM_RULES,
    AnswerOutput,
    DocAnswer,
    escape_doc_text,
    format_pages,
    qa_doc_prompt,
    qa_prompt,
    qa_reduce_prompt,
)
from litreview.analysis.common import (
    check_claims,
    coverage_note,
    dumps,
    page_lookup,
    readable_pages,
    render_claims,
    split_sections,
)
from litreview.context import AppContext
from litreview.db import new_id, now
from litreview.documents import Document, eligible_documents
from litreview.search import search

FOCUSED_PASSAGES = 24
PER_DOC_PASSAGES = 4


def _passages_xml(hits) -> str:
    parts = []
    for h in hits:
        section = f' section="{escape_doc_text(h.section)}"' if h.section else ""
        parts.append(
            f'<passage doc_label="{h.label}" page="{h.page_no}"{section}>\n{escape_doc_text(h.text)}\n</passage>'
        )
    return "<passages>\n" + "\n".join(parts) + "\n</passages>"


def _save_output(
    ctx: AppContext,
    project_id: str,
    kind: str,
    title: str,
    request: dict,
    content_md: str,
    content_json: dict,
    coverage: dict,
    citation_report: dict,
    model: str | None,
    prompt_version: str,
) -> str:
    out_id = new_id()
    ctx.db.execute(
        "INSERT INTO outputs(id, project_id, kind, title, request, content_md, content_json, coverage,"
        " citation_report, source, model, prompt_version, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?, ?, ?)",
        (
            out_id,
            project_id,
            kind,
            title[:200],
            json.dumps(request),
            content_md,
            json.dumps(content_json, ensure_ascii=False),
            json.dumps(coverage),
            json.dumps(citation_report),
            model,
            prompt_version,
            now(),
            now(),
        ),
    )
    return out_id


def _citation_summary(claims: list[dict]) -> dict:
    total = sum(len(c["citations"]) for c in claims)
    verified = sum(1 for c in claims for ci in c["citations"] if ci["status"] == "verified")
    unsupported = [c["text"][:160] for c in claims if not c["supported"]]
    return {"citations": total, "verified": verified, "unsupported_claims": unsupported}


def render_answer(
    question: str, status: str, claims: list[dict], caveats: list[str], missing: list[str], coverage_text: str
) -> str:
    md = [f"**Question:** {question}", "", f"*{coverage_text}*", ""]
    if status == "not_answerable_from_sources":
        md.append("**The supplied sources do not answer this question.**")
    elif status == "partially_answered":
        md.append("**Partial answer** - some aspects are not covered by the sources.")
    md += ["", render_claims(claims)]
    if caveats:
        md += ["", "**Conflicts and caveats**", *[f"- {c}" for c in caveats]]
    if missing:
        md += ["", "**Not found in the sources**", *[f"- {m}" for m in missing]]
    md += ["", "*AI-generated. Claims marked as tool synthesis are interpretation, not reported results.*"]
    return "\n".join(md)


def ask_focused(ai: AIService, project_id: str, question: str, doc_ids: list[str] | None) -> dict:
    ctx = ai.ctx
    docs = eligible_documents(ctx, project_id, doc_ids)
    if not docs:
        return {"error": "No eligible (processed and included) documents in scope."}
    hits = search(
        ctx,
        project_id,
        question,
        doc_ids=[d.id for d in docs],
        limit=FOCUSED_PASSAGES,
        per_doc_limit=PER_DOC_PASSAGES,
    )
    hit_docs = sorted({h.label for h in hits})
    coverage = {
        "mode": "focused",
        "documents_in_scope": len(docs),
        "documents_with_passages": len(hit_docs),
        "labels_with_passages": hit_docs,
        "passages": len(hits),
    }
    coverage_text = (
        f"Focused answer from the top {len(hits)} keyword-matched passages in {len(hit_docs)} of "
        f"{len(docs)} documents in scope. This is not an exhaustive review of every document."
    )
    if not hits:
        return {
            "error": "No passages matched the question's keywords, so nothing was sent to the AI. "
            "Try different wording or use exhaustive mode.",
            "coverage": coverage,
        }
    scope_note = (
        "These passages were retrieved by keyword search and may not include every relevant part "
        "of every paper. Say so if the passages are insufficient."
    )
    req = AIRequest(
        purpose="qa_focused",
        system=SYSTEM_RULES,
        user=qa_prompt(question, _passages_xml(hits), scope_note),
        output_model=AnswerOutput,
        prompt_version=QA_VERSION,
        expected_output_tokens=3000,
    )
    result = ai.call(project_id, req)
    data: AnswerOutput = result.data  # type: ignore[assignment]
    lookup = page_lookup(ctx, [d for d in docs if d.label in hit_docs])
    # Only pages actually supplied to the model count as valid citations.
    supplied = {(h.label, h.page_no) for h in hits}
    claims = check_claims(data.claims, lookup)
    for claim in claims:
        for ci in claim["citations"]:
            if ci["status"] == "verified" and (ci["doc_label"], ci["page"]) not in supplied:
                ci["status"] = "verified (page not among supplied passages)"
        claim["supported"] = any(ci["status"].startswith("verified") for ci in claim["citations"])
    md = render_answer(
        question,
        data.answer_status,
        claims,
        data.conflicts_or_caveats,
        data.missing_information,
        coverage_text,
    )
    report = _citation_summary(claims)
    out_id = _save_output(
        ctx,
        project_id,
        "qa",
        question,
        {"question": question, "mode": "focused", "doc_ids": doc_ids},
        md,
        {
            "answer_status": data.answer_status,
            "claims": claims,
            "caveats": data.conflicts_or_caveats,
            "missing": data.missing_information,
        },
        coverage,
        report,
        result.model,
        QA_VERSION,
    )
    return {"output_id": out_id, "markdown": md, "coverage": coverage, "citation_report": report}


# ---- Exhaustive mode -----------------------------------------------------------------------------


def doc_request(ctx: AppContext, project_id: str, doc: Document, question: str) -> tuple[AIRequest, str]:
    pages = readable_pages(ctx, doc.id)
    sections = split_sections(pages, ctx.settings.ai_section_token_budget)
    if len(sections) <= 1:
        coverage = "full_text"
        note = "You are given the complete extracted text of this paper. " + coverage_note(doc)
        xml = format_pages(doc.label, doc.filename, pages, coverage_note(doc))
    else:
        coverage = "relevant_passages"
        hits = search(ctx, project_id, question, doc_ids=[doc.id], limit=20)
        wanted = {h.page_no for h in hits} | {pages[0]["page_no"]}
        chosen = [p for p in pages if p["page_no"] in wanted]
        budget = ctx.settings.ai_section_token_budget
        trimmed, used = [], 0
        for p in chosen:
            t = estimate_tokens(p["text"])
            if used + t > budget:
                break
            trimmed.append(p)
            used += t
        note = (
            f"This paper is long; you are given {len(trimmed)} of its {len(pages)} readable pages "
            "selected by keyword relevance (plus the first page). Say if the answer may lie elsewhere."
        )
        xml = format_pages(doc.label, doc.filename, trimmed, coverage_note(doc))
    req = AIRequest(
        purpose=f"qa_doc:{doc.label}",
        system=SYSTEM_RULES,
        user=qa_doc_prompt(question, xml, note),
        output_model=DocAnswer,
        prompt_version=QA_DOC_VERSION,
        expected_output_tokens=1500,
    )
    return req, coverage


def run_doc_question(ai: AIService, project_id: str, doc: Document, question: str, run_id: str) -> dict:
    req, coverage = doc_request(ai.ctx, project_id, doc, question)
    result = ai.call(project_id, req, run_id=run_id)
    data: DocAnswer = result.data  # type: ignore[assignment]
    claims = check_claims(data.claims, page_lookup(ai.ctx, [doc]))
    return {
        "doc_label": doc.label,
        "relevance": data.relevance,
        "coverage": coverage,
        "claims": claims,
        "notes": data.notes,
    }


def finalize_exhaustive(ai: AIService, project_id: str, run: dict) -> dict:
    ctx = ai.ctx
    question = run["params"]["question"]
    rows = ctx.db.query(
        "SELECT j.status, j.error, j.result, d.label FROM jobs j JOIN documents d ON d.id = j.doc_id"
        " WHERE j.run_id = ? AND j.kind = ? ORDER BY d.label",
        (run["id"], jobs.JOB_AI_QA_DOC),
    )
    coverage_rows = []
    findings = []
    for r in rows:
        res = json.loads(r["result"]) if r["result"] else None
        if r["status"] != "done" or res is None:
            coverage_rows.append(
                {
                    "doc_label": r["label"],
                    "status": f"not processed ({r['status']})",
                    "detail": r["error"] or "",
                }
            )
            continue
        supported = [c for c in res["claims"] if c["supported"]]
        status = (
            res["relevance"]
            if res["relevance"] != "relevant" or supported
            else "relevant (no verified citations)"
        )
        coverage_rows.append({"doc_label": r["label"], "status": status, "detail": res["coverage"]})
        if supported:
            findings.append(
                {
                    "doc_label": r["label"],
                    "claims": [
                        {
                            "text": c["text"],
                            "kind": c["kind"],
                            "citations": [ci for ci in c["citations"] if ci["status"] == "verified"],
                        }
                        for c in supported
                    ],
                }
            )
    excluded = run["params"].get("excluded", [])
    processed = sum(1 for c in coverage_rows if not c["status"].startswith("not processed"))
    coverage_text = (
        f"Exhaustive mode: {processed} of {len(coverage_rows)} eligible documents examined individually; "
        f"{len(findings)} contained relevant, verified evidence."
    )
    if excluded:
        coverage_text += f" {len(excluded)} document(s) were not eligible: " + ", ".join(excluded) + "."
    coverage = {"mode": "exhaustive", "rows": coverage_rows, "excluded": excluded, "text": coverage_text}

    if not findings:
        md = render_answer(
            question,
            "not_answerable_from_sources",
            [],
            [],
            ["No document contained verified evidence for this question."],
            coverage_text,
        )
        out_id = _save_output(
            ctx,
            project_id,
            "qa",
            question,
            run["params"],
            md,
            {"claims": []},
            coverage,
            {},
            None,
            QA_REDUCE_VERSION,
        )
        return {"output_id": out_id}

    findings_json = dumps(findings)
    budget = ctx.settings.ai_synthesis_token_budget
    groups = [findings]
    if estimate_tokens(findings_json) > budget:
        groups, cur, used = [], [], 0
        for f in findings:
            t = estimate_tokens(dumps(f))
            if cur and used + t > budget:
                groups.append(cur)
                cur, used = [], 0
            cur.append(f)
            used += t
        groups.append(cur)
    all_claims: list[dict] = []
    caveats: list[str] = []
    missing: list[str] = []
    statuses = []
    model = None
    docs = eligible_documents(ctx, project_id)
    lookup = page_lookup(ctx, [d for d in docs if d.label in {f["doc_label"] for f in findings}])
    for i, group in enumerate(groups, start=1):
        note = (
            coverage_text
            if len(groups) == 1
            else f"{coverage_text} This is group {i} of {len(groups)} of the findings."
        )
        req = AIRequest(
            purpose=f"qa_reduce:{i}",
            system=SYSTEM_RULES,
            user=qa_reduce_prompt(question, dumps(group), note),
            output_model=AnswerOutput,
            prompt_version=QA_REDUCE_VERSION,
            expected_output_tokens=4000,
        )
        result = ai.call(project_id, req, run_id=run["id"])
        data: AnswerOutput = result.data  # type: ignore[assignment]
        model = result.model
        all_claims += check_claims(data.claims, lookup)
        caveats += data.conflicts_or_caveats
        missing += data.missing_information
        statuses.append(data.answer_status)
    status = "answered" if all(s == "answered" for s in statuses) else "partially_answered"
    md = render_answer(question, status, all_claims, caveats, missing, coverage_text)
    md += "\n\n**Per-document coverage**\n\n| Document | Outcome | Text used |\n|---|---|---|\n"
    md += "\n".join(f"| {c['doc_label']} | {c['status']} | {c['detail']} |" for c in coverage_rows)
    report = _citation_summary(all_claims)
    out_id = _save_output(
        ctx,
        project_id,
        "qa",
        question,
        run["params"],
        md,
        {
            "answer_status": status,
            "claims": all_claims,
            "caveats": caveats,
            "missing": missing,
            "per_document": findings,
        },
        coverage,
        report,
        model,
        QA_REDUCE_VERSION,
    )
    return {"output_id": out_id, "groups": len(groups)}
