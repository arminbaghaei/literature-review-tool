"""Upload validation, extraction, OCR, duplicates, recovery and cancellation."""

from __future__ import annotations

import contextlib
import io
import shutil
from dataclasses import replace

from conftest import (
    PAPER_A,
    PAPER_B,
    incoming,
    make_encrypted_pdf,
    make_mixed_pdf,
    make_scanned_pdf,
    make_text_pdf,
)

from litreview import jobs
from litreview.context import create_context
from litreview.documents import get_pages, list_documents, retry_document
from litreview.ingest import IncomingFile, files_from_folder, ingest_batch, safe_display_name
from litreview.pdf_extract import extract_document
from litreview.projects import create_project
from litreview.worker import Worker


def _docs(ctx, project):
    return {d.filename: d for d in list_documents(ctx, project.id)}


def test_mixed_batch_outcomes_are_all_reported(ctx, project, pdf_dir):
    a = make_text_pdf(
        pdf_dir / "a.pdf", PAPER_A, title="Effects of Insulation Retrofits on Indoor Temperature"
    )
    shutil.copy(a, pdf_dir / "a_copy.pdf")
    (pdf_dir / "empty.pdf").write_bytes(b"")
    (pdf_dir / "notpdf.pdf").write_bytes(b"PK\x03\x04 this is a zip")
    report = ingest_batch(ctx, project.id, [incoming(p) for p in sorted(pdf_dir.iterdir())])
    outcomes = {o.filename: o.outcome for o in report.outcomes}
    assert outcomes == {
        "a.pdf": "accepted",
        "a_copy.pdf": "duplicate",
        "empty.pdf": "rejected",
        "notpdf.pdf": "rejected",
    }
    assert report.accepted == 1 and report.duplicates == 1 and report.rejected == 2
    log = ctx.db.query("SELECT outcome FROM ingest_log WHERE project_id = ?", (project.id,))
    assert len(log) == 4  # nothing silently dropped


def test_limits_enforced(tmp_path, pdf_dir):
    from litreview.config import load_settings

    settings = replace(
        load_settings(tmp_path / "d"),
        max_files_per_batch=2,
        max_file_size_mib=1,
        max_batch_size_mib=3,
        max_project_storage_mib=5,
        min_free_disk_mib=1,
    )
    ctx = create_context(settings=settings)
    project = create_project(ctx, "limits")
    files = [make_text_pdf(pdf_dir / f"p{i}.pdf", [f"Paper {i} text " * 20]) for i in range(3)]
    report = ingest_batch(ctx, project.id, [incoming(f) for f in files])
    assert report.batch_error and "limit is 2" in report.batch_error
    assert report.rejected == 3 and not list_documents(ctx, project.id)

    big = IncomingFile(
        "big.pdf", 2 * 1024 * 1024, lambda: io.BytesIO(b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024))
    )
    report = ingest_batch(ctx, project.id, [big])
    assert report.outcomes[0].outcome == "rejected" and "per-file limit" in report.outcomes[0].reason

    # Declared size lies (None): the streamed byte count still enforces the limit.
    liar = IncomingFile("liar.pdf", None, lambda: io.BytesIO(b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)))
    report = ingest_batch(ctx, project.id, [liar])
    assert report.outcomes[0].outcome == "rejected"
    assert not list(ctx.files.staging_dir(project.id).iterdir())  # partial file cleaned up

    over = [IncomingFile(f"x{i}.pdf", 2 * 1024 * 1024 - 10, lambda: io.BytesIO(b"%PDF")) for i in range(2)]
    report = ingest_batch(ctx, project.id, over)
    assert report.batch_error and "MiB" in report.batch_error


def test_safe_names_prevent_traversal(ctx, project, pdf_dir):
    a = make_text_pdf(pdf_dir / "a.pdf", PAPER_A)
    evil = IncomingFile("..\\..\\windows/../evil\x00name.pdf", a.stat().st_size, lambda: a.open("rb"))
    report = ingest_batch(ctx, project.id, [evil])
    out = report.outcomes[0]
    assert out.outcome == "accepted"
    assert out.filename == "evilname.pdf"
    stored = list(ctx.files.originals_dir(project.id).iterdir())
    assert [p.name for p in stored] == [f"{out.doc_id}.pdf"]
    assert safe_display_name("a/b/c.pdf") == "c.pdf"


