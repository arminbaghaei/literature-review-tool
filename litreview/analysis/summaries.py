"""Editable per-paper summaries with page citations.

Short papers are summarised from their full extracted text. Papers longer than one section are
summarised from their source-linked evidence records (run matrix extraction first), so a long paper
is never silently truncated.
"""

from __future__ import annotations

import json

from litreview.ai.client import AIRequest, AIService
from litreview.ai.prompts import SUMMARY_VERSION, SYSTEM_RULES, SummaryOutput, format_pages, summary_prompt
from litreview.analysis.common import (
    KIND_LABEL,
    coverage_note,
    dumps,
    matrix_records,
    readable_pages,
    split_sections,
)
from litreview.citations import format_citation
from litreview.context import AppContext
from litreview.db import now
from litreview.documents import Document


class SummaryError(Exception):
    pass


def build_request(ctx: AppContext, project_id: str, doc: Document) -> AIRequest:
    pages = readable_pages(ctx, doc.id)
    if not pages:
        raise SummaryError(f"{doc.label} has no readable text")
    sections = split_sections(pages, ctx.settings.ai_section_token_budget)
    if len(sections) == 1:
        source = format_pages(doc.label, doc.filename, pages, coverage_note(doc))
        kind = "complete extracted text"
    else:
        records, missing = matrix_records(ctx, project_id, [doc])
        if missing:
            raise SummaryError(
                f"{doc.label} is too long to summarise in one request. Run matrix extraction for it "
                "first; the summary is then built from its page-linked evidence."
            )
        source = "<evidence_record>\n" + dumps(records[0]) + "\n</evidence_record>"
        kind = "page-linked evidence record extracted section by section from the full paper"
    return AIRequest(
        purpose=f"summary:{doc.label}",
        system=SYSTEM_RULES,
        user=summary_prompt(doc.label, source, kind),
        output_model=SummaryOutput,
        prompt_version=SUMMARY_VERSION,
        expected_output_tokens=2500,
    )


def render_summary(label: str, out: SummaryOutput, page_count: int, readable: set[int]) -> tuple[str, dict]:
    parts = []
    problems = []
    for s in out.sentences:
        good = [p for p in s.pages if 1 <= p <= page_count and p in readable]
        bad = [p for p in s.pages if p not in good]
        if bad:
            problems.append({"sentence": s.text[:120], "invalid_pages": bad})
        cite = format_citation(label, good) if good else "**[no valid page citation - verify]**"
        parts.append(f"{s.text}{KIND_LABEL.get(s.kind, '')} {cite}")
    report = {"sentences": len(out.sentences), "invalid_page_refs": problems}
    return " ".join(parts), report


def generate_summary(ai: AIService, project_id: str, doc: Document, run_id: str | None = None) -> dict:
    ctx = ai.ctx
    req = build_request(ctx, project_id, doc)
    result = ai.call(project_id, req, run_id=run_id)
    readable = {p["page_no"] for p in readable_pages(ctx, doc.id)}
    text, report = render_summary(doc.label, result.data, doc.page_count or 0, readable)  # type: ignore[arg-type]
    text = "*AI-generated draft - check against the source.* " + text
    with ctx.db.transaction() as c:
        row = c.execute(
            "SELECT source FROM summaries WHERE project_id = ? AND doc_id = ?", (project_id, doc.id)
        ).fetchone()
        if row and row["source"] == "researcher":
            c.execute(
                "UPDATE summaries SET ai_suggestion = ?, updated_at = ? WHERE project_id = ? AND doc_id = ?",
                (text, now(), project_id, doc.id),
            )
            outcome = "suggested"
        else:
            c.execute(
                "INSERT OR REPLACE INTO summaries(project_id, doc_id, text, source, citation_report,"
                " model, prompt_version, updated_at) VALUES (?, ?, ?, 'ai', ?, ?, ?, ?)",
                (project_id, doc.id, text, json.dumps(report), result.model, SUMMARY_VERSION, now()),
            )
            outcome = "written"
    return {"status": "done", "outcome": outcome, **report}


def get_summary(ctx: AppContext, project_id: str, doc_id: str) -> dict | None:
    row = ctx.db.query_one(
        "SELECT * FROM summaries WHERE project_id = ? AND doc_id = ?", (project_id, doc_id)
    )
    return dict(row) if row else None


def save_researcher_summary(ctx: AppContext, project_id: str, doc_id: str, text: str) -> None:
    with ctx.db.transaction() as c:
        row = c.execute(
            "SELECT text FROM summaries WHERE project_id = ? AND doc_id = ?", (project_id, doc_id)
        ).fetchone()
        old = row["text"] if row else None
        if old == text:
            return
        c.execute(
            "INSERT INTO summaries(project_id, doc_id, text, source, updated_at) VALUES (?, ?, ?, 'researcher', ?)"
            " ON CONFLICT(project_id, doc_id) DO UPDATE SET text = excluded.text, source = 'researcher',"
            " updated_at = excluded.updated_at",
            (project_id, doc_id, text, now()),
        )
        c.execute(
            "INSERT INTO edit_history(project_id, entity, entity_id, field, old_value, new_value, changed_by,"
            " changed_at) VALUES (?, 'summary', ?, 'text', ?, ?, 'researcher', ?)",
            (project_id, doc_id, old, text, now()),
        )
