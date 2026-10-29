"""Exports: matrix and evidence as CSV/JSON, outputs and drafts as Markdown.

Exports include document IDs, labels, source page locations, processing coverage and review
status. They never include API keys or full document text.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from litreview import matrix
from litreview.context import AppContext
from litreview.documents import list_documents
from litreview.projects import get_project


def _ts() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def matrix_rows(ctx: AppContext, project_id: str) -> list[dict]:
    fields = matrix.list_fields(ctx, project_id)
    rows = []
    for doc in list_documents(ctx, project_id):
        cells = {c.field_key: c for c in matrix.get_cells(ctx, project_id, doc.id)}
        row: dict = {
            "doc_id": doc.id,
            "label": doc.label,
            "filename": doc.filename,
            "sha256": doc.sha256,
            "processing_status": doc.status,
            "included": doc.included,
            "exclusion_reason": doc.exclusion_reason or "",
            "page_count": doc.page_count or 0,
            "pages_ocr": doc.ocr_pages,
            "pages_low_confidence": doc.low_confidence_pages,
            "pages_unreadable": doc.unreadable_pages,
            "coverage": doc.coverage_note,
            "study_group": doc.study_group or "",
            "probable_duplicate_of": doc.probable_duplicate_of or "",
            "duplicate_review": doc.duplicate_review,
        }
        for f in fields:
            cell = cells.get(f.key)
            row[f.key] = matrix.display_value(cell)
            row[f"{f.key}__status"] = cell.status if cell else "not_extracted"
            row[f"{f.key}__source"] = cell.source if cell else ""
            row[f"{f.key}__reviewed"] = bool(cell.reviewed) if cell else False
            pages = sorted({e.get("page") for e in (cell.evidence if cell else []) if e.get("page")})
            row[f"{f.key}__pages"] = ";".join(map(str, pages))
            row[f"{f.key}__evidence"] = (
                [
                    {"page": e.get("page"), "quote": e.get("quote"), "verified": e.get("verified", False)}
                    for e in cell.evidence
                ]
                if cell
                else []
            )
        rows.append(row)
    return rows


def matrix_csv(ctx: AppContext, project_id: str) -> str:
    rows = matrix_rows(ctx, project_id)
    buf = io.StringIO()
    if not rows:
        return ""
    flat = []
    for r in rows:
        flat.append(
            {
                k: (
                    " | ".join(
                        f'p.{e["page"]}: "{e["quote"]}"{"" if e["verified"] else " (unverified)"}'
                        for e in v
                        if e.get("quote")
                    )
                    if k.endswith("__evidence")
                    else v
                )
                for k, v in r.items()
            }
        )
    writer = csv.DictWriter(buf, fieldnames=list(flat[0].keys()))
    writer.writeheader()
    writer.writerows(flat)
    return buf.getvalue()


def evidence_rows(ctx: AppContext, project_id: str) -> list[dict]:
    rows = ctx.db.query(
        "SELECT e.id, d.id AS doc_id, d.label, d.original_filename, e.category, e.statement, e.evidence_type,"
        " e.page_no, e.quote, e.quote_verified, e.source, e.reviewed, e.model, e.prompt_version"
        " FROM evidence e JOIN documents d ON d.id = e.doc_id WHERE e.project_id = ? ORDER BY d.label, e.id",
        (project_id,),
    )
    return [
        {
            "evidence_id": r["id"],
            "doc_id": r["doc_id"],
            "label": r["label"],
            "filename": r["original_filename"],
            "category": r["category"],
            "statement": r["statement"],
            "evidence_type": r["evidence_type"],
            "page": r["page_no"],
            "quote": r["quote"],
            "quote_verified": bool(r["quote_verified"]),
            "source": r["source"],
            "reviewed": bool(r["reviewed"]),
            "model": r["model"],
            "prompt_version": r["prompt_version"],
        }
        for r in rows
    ]


def evidence_csv(ctx: AppContext, project_id: str) -> str:
    rows = evidence_rows(ctx, project_id)
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def project_json(ctx: AppContext, project_id: str) -> str:
    project = get_project(ctx, project_id)
    docs = list_documents(ctx, project_id)
    summaries = {
        r["doc_id"]: {"text": r["text"], "source": r["source"]}
        for r in ctx.db.query(
            "SELECT doc_id, text, source FROM summaries WHERE project_id = ?", (project_id,)
        )
    }
    themes = [
        dict(r)
        for r in ctx.db.query(
            "SELECT t.name, t.description, t.source, d.label, td.pages, td.note FROM themes t"
            " LEFT JOIN theme_docs td ON td.theme_id = t.id LEFT JOIN documents d ON d.id = td.doc_id WHERE t.project_id = ?",
            (project_id,),
        )
    ]
    payload = {
        "exported_at": _ts(),
        "project": {"id": project.id, "name": project.name, "review_question": project.review_question},
        "notice": (
            "Matrix values marked 'heuristic' or 'requires_verification', AI-generated summaries and "
            "synthesis are not verified. Page numbers are PDF page numbers."
        ),
        "coverage": {
            "documents": len(docs),
            "by_status": {s: sum(1 for d in docs if d.status == s) for s in sorted({d.status for d in docs})},
            "eligible": sum(1 for d in docs if d.eligible),
        },
        "fields": [
            {"key": f.key, "label": f.label, "description": f.description}
            for f in matrix.list_fields(ctx, project_id)
        ],
        "matrix": matrix_rows(ctx, project_id),
        "evidence": evidence_rows(ctx, project_id),
        "summaries": {d.label: summaries[d.id] for d in docs if d.id in summaries},
        "themes": themes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def output_markdown(ctx: AppContext, project_id: str, output: dict) -> str:
    project = get_project(ctx, project_id)
    header = [
        f"<!-- Exported from literature review tool: project '{project.name}', output {output['id']}, {_ts()} -->",
        f"<!-- Source: {output['source']}; model: {output.get('model') or 'n/a'}; prompt: {output.get('prompt_version') or 'n/a'} -->",
        "",
    ]
    body = output["content_md"]
    report = output.get("citation_report")
    footer = []
    if isinstance(report, dict) and report:
        footer = [
            "",
            "---",
            "",
            "Citation check at export time:",
            "",
            "```json",
            json.dumps(report, indent=1)[:4000],
            "```",
        ]
    return "\n".join(header) + body + "\n".join(footer) + "\n"