def test_extraction_of_text_scanned_mixed_encrypted_corrupt(ctx, project, pdf_dir):
    a = make_text_pdf(
        pdf_dir / "text.pdf",
        PAPER_A,
        title="Effects of Insulation Retrofits on Indoor Temperature",
        author="Jane Smith",
    )
    make_scanned_pdf(pdf_dir / "scanned.pdf", [["Scanned study of 42 participants", "Results were mixed"]])
    make_mixed_pdf(pdf_dir / "mixed.pdf", PAPER_B, ["Appendix table", "Savings were 15 percent"])
    make_encrypted_pdf(pdf_dir / "encrypted.pdf", a)
    (pdf_dir / "corrupt.pdf").write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Catalog >> broken")
    ingest_batch(ctx, project.id, [incoming(p) for p in sorted(pdf_dir.iterdir())])
    Worker(ctx).run_until_idle(timeout=300)
    docs = _docs(ctx, project)

    text = docs["text.pdf"]
    assert text.status == "completed" and text.page_count == 3 and text.ocr_pages == 0
    pages = get_pages(ctx, text.id)
    assert [p["page_no"] for p in pages] == [1, 2, 3]
    assert "120 households" in pages[1]["text"]

    scanned = docs["scanned.pdf"]
    assert scanned.status == "completed" and scanned.ocr_pages == 1
    assert "42 participants" in get_pages(ctx, scanned.id)[0]["text"]

    mixed = docs["mixed.pdf"]
    assert mixed.page_count == 3 and mixed.ocr_pages == 1 and mixed.text_pages == 2

    assert docs["encrypted.pdf"].status == "failed" and "password" in docs["encrypted.pdf"].error.lower()
    assert docs["corrupt.pdf"].status == "failed" and docs["corrupt.pdf"].error

    # Heuristic metadata is marked heuristic, never as verified.
    cell = ctx.db.query_one(
        "SELECT value, status FROM matrix_cells WHERE doc_id = ? AND field_key = 'doi'", (text.id,)
    )
    assert cell["value"] == "10.1234/example.2021.001" and cell["status"] == "heuristic"
    # Bad files did not disturb completed work.
    assert ctx.db.query_one("SELECT COUNT(*) AS n FROM chunks WHERE doc_id = ?", (text.id,))["n"] > 0


def test_ocr_unavailable_flags_unreadable_pages(tmp_path, pdf_dir):
    from litreview.config import load_settings

    ctx = create_context(
        settings=replace(load_settings(tmp_path / "d"), ocr_enabled=False, min_free_disk_mib=1)
    )
    project = create_project(ctx, "no-ocr")
    make_mixed_pdf(pdf_dir / "mixed.pdf", PAPER_B, ["Scanned appendix text here"])
    make_scanned_pdf(pdf_dir / "scan.pdf", [["only an image"]])
    ingest_batch(ctx, project.id, [incoming(p) for p in sorted(pdf_dir.iterdir())])
    Worker(ctx).run_until_idle(timeout=120)
    docs = _docs(ctx, project)
    assert docs["mixed.pdf"].status == "partial" and docs["mixed.pdf"].unreadable_pages == 1
    assert "unreadable" in docs["mixed.pdf"].error
    assert docs["scan.pdf"].status == "failed" and "No readable text" in docs["scan.pdf"].error
    page = get_pages(ctx, docs["mixed.pdf"].id)[-1]
    assert page["quality"] == "unreadable" and "ocr_unavailable" in page["flags"]


