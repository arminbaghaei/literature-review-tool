"""Read access to documents, pages and coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass

from litreview import jobs
from litreview.context import AppContext
from litreview.db import now

ELIGIBLE_STATUSES = ("completed", "partial")


@dataclass
class Document:
    id: str
    label: str
    filename: str
    sha256: str
    size_bytes: int
    status: str
    error: str | None
    page_count: int | None
    pages_done: int
    text_pages: int
    ocr_pages: int
    low_confidence_pages: int
    unreadable_pages: int
    flagged_pages: int
    probable_duplicate_of: str | None
    duplicate_review: str
    study_group: str | None
    included: bool
    exclusion_reason: str | None
    pdf_metadata: dict

    @property
    def eligible(self) -> bool:
        return self.status in ELIGIBLE_STATUSES and self.included

    @property
    def coverage_note(self) -> str:
        if not self.page_count:
            return "No pages extracted"
        readable = self.page_count - self.unreadable_pages
        note = f"{readable}/{self.page_count} pages readable"
        if self.ocr_pages:
            note += f", {self.ocr_pages} via OCR"
        if self.low_confidence_pages:
            note += f", {self.low_confidence_pages} low-confidence"
        return note


def _row_to_doc(r) -> Document:
    return Document(
        id=r["id"],
        label=r["label"],
        filename=r["original_filename"],
        sha256=r["sha256"],
        size_bytes=r["size_bytes"],
        status=r["status"],
        error=r["error"],
        page_count=r["page_count"],
        pages_done=r["pages_done"],
        text_pages=r["text_pages"],
        ocr_pages=r["ocr_pages"],
        low_confidence_pages=r["low_confidence_pages"],
        unreadable_pages=r["unreadable_pages"],
        flagged_pages=r["flagged_pages"],
        probable_duplicate_of=r["probable_duplicate_of"],
        duplicate_review=r["duplicate_review"],
        study_group=r["study_group"],
        included=bool(r["included"]),
        exclusion_reason=r["exclusion_reason"],
        pdf_metadata=json.loads(r["pdf_metadata"] or "{}"),
    )


def list_documents(ctx: AppContext, project_id: str) -> list[Document]:
    rows = ctx.db.query("SELECT * FROM documents WHERE project_id = ? ORDER BY label", (project_id,))
    return [_row_to_doc(r) for r in rows]


def get_document(ctx: AppContext, project_id: str, doc_id: str) -> Document | None:
    r = ctx.db.query_one("SELECT * FROM documents WHERE project_id = ? AND id = ?", (project_id, doc_id))
    return _row_to_doc(r) if r else None


def doc_by_label(ctx: AppContext, project_id: str, label: str) -> Document | None:
    r = ctx.db.query_one(
        "SELECT * FROM documents WHERE project_id = ? AND label = ?", (project_id, label.upper())
    )
    return _row_to_doc(r) if r else None


def eligible_documents(ctx: AppContext, project_id: str, doc_ids: list[str] | None = None) -> list[Document]:
    docs = [d for d in list_documents(ctx, project_id) if d.eligible]
    if doc_ids is not None:
        wanted = set(doc_ids)
        docs = [d for d in docs if d.id in wanted]
    return docs


def get_pages(ctx: AppContext, doc_id: str) -> list[dict]:
    rows = ctx.db.query(
        "SELECT page_no, page_label, method, quality, ocr_confidence, flags, char_count, text"
        " FROM pages WHERE doc_id = ? ORDER BY page_no",
        (doc_id,),
    )
    return [dict(r) for r in rows]


def get_page(ctx: AppContext, doc_id: str, page_no: int) -> dict | None:
    r = ctx.db.query_one("SELECT * FROM pages WHERE doc_id = ? AND page_no = ?", (doc_id, page_no))
    return dict(r) if r else None


def status_counts(ctx: AppContext, project_id: str) -> dict[str, int]:
    rows = ctx.db.query(
        "SELECT status, COUNT(*) AS n FROM documents WHERE project_id = ? GROUP BY status",
        (project_id,),
    )
    return {r["status"]: r["n"] for r in rows}


def set_inclusion(ctx: AppContext, project_id: str, doc_id: str, included: bool, reason: str = "") -> None:
    ctx.db.execute(
        "UPDATE documents SET included = ?, exclusion_reason = ?, updated_at = ? WHERE id = ? AND"
        " project_id = ?",
        (
            int(included),
            None if included else (reason.strip() or "Excluded by researcher"),
            now(),
            doc_id,
            project_id,
        ),
    )


def set_duplicate_review(
    ctx: AppContext, project_id: str, doc_id: str, decision: str, study_group: str | None = None
) -> None:
    """Record the researcher's decision on a probable duplicate.

    ``decision``: ``not_duplicate``, ``same_study`` (related report of the same study; kept but
    grouped so it is not counted as independent evidence) or ``duplicate`` (excluded).
    """
    if decision not in ("not_duplicate", "same_study", "duplicate", "pending"):
        raise ValueError("Unknown decision")
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE documents SET duplicate_review = ?, study_group = COALESCE(?, study_group),"
            " updated_at = ? WHERE id = ? AND project_id = ?",
            (decision, study_group, now(), doc_id, project_id),
        )
        if decision == "duplicate":
            c.execute(
                "UPDATE documents SET included = 0, exclusion_reason = 'Duplicate publication'"
                " WHERE id = ? AND project_id = ?",
                (doc_id, project_id),
            )


def set_study_group(ctx: AppContext, project_id: str, doc_id: str, group: str) -> None:
    ctx.db.execute(
        "UPDATE documents SET study_group = ?, updated_at = ? WHERE id = ? AND project_id = ?",
        (group.strip() or None, now(), doc_id, project_id),
    )


def independent_study_count(docs: list[Document]) -> int:
    groups = {d.study_group or d.id for d in docs}
    return len(groups)


def retry_document(ctx: AppContext, project_id: str, doc_id: str) -> None:
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE documents SET status = 'queued', error = NULL, updated_at = ? WHERE id = ? AND"
            " project_id = ?",
            (now(), doc_id, project_id),
        )
        c.execute(
            "UPDATE jobs SET status = 'cancelled' WHERE doc_id = ? AND kind = ? AND status IN ('failed')",
            (doc_id, jobs.JOB_EXTRACT),
        )
        jobs.enqueue(c, project_id, jobs.JOB_EXTRACT, doc_id=doc_id)


def remove_document(ctx: AppContext, project_id: str, doc_id: str) -> None:
    """Remove a document, its derived records and its stored original."""
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE jobs SET status = 'cancelled' WHERE doc_id = ? AND status IN ('queued',"
            " 'running', 'cancel_requested')",
            (doc_id,),
        )
        c.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        c.execute("DELETE FROM documents WHERE id = ? AND project_id = ?", (doc_id, project_id))
    ctx.files.delete_document_file(project_id, doc_id)
