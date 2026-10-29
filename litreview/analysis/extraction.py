"""Per-paper structured extraction into the literature review matrix.

Long papers are processed in bounded sections; section results are merged deterministically so
every value keeps its own page-level evidence. Quotations are verified against stored page text.
A reported value without any verified quotation becomes ``requires_verification``.
"""

from __future__ import annotations

from litreview import matrix
from litreview.ai.client import AIRequest, AIService
from litreview.ai.prompts import (
    EXTRACT_VERSION,
    SYSTEM_RULES,
    ExtractionOutput,
    extraction_prompt,
    format_pages,
)
from litreview.analysis.common import coverage_note, readable_pages, split_sections
from litreview.citations import verify_quote
from litreview.context import AppContext
from litreview.db import now
from litreview.documents import Document

MULTI_VALUE_FIELDS = {"findings", "limitations", "measures", "methods", "population"}


def build_requests(ctx: AppContext, project_id: str, doc: Document) -> list[AIRequest]:
    project = ctx.db.query_one("SELECT review_question FROM projects WHERE id = ?", (project_id,))
    fields = [(f.key, f.label, f.description) for f in matrix.list_fields(ctx, project_id)]
    pages = readable_pages(ctx, doc.id)
    sections = split_sections(pages, ctx.settings.ai_section_token_budget)
    requests = []
    for i, section in enumerate(sections, start=1):
        if len(sections) > 1:
            note = (
                f"This is section {i} of {len(sections)} of the paper (pages {section[0]['page_no']}-"
                f"{section[-1]['page_no']}). Use 'not_reported' for fields not covered in this section."
            )
        else:
            note = "This is the complete extracted text of the paper."
        xml = format_pages(doc.label, doc.filename, section, coverage_note(doc))
        requests.append(
            AIRequest(
                purpose=f"extract:{doc.label}:s{i}",
                system=SYSTEM_RULES,
                user=extraction_prompt(project["review_question"] if project else "", fields, xml, note),
                output_model=ExtractionOutput,
                prompt_version=EXTRACT_VERSION,
                expected_output_tokens=5000,
            )
        )
    return requests


def extract_document(ai: AIService, project_id: str, doc: Document, run_id: str | None = None) -> dict:
    ctx = ai.ctx
    requests = build_requests(ctx, project_id, doc)
    if not requests:
        return {"status": "skipped", "reason": "No readable text"}
    page_text = {p["page_no"]: p["text"] for p in readable_pages(ctx, doc.id)}
    outputs: list[ExtractionOutput] = []
    for req in requests:
        outputs.append(ai.call(project_id, req, run_id=run_id).data)  # type: ignore[arg-type]
    return apply_extraction(ctx, project_id, doc, outputs, page_text, ai.model)


def _verify(evidence: list, page_text: dict[int, str]) -> list[dict]:
    out = []
    for q in evidence:
        verified = q.page in page_text and verify_quote(page_text[q.page], q.quote)
        out.append({"page": q.page, "quote": q.quote, "verified": verified})
    return out


def apply_extraction(
    ctx: AppContext,
    project_id: str,
    doc: Document,
    outputs: list[ExtractionOutput],
    page_text: dict[int, str],
    model: str,
) -> dict:
    fields = matrix.list_fields(ctx, project_id)
    written = suggested = 0
    for f in fields:
        candidates = []
        statuses = []
        for out in outputs:
            for fv in out.fields:
                if fv.key == f.key:
                    statuses.append(fv.status)
                    if fv.status in ("reported", "unclear") and fv.value.strip():
                        candidates.append(fv)
        if candidates:
            chosen = candidates if f.key in MULTI_VALUE_FIELDS else candidates[:1]
            values = list(dict.fromkeys(c.value.strip() for c in chosen))
            value = "; ".join(values)
            evidence = [e for c in chosen for e in _verify(c.evidence, page_text)]
            any_verified = any(e["verified"] for e in evidence)
            if all(c.status == "reported" for c in chosen) and any_verified:
                status = "reported"
            else:
                status = "requires_verification"
        else:
            value, evidence = "", []
            status = "not_found" if (doc.unreadable_pages or not statuses) else "not_reported"
        result = matrix.machine_write(
            ctx,
            project_id,
            doc.id,
            f.key,
            value,
            status,
            "ai",
            evidence,
            model=model,
            prompt_version=EXTRACT_VERSION,
        )
        written += result == "written"
        suggested += result == "suggested"

    with ctx.db.transaction() as c:
        c.execute(
            "DELETE FROM evidence WHERE project_id = ? AND doc_id = ? AND source = 'ai' AND reviewed = 0",
            (project_id, doc.id),
        )
        n_items = 0
        for out in outputs:
            for item in out.evidence_items:
                verified = item.page in page_text and verify_quote(page_text[item.page], item.quote)
                c.execute(
                    "INSERT INTO evidence(project_id, doc_id, category, statement, evidence_type, page_no,"
                    " quote, quote_verified, source, model, prompt_version, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?, ?)",
                    (
                        project_id,
                        doc.id,
                        item.category,
                        item.statement,
                        item.evidence_type,
                        item.page,
                        item.quote,
                        int(verified),
                        model,
                        EXTRACT_VERSION,
                        now(),
                    ),
                )
                n_items += 1
    return {
        "status": "done",
        "sections": len(outputs),
        "fields_written": written,
        "fields_suggested": suggested,
        "evidence_items": n_items,
    }
