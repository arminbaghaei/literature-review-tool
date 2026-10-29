"""Draft literature review sections with traceable citations, plus the reference list."""

from __future__ import annotations

import json

from litreview import matrix
from litreview.ai.client import AIRequest, AIService, estimate_tokens
from litreview.ai.prompts import DRAFT_VERSION, SYSTEM_RULES, DraftOutput, draft_prompt
from litreview.analysis.common import dumps, matrix_records, page_lookup
from litreview.analysis.qa import _save_output
from litreview.citations import parse_citations, validate_text
from litreview.context import AppContext
from litreview.db import now
from litreview.documents import Document, eligible_documents

VERIFIED_STATUSES = ("reported", "researcher")


def reference_entry(ctx: AppContext, project_id: str, doc: Document) -> str:
    """Build a reference from extracted metadata only. Unverified parts are labelled, never invented."""
    cells = {c.field_key: c for c in matrix.get_cells(ctx, project_id, doc.id)}

    def part(key: str) -> tuple[str, bool]:
        c = cells.get(key)
        if c is None or not c.value:
            return "", False
        return c.value, (c.status in VERIFIED_STATUSES) or c.reviewed

    authors, a_ok = part("authors")
    year, y_ok = part("year")
    title, t_ok = part("title")
    doi, d_ok = part("doi")
    pieces = [
        authors or "[authors not extracted]",
        f"({year})" if year else "(n.d. - year not extracted)",
        title or f"[title not extracted; file: {doc.filename}]",
    ]
    entry = f"**{doc.label}** " + ". ".join(pieces) + "."
    if doi:
        entry += f" https://doi.org/{doi}" if not doi.lower().startswith("http") else f" {doi}"
    unverified = [
        k
        for k, ok, v in (
            ("authors", a_ok, authors),
            ("year", y_ok, year),
            ("title", t_ok, title),
            ("doi", d_ok, doi),
        )
        if v and not ok
    ]
    if unverified or not (authors and year and title):
        entry += " *[metadata requires verification: " + ", ".join(unverified or ["incomplete"]) + "]*"
    return entry


def reference_list(ctx: AppContext, project_id: str, labels: list[str]) -> str:
    docs = {d.label: d for d in eligible_documents(ctx, project_id)}
    lines = [reference_entry(ctx, project_id, docs[label]) for label in sorted(set(labels)) if label in docs]
    return "\n\n".join(lines)


def run_draft(ai: AIService, project_id: str, run: dict) -> dict:
    ctx = ai.ctx
    params = run["params"]
    docs = eligible_documents(ctx, project_id, params.get("doc_ids"))
    records, missing = matrix_records(ctx, project_id, docs)
    if not records:
        raise ValueError("No evidence records. Run matrix extraction first.")
    synthesis_json = "{}"
    if params.get("synthesis_output_id"):
        row = ctx.db.query_one(
            "SELECT content_json FROM outputs WHERE id = ? AND project_id = ?",
            (params["synthesis_output_id"], project_id),
        )
        if row and row["content_json"]:
            synthesis_json = row["content_json"]
    records_json = dumps(records)
    budget = ctx.settings.ai_synthesis_token_budget
    if estimate_tokens(records_json) + estimate_tokens(synthesis_json) > budget:
        raise ValueError(
            f"The evidence for {len(records)} documents exceeds the drafting budget ({budget:,} tokens). "
            "Select fewer documents (e.g. one theme) for this section, or raise LITREVIEW_SYNTHESIS_TOKENS."
        )
    coverage_text = f"Draft based on evidence records of {len(records)} of {len(docs)} eligible documents."
    if missing:
        coverage_text += f" Not covered (no matrix extraction yet): {', '.join(missing)}."
    project = ctx.db.query_one("SELECT review_question FROM projects WHERE id = ?", (project_id,))
    req = AIRequest(
        purpose="draft",
        system=SYSTEM_RULES,
        user=draft_prompt(
            params.get("section_title", "Literature review"),
            params.get("instructions", ""),
            project["review_question"] if project else "",
            records_json,
            synthesis_json,
            coverage_text,
        ),
        output_model=DraftOutput,
        prompt_version=DRAFT_VERSION,
        expected_output_tokens=8000,
    )
    result = ai.call(project_id, req, run_id=run["id"])
    data: DraftOutput = result.data  # type: ignore[assignment]
    covered = [r["doc_label"] for r in records]
    lookup = page_lookup(ctx, [d for d in docs if d.label in covered])
    report = validate_text(data.markdown, lookup).to_dict()
    report["unsupported_points"] = data.unsupported_points
    md = compose_draft_markdown(
        ctx,
        project_id,
        params.get("section_title", "Literature review"),
        data.markdown,
        coverage_text,
        report,
    )
    out_id = _save_output(
        ctx,
        project_id,
        "draft",
        params.get("section_title", "Draft section"),
        params,
        md,
        {"body": data.markdown, "unsupported_points": data.unsupported_points},
        {"covered": covered, "missing_extraction": missing, "text": coverage_text},
        report,
        result.model,
        DRAFT_VERSION,
    )
    return {"output_id": out_id}


def compose_draft_markdown(
    ctx: AppContext, project_id: str, title: str, body: str, coverage_text: str, report: dict
) -> str:
    cited = [r.label for r in parse_citations(body)]
    md = [
        f"## {title}",
        "",
        "> AI-generated draft for researcher editing. Citations use document labels and PDF page numbers;",
        "> check each claim against the source before use.",
        f"> {coverage_text}",
        "",
        body.strip(),
        "",
        "### Sources cited",
        "",
        reference_list(ctx, project_id, cited) or "_No valid citations._",
    ]
    if (
        report.get("invalid")
        or report.get("quotes_unverified")
        or report.get("uncited_paragraphs")
        or report.get("unsupported_points")
    ):
        md += ["", "### Citation check (remove before submission)", ""]
        md += [f"- Invalid citation {i['citation']}: {i['reason']}" for i in report.get("invalid", [])]
        md += [
            f'- Quotation not found on cited page: "{q["quote"][:80]}" {q["citation"]}'
            for q in report.get("quotes_unverified", [])
        ]
        md += [f'- Paragraph without citation: "{p[:80]}..."' for p in report.get("uncited_paragraphs", [])]
        md += [f"- Could not be supported by the evidence: {p}" for p in report.get("unsupported_points", [])]
    return "\n".join(md)


def save_output_edit(ctx: AppContext, project_id: str, output_id: str, new_md: str) -> dict:
    """Save a researcher edit to an output and re-run citation validation on the edited text."""
    row = ctx.db.query_one("SELECT * FROM outputs WHERE id = ? AND project_id = ?", (output_id, project_id))
    if row is None:
        raise ValueError("Output not found")
    docs = eligible_documents(ctx, project_id)
    report = validate_text(
        new_md, page_lookup(ctx, docs), require_paragraph_citations=row["kind"] == "draft"
    ).to_dict()
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE outputs SET content_md = ?, source = 'researcher_edited', citation_report = ?, updated_at = ? WHERE id = ?",
            (new_md, json.dumps(report), now(), output_id),
        )
        c.execute(
            "INSERT INTO edit_history(project_id, entity, entity_id, field, old_value, new_value, changed_by, changed_at)"
            " VALUES (?, 'output', ?, 'content_md', ?, ?, 'researcher', ?)",
            (project_id, output_id, row["content_md"], new_md, now()),
        )
    return report
