"""Page-by-page PDF text extraction with OCR fallback.

Runs inside a worker child process (one document per process) so a crashing or hanging parser
cannot take down the worker. Pages are committed incrementally: if the process is interrupted,
the next attempt resumes after the last committed page and never duplicates page rows.

Page quality values
-------------------
``text``            embedded text looks usable
``ocr``             OCR text with confidence at or above the threshold
``ocr_low_conf``    OCR text below the confidence threshold: requires verification
``low_text``        sparse or garbled embedded text kept because OCR could not improve it
``blank``           the rendered page is visually empty
``unreadable``      no usable text (OCR unavailable or failed)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from litreview import jobs
from litreview.chunking import chunk_pages
from litreview.config import CHUNKER_VERSION, EXTRACTION_VERSION, OCR_VERSION, Settings
from litreview.context import AppContext
from litreview.db import now
from litreview.metadata import heuristic_metadata, is_probable_duplicate
from litreview.ocr import OcrEngine, get_engine

log = logging.getLogger(__name__)

MAX_RENDER_PIXELS = 4500 * 4500
COMMIT_EVERY_PAGES = 5
READABLE_QUALITIES = ("text", "ocr", "ocr_low_conf", "low_text")


class ExtractionCancelled(Exception):
    pass


@dataclass
class PageResult:
    page_no: int
    label: str | None
    method: str
    quality: str
    text: str
    ocr_confidence: float | None
    flags: list[str]


def _text_stats(text: str) -> tuple[int, float, float]:
    """Return (non-space chars, bad-char ratio, digit ratio)."""
    chars = [ch for ch in text if not ch.isspace()]
    n = len(chars)
    if n == 0:
        return 0, 0.0, 0.0
    bad = sum(1 for ch in chars if ch == "�" or 0xE000 <= ord(ch) <= 0xF8FF or ord(ch) < 32)
    digits = sum(1 for ch in chars if ch.isdigit())
    return n, bad / n, digits / n


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = text.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _content_flags(text: str, digit_ratio: float) -> list[str]:
    flags = []
    if digit_ratio > 0.12 or re.search(r"\bTable\s+\d", text):
        flags.append("tables_or_statistics")
    if re.search(r"[=±≤≥∑∫√]|\bp\s*[<=>]\s*0?\.\d", text):
        flags.append("equations_or_stat_values")
    return flags


def process_page(page, page_no: int, label: str | None, settings: Settings, ocr: OcrEngine) -> PageResult:
    textpage = page.get_textpage()
    try:
        raw = textpage.get_text_range()
    finally:
        textpage.close()
    text = clean_text(raw)
    n, bad_ratio, digit_ratio = _text_stats(text)
    needs_ocr = n < settings.min_chars_per_page or bad_ratio > 0.10
    flags: list[str] = []
    if not needs_ocr:
        return PageResult(page_no, label, "text", "text", text, None, _content_flags(text, digit_ratio))

    if not ocr.available:
        flags.append("ocr_unavailable")
        quality = "low_text" if n > 0 else "unreadable"
        return PageResult(page_no, label, "text", quality, text, None, flags)

    width, height = page.get_size()
    scale = settings.ocr_dpi / 72
    if width * height * scale * scale > MAX_RENDER_PIXELS:
        scale = (MAX_RENDER_PIXELS / (width * height)) ** 0.5
    bitmap = page.render(scale=scale)
    try:
        image = bitmap.to_pil()
    finally:
        bitmap.close()
    grey = image.convert("L")
    extrema = grey.getextrema()
    if extrema[1] - extrema[0] < 16:
        return PageResult(page_no, label, "text", "blank" if n == 0 else "low_text", text, None, flags)
    try:
        result = ocr.recognise(image)
    except Exception as exc:  # OCR failure on one page must not fail the document
        log.warning("OCR failed on page %s: %s", page_no, type(exc).__name__)
        flags.append("ocr_failed")
        return PageResult(page_no, label, "text", "low_text" if n else "unreadable", text, None, flags)
    finally:
        image.close()
        grey.close()
    ocr_text = clean_text(result.text)
    ocr_n, _, ocr_digits = _text_stats(ocr_text)
    if ocr_n >= max(n, 1) and ocr_n > 0:
        quality = "ocr" if result.confidence >= settings.ocr_min_confidence else "ocr_low_conf"
        flags += _content_flags(ocr_text, ocr_digits)
        flags.append("ocr")
        return PageResult(page_no, label, "ocr", quality, ocr_text, result.confidence, flags)
    if n > 0:
        return PageResult(page_no, label, "text", "low_text", text, None, flags)
    return PageResult(page_no, label, "ocr", "unreadable", "", result.confidence, flags)


def _set_doc(ctx: AppContext, doc_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    ctx.db.execute(
        f"UPDATE documents SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), now(), doc_id)
    )


def _classify_open_error(exc: Exception) -> str:
    msg = str(exc)
    if "password" in msg.lower():
        return "Encrypted / password-protected PDF: cannot be read without the password"
    if "format" in msg.lower() or "file" in msg.lower():
        return f"Corrupted or unsupported PDF ({msg})"
    return f"PDF could not be opened ({msg})"


def extract_document(ctx: AppContext, project_id: str, doc_id: str, job_id: int | None) -> str:
    """Extract one document. Returns the final document status."""
    import pypdfium2 as pdfium

    settings = ctx.settings
    pdf_path = ctx.files.original_path(project_id, doc_id)
    if not pdf_path.exists():
        _set_doc(ctx, doc_id, status="failed", error="Original file missing from storage")
        return "failed"
    _set_doc(ctx, doc_id, status="processing", error=None)

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except pdfium.PdfiumError as exc:
        _set_doc(ctx, doc_id, status="failed", error=_classify_open_error(exc))
        return "failed"

    try:
        page_count = len(pdf)
        if page_count == 0:
            _set_doc(ctx, doc_id, status="failed", error="PDF has no pages", page_count=0)
            return "failed"
        if page_count > settings.max_pages_per_document:
            _set_doc(
                ctx,
                doc_id,
                status="failed",
                page_count=page_count,
                error=f"PDF has {page_count} pages; the limit is {settings.max_pages_per_document}",
            )
            return "failed"
        try:
            meta = {k: v for k, v in pdf.get_metadata_dict(skip_empty=True).items()}
        except Exception:
            meta = {}
        version = f"{EXTRACTION_VERSION}+{OCR_VERSION if settings.ocr_enabled else 'no-ocr'}"
        _set_doc(
            ctx, doc_id, page_count=page_count, pdf_metadata=json.dumps(meta), extraction_version=version
        )

        # Resume support: keep pages already extracted with the same pipeline version.
        with ctx.db.transaction() as c:
            c.execute("DELETE FROM pages WHERE doc_id = ? AND extraction_version != ?", (doc_id, version))
            row = c.execute(
                "SELECT COUNT(*) AS n, MAX(page_no) AS last FROM pages WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        start = (row["last"] or 0) + 1 if row["n"] == (row["last"] or 0) else 1
        if start == 1:
            ctx.db.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))

        ocr = get_engine(settings.ocr_enabled)
        pending: list[PageResult] = []
        for page_no in range(start, page_count + 1):
            page = pdf[page_no - 1]
            try:
                try:
                    label = pdf.get_page_label(page_no - 1) or None
                except Exception:
                    label = None
                result = process_page(page, page_no, label, settings, ocr)
            except Exception as exc:  # a single bad page is recorded, not fatal
                log.warning("Page %s failed: %s", page_no, type(exc).__name__)
                result = PageResult(page_no, None, "none", "unreadable", "", None, ["page_error"])
            finally:
                page.close()
            pending.append(result)
            if len(pending) >= COMMIT_EVERY_PAGES or page_no == page_count:
                _commit_pages(ctx, doc_id, version, pending)
                pending = []
                _set_doc(ctx, doc_id, pages_done=page_no)
                if job_id is not None:
                    status = jobs.heartbeat(ctx, job_id, page_no, page_count)
                    if status == "cancel_requested":
                        raise ExtractionCancelled()
    except ExtractionCancelled:
        _set_doc(ctx, doc_id, status="cancelled", error="Cancelled; completed pages are kept")
        return "cancelled"
    finally:
        pdf.close()

    return finalise_document(ctx, project_id, doc_id)


def _commit_pages(ctx: AppContext, doc_id: str, version: str, results: list[PageResult]) -> None:
    with ctx.db.transaction() as c:
        for r in results:
            c.execute(
                "INSERT OR REPLACE INTO pages(doc_id, page_no, page_label, method, quality,"
                " ocr_confidence, flags, char_count, extraction_version, text)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    r.page_no,
                    r.label,
                    r.method,
                    r.quality,
                    r.ocr_confidence,
                    ",".join(sorted(set(r.flags))),
                    len(r.text),
                    version,
                    r.text,
                ),
            )


def finalise_document(ctx: AppContext, project_id: str, doc_id: str) -> str:
    """Build chunks, coverage statistics, heuristic metadata and duplicate flags."""
    rows = ctx.db.query(
        "SELECT page_no, quality, flags, text FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,)
    )
    pages = [(r["page_no"], r["text"]) for r in rows if r["quality"] in READABLE_QUALITIES]
    chunks = chunk_pages(pages)
    with ctx.db.transaction() as c:
        c.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        for ch in chunks:
            c.execute(
                "INSERT INTO chunks(project_id, doc_id, page_no, seq, section, text, chunker_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, doc_id, ch.page_no, ch.seq, ch.section, ch.text, CHUNKER_VERSION),
            )
    counts = {q: 0 for q in ("text", "ocr", "ocr_low_conf", "low_text", "blank", "unreadable")}
    flagged = 0
    for r in rows:
        counts[r["quality"]] = counts.get(r["quality"], 0) + 1
        if "tables_or_statistics" in r["flags"] or "equations_or_stat_values" in r["flags"]:
            flagged += 1
    unreadable = counts["unreadable"]
    low = counts["ocr_low_conf"] + counts["low_text"]
    if not pages:
        status, error = "failed", "No readable text on any page (scanned without usable OCR?)"
    elif unreadable or low:
        status = "partial"
        error = f"{unreadable} unreadable page(s), {low} low-confidence page(s): verify these pages"
    else:
        status, error = "completed", None
    _set_doc(
        ctx,
        doc_id,
        status=status,
        error=error,
        text_pages=counts["text"],
        ocr_pages=counts["ocr"] + counts["ocr_low_conf"],
        low_confidence_pages=low,
        unreadable_pages=unreadable,
        flagged_pages=flagged,
    )
    _apply_heuristic_metadata(ctx, project_id, doc_id, pages)
    return status


def _apply_heuristic_metadata(ctx: AppContext, project_id: str, doc_id: str, pages) -> None:
    from litreview import matrix

    row = ctx.db.query_one("SELECT pdf_metadata FROM documents WHERE id = ?", (doc_id,))
    meta = json.loads(row["pdf_metadata"] or "{}") if row else {}
    found = heuristic_metadata(meta, pages)
    for key, info in found.items():
        evidence = [{"page": info["page"], "quote": None, "basis": info["basis"], "verified": False}]
        matrix.machine_write(ctx, project_id, doc_id, key, info["value"], "heuristic", "heuristic", evidence)
    flag_probable_duplicates(ctx, project_id, doc_id)


def _doc_meta(ctx: AppContext, project_id: str, doc_id: str) -> dict:
    rows = ctx.db.query(
        "SELECT field_key, value FROM matrix_cells WHERE project_id = ? AND doc_id = ? AND"
        " field_key IN ('title', 'doi', 'year')",
        (project_id, doc_id),
    )
    return {r["field_key"]: r["value"] for r in rows}


def flag_probable_duplicates(ctx: AppContext, project_id: str, doc_id: str) -> None:
    """Flag probable duplicate publications for human review (never merged automatically)."""
    mine = _doc_meta(ctx, project_id, doc_id)
    if not mine:
        return
    others = ctx.db.query(
        "SELECT id, label FROM documents WHERE project_id = ? AND id != ? AND status IN"
        " ('completed', 'partial')",
        (project_id, doc_id),
    )
    for other in others:
        reason = is_probable_duplicate(mine, _doc_meta(ctx, project_id, other["id"]))
        if reason:
            ctx.db.execute(
                "UPDATE documents SET probable_duplicate_of = ?, duplicate_review = 'pending'"
                " WHERE id = ? AND duplicate_review IN ('none', 'pending')",
                (f"{other['label']}: {reason}", doc_id),
            )
            return


def run_extraction_child(settings: Settings, project_id: str, doc_id: str, job_id: int) -> None:
    """Entry point for the per-document child process."""
    from litreview.context import create_context

    logging.basicConfig(level=logging.WARNING)
    ctx = create_context(settings=settings)
    try:
        status = extract_document(ctx, project_id, doc_id, job_id)
    except Exception as exc:
        log.exception("Extraction crashed for %s", doc_id)
        _set_doc(ctx, doc_id, status="failed", error=f"Extraction error: {type(exc).__name__}: {exc}")
        jobs.finish(ctx, job_id, "failed", error=f"{type(exc).__name__}: {exc}")
        return
    if status == "cancelled":
        jobs.finish(ctx, job_id, "cancelled", error="Cancelled by user")
    elif status == "failed":
        row = ctx.db.query_one("SELECT error FROM documents WHERE id = ?", (doc_id,))
        jobs.finish(ctx, job_id, "failed", error=row["error"] if row else "failed")
    else:
        jobs.finish(ctx, job_id, "done", result={"status": status})
