"""Helpers shared by the analysis features."""

from __future__ import annotations

import json

from litreview import matrix
from litreview.ai.client import estimate_tokens
from litreview.ai.prompts import Claim
from litreview.citations import DocPages, format_citation, verify_quote
from litreview.context import AppContext
from litreview.documents import Document, get_pages
from litreview.pdf_extract import READABLE_QUALITIES

KIND_LABEL = {
    "reported": "",
    "reported_result": "",
    "author_interpretation": " *(authors' interpretation)*",
    "synthesis": " *(tool synthesis - verify)*",
}


def readable_pages(ctx: AppContext, doc_id: str) -> list[dict]:
    return [p for p in get_pages(ctx, doc_id) if p["quality"] in READABLE_QUALITIES and p["text"].strip()]


def coverage_note(doc: Document) -> str:
    if not doc.page_count:
        return "No pages were extracted."
    parts = [f"{doc.label}: {doc.coverage_note}."]
    if doc.unreadable_pages:
        parts.append(
            f"{doc.unreadable_pages} page(s) are unreadable and were not supplied; information on "
            "them must be reported as 'Not found in extracted text'."
        )
    if doc.low_confidence_pages:
        parts.append(f"{doc.low_confidence_pages} page(s) have low-confidence text (OCR).")
    return " ".join(parts)


def split_sections(pages: list[dict], budget_tokens: int) -> list[list[dict]]:
    """Group consecutive pages so each group stays within the token budget."""
    sections: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for page in pages:
        tokens = estimate_tokens(page["text"]) + 20
        if current and used + tokens > budget_tokens:
            sections.append(current)
            current, used = [], 0
        if tokens > budget_tokens:
            # A single enormous page: split its text (rare; e.g. a giant table page).
            text = page["text"]
            step = int(budget_tokens * 3.2)
            for i in range(0, len(text), step):
                sections.append([{**page, "text": text[i : i + step]}])
            continue
        current.append(page)
        used += tokens
    if current:
        sections.append(current)
    return sections


def page_lookup(ctx: AppContext, docs: list[Document]) -> dict[str, DocPages]:
    out: dict[str, DocPages] = {}
    for doc in docs:
        pages = get_pages(ctx, doc.id)
        out[doc.label] = DocPages(
            page_count=doc.page_count or 0,
            readable={p["page_no"] for p in pages if p["quality"] in READABLE_QUALITIES},
            texts={p["page_no"]: p["text"] for p in pages},
        )
    return out


def check_claims(claims: list[Claim], lookup: dict[str, DocPages]) -> list[dict]:
    """Validate each citation of each claim; return JSON-able claims with verification flags."""
    out = []
    for claim in claims:
        cites = []
        for c in claim.citations:
            label = c.doc_label.strip().upper()
            doc = lookup.get(label)
            if doc is None:
                status = "invalid: document not in scope"
            elif not 1 <= c.page <= doc.page_count:
                status = "invalid: page out of range"
            elif c.page not in doc.readable:
                status = "invalid: page unreadable"
            elif verify_quote(doc.texts.get(c.page, ""), c.quote):
                status = "verified"
            else:
                status = "quote not found on page"
            cites.append({"doc_label": label, "page": c.page, "quote": c.quote, "status": status})
        verified = any(ci["status"] == "verified" for ci in cites)
        out.append({"text": claim.text, "kind": claim.kind, "citations": cites, "supported": verified})
    return out


def render_claims(claims: list[dict]) -> str:
    lines = []
    for claim in claims:
        good = [c for c in claim["citations"] if c["status"] == "verified"]
        cite_txt = " ".join(format_citation(c["doc_label"], [c["page"]]) for c in good)
        flag = "" if claim["supported"] else " **[citation could not be verified - check source]**"
        lines.append(f"- {claim['text']}{KIND_LABEL.get(claim['kind'], '')} {cite_txt}{flag}".rstrip())
    return "\n".join(lines)


def matrix_records(ctx: AppContext, project_id: str, docs: list[Document]) -> tuple[list[dict], list[str]]:
    """Compact evidence records for synthesis/drafting. Returns (records, labels_without_extraction)."""
    fields = matrix.list_fields(ctx, project_id)
    records, missing = [], []
    for doc in docs:
        cells = {c.field_key: c for c in matrix.get_cells(ctx, project_id, doc.id)}
        if not any(c.source in ("ai", "researcher") for c in cells.values()):
            missing.append(doc.label)
            continue
        rec: dict = {
            "doc_label": doc.label,
            "study_group": doc.study_group or doc.label,
            "coverage": doc.coverage_note,
        }
        for f in fields:
            cell = cells.get(f.key)
            if cell is None:
                continue
            pages = sorted({e.get("page") for e in cell.evidence if e.get("page")})
            rec[f.key] = {"value": matrix.display_value(cell), "status": cell.status, "pages": pages}
        ev = ctx.db.query(
            "SELECT category, statement, evidence_type, page_no, quote, quote_verified FROM evidence"
            " WHERE project_id = ? AND doc_id = ? ORDER BY id",
            (project_id, doc.id),
        )
        rec["evidence"] = [
            {
                "category": e["category"],
                "statement": e["statement"],
                "type": e["evidence_type"],
                "page": e["page_no"],
                "quote": e["quote"] if e["quote_verified"] else None,
            }
            for e in ev
        ]
        records.append(rec)
    return records, missing


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
