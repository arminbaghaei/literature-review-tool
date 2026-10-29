"""Search, project isolation, deletion, matrix provenance and citation validation."""

from __future__ import annotations

from conftest import PAPER_A, PAPER_B, incoming, make_text_pdf

from litreview import matrix
from litreview.chunking import chunk_pages
from litreview.citations import DocPages, parse_citations, validate_text, verify_quote
from litreview.documents import list_documents
from litreview.ingest import ingest_batch
from litreview.projects import create_project, delete_project
from litreview.search import build_fts_query, search
from litreview.worker import Worker


def _load(ctx, project, pdf_dir, papers):
    files = [make_text_pdf(pdf_dir / f"{name}.pdf", pages) for name, pages in papers.items()]
    ingest_batch(ctx, project.id, [incoming(f) for f in files])
    Worker(ctx).run_until_idle(timeout=120)


def test_search_returns_page_references_and_isolates_projects(ctx, project, pdf_dir):
    _load(ctx, project, pdf_dir, {"a": PAPER_A})
    other = create_project(ctx, "Other")
    other_dir = pdf_dir / "other"
    other_dir.mkdir()
    f = make_text_pdf(other_dir / "b.pdf", PAPER_B)
    ingest_batch(ctx, other.id, [incoming(f)])
    Worker(ctx).run_until_idle(timeout=120)

    hits = search(ctx, project.id, "households Wellington")
    assert hits and hits[0].label == "D001" and hits[0].page_no == 2
    assert not search(ctx, project.id, "heat pump rental")  # only in the other project
    assert search(ctx, other.id, "heat pump rental")[0].page_no in (1, 2)


def test_fts_query_is_injection_safe(ctx, project, pdf_dir):
    _load(ctx, project, pdf_dir, {"a": PAPER_A})
    for q in ['NEAR(" OR *', "title:foo AND", '"unclosed', "c++ (p<0.05)", "-- DROP TABLE"]:
        search(ctx, project.id, q)  # must not raise
    assert build_fts_query('"indoor temperature" homes') == '"indoor temperature" OR "homes"'
    assert search(ctx, project.id, '"indoor temperature"', mode="all")


def test_delete_project_removes_records_and_files(ctx, project, pdf_dir):
    _load(ctx, project, pdf_dir, {"a": PAPER_A})
    folder = ctx.files.project_dir(project.id)
    assert folder.exists()
    delete_project(ctx, project.id)
    assert not folder.exists()
    for table in ("documents", "chunks", "jobs", "matrix_cells", "ingest_log"):
        assert (
            ctx.db.query_one(f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", (project.id,))["n"]
            == 0
        )
    assert ctx.db.query_one("SELECT COUNT(*) AS n FROM pages")["n"] == 0
    assert (
        ctx.db.query_one("SELECT COUNT(*) AS n FROM chunks_fts WHERE chunks_fts MATCH 'households'")["n"] == 0
    )


def test_researcher_edits_are_never_overwritten(ctx, project, pdf_dir):
    _load(ctx, project, pdf_dir, {"a": PAPER_A})
    doc = list_documents(ctx, project.id)[0]
    matrix.researcher_edit(ctx, project.id, doc.id, "sample_size", "120 households (checked)")
    result = matrix.machine_write(ctx, project.id, doc.id, "sample_size", "100", "reported", "ai", [])
    assert result == "suggested"
    cell = matrix.get_cell(ctx, project.id, doc.id, "sample_size")
    assert cell.value == "120 households (checked)" and cell.source == "researcher"
    assert cell.ai_suggestion["value"] == "100"
    history = matrix.edit_history(ctx, project.id)
    assert history[0]["new_value"] == "120 households (checked)" and history[0]["changed_by"] == "researcher"
    matrix.accept_suggestion(ctx, project.id, doc.id, "sample_size")
    assert matrix.get_cell(ctx, project.id, doc.id, "sample_size").value == "100"


def test_custom_fields(ctx, project):
    matrix.add_field(ctx, project.id, "climate_zone", "Climate zone", "Climate zone of the study site")
    assert "climate_zone" in [f.key for f in matrix.list_fields(ctx, project.id)]
    try:
        matrix.remove_field(ctx, project.id, "title")
        raise AssertionError("built-in field removed")
    except matrix.MatrixError:
        pass
    matrix.remove_field(ctx, project.id, "climate_zone")


def test_quote_verification_and_citation_validation():
    page = "Retrofitted homes were on aver-\nage 1.2 degrees C warmer than control homes (p < 0.05)."
    assert verify_quote(page, "on average 1.2 degrees C warmer")
    assert verify_quote(page, "Retrofitted homes … warmer than control homes")
    assert not verify_quote(page, "2.5 degrees C warmer")
    assert not verify_quote(page, "")

    refs = parse_citations("Warmer homes [D001 p.3; D002 pp.4-5].")
    assert [(r.label, r.pages) for r in refs] == [("D001", [3]), ("D002", [4, 5])]

    docs = {"D001": DocPages(3, {1, 2, 3}, {3: page}), "D002": DocPages(5, {1, 2, 3, 4}, {})}
    text = (
        'Retrofits raised temperatures by "1.2 degrees C warmer than control homes" [D001 p.3]. '
        "Another claim [D002 pp.4-5]. Bad page [D001 p.9]. Unknown [D009 p.1].\n\n"
        + "This long paragraph makes a substantive claim about the literature without citing any source at all, which must be flagged."
    )
    report = validate_text(text, docs)
    assert report.valid == 1 and report.quotes_checked == 1 and not report.quotes_unverified
    reasons = " ".join(i["reason"] for i in report.invalid)
    assert "no readable text" in reasons and "outside 1-3" in reasons and "not in the cited scope" in reasons
    assert len(report.uncited_paragraphs) == 1

    fabricated = validate_text('They said "homes were 5 degrees warmer overall" [D001 p.3].', docs)
    assert fabricated.quotes_unverified


def test_chunks_stay_within_pages_and_keep_sections():
    chunks = chunk_pages(
        [(1, "Methods\nWe surveyed 120 homes.\n\n" + "Word " * 600), (2, "Results\nHomes were warmer.")]
    )
    assert {c.page_no for c in chunks} == {1, 2}
    assert chunks[0].section == "Methods"
    assert chunks[-1].section == "Results" and chunks[-1].page_no == 2
    assert all(len(c.text) <= 2100 for c in chunks)