def test_page_limit(tmp_path, pdf_dir):
    from litreview.config import load_settings

    ctx = create_context(
        settings=replace(load_settings(tmp_path / "d"), max_pages_per_document=2, min_free_disk_mib=1)
    )
    project = create_project(ctx, "pages")
    make_text_pdf(pdf_dir / "three.pdf", PAPER_A)
    ingest_batch(ctx, project.id, [incoming(pdf_dir / "three.pdf")])
    Worker(ctx).run_until_idle(timeout=120)
    doc = list_documents(ctx, project.id)[0]
    assert doc.status == "failed" and "limit is 2" in doc.error


def test_resume_after_interruption_does_not_duplicate(ctx, project, pdf_dir):
    make_text_pdf(pdf_dir / "a.pdf", PAPER_A * 4)  # 12 pages
    ingest_batch(ctx, project.id, [incoming(pdf_dir / "a.pdf")])
    doc = list_documents(ctx, project.id)[0]
    # Simulate a worker that crashed after committing the first pages.
    job = jobs.claim_next(ctx, (jobs.JOB_EXTRACT,), 99999)
    import litreview.pdf_extract as pe

    original = pe._commit_pages
    calls = {"n": 0}

    def crash_after_first_commit(*args, **kwargs):
        original(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt("simulated crash")

    pe._commit_pages = crash_after_first_commit
    try:
        with contextlib.suppress(KeyboardInterrupt):
            extract_document(ctx, project.id, doc.id, job.id)
    finally:
        pe._commit_pages = original
    assert len(get_pages(ctx, doc.id)) == 5
    ctx.db.execute("UPDATE jobs SET heartbeat_at = 0 WHERE id = ?", (job.id,))

    Worker(ctx).run_until_idle(timeout=120)  # restart: recovers and resumes
    pages = get_pages(ctx, doc.id)
    assert [p["page_no"] for p in pages] == list(range(1, 13))
    assert list_documents(ctx, project.id)[0].status == "completed"
    assert jobs.get_job(ctx, job.id).status == "done"


def test_cancel_and_retry(ctx, project, pdf_dir):
    files = [make_text_pdf(pdf_dir / f"p{i}.pdf", [f"Paper number {i} " * 30]) for i in range(3)]
    ingest_batch(ctx, project.id, [incoming(f) for f in files])
    assert jobs.request_cancel(ctx, project.id) == 3
    docs = list_documents(ctx, project.id)
    assert {d.status for d in docs} == {"cancelled"}
    retry_document(ctx, project.id, docs[0].id)
    Worker(ctx).run_until_idle(timeout=120)
    statuses = {d.filename: d.status for d in list_documents(ctx, project.id)}
    assert statuses["p0.pdf"] == "completed" and statuses["p1.pdf"] == "cancelled"


def test_probable_duplicate_flagged_for_review(ctx, project, pdf_dir):
    make_text_pdf(
        pdf_dir / "v1.pdf", PAPER_A, title="Effects of Insulation Retrofits on Indoor Temperature in Homes"
    )
    make_text_pdf(
        pdf_dir / "v2.pdf",
        PAPER_A + ["Erratum page"],
        title="Effects of Insulation Retrofits on Indoor Temperature in Homes.",
    )
    ingest_batch(ctx, project.id, [incoming(pdf_dir / "v1.pdf")])
    Worker(ctx).run_until_idle(timeout=120)
    ingest_batch(ctx, project.id, [incoming(pdf_dir / "v2.pdf")])
    Worker(ctx).run_until_idle(timeout=120)
    docs = _docs(ctx, project)
    assert docs["v2.pdf"].duplicate_review == "pending"
    assert docs["v2.pdf"].probable_duplicate_of.startswith("D001")
    assert docs["v2.pdf"].included  # never excluded automatically


def test_folder_import_streams_files(ctx, project, pdf_dir):
    make_text_pdf(pdf_dir / "a.pdf", PAPER_A)
    make_text_pdf(pdf_dir / "b.pdf", PAPER_B)
    (pdf_dir / "notes.txt").write_text("ignored")
    files = files_from_folder(pdf_dir)
    assert [f.name for f in files] == ["a.pdf", "b.pdf"]
    report = ingest_batch(ctx, project.id, files)
    assert report.accepted == 2
