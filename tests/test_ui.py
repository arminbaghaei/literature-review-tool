"""Interface tests using Streamlit's AppTest (runs pages headlessly against a temp data dir)."""

from __future__ import annotations

import pytest
import streamlit as st
from conftest import PAPER_A, PAPER_B, incoming, make_text_pdf
from streamlit.testing.v1 import AppTest

from litreview.context import create_context
from litreview.ingest import ingest_batch
from litreview.projects import create_project
from litreview.worker import Worker


@pytest.fixture
def ui_env(tmp_path, monkeypatch, pdf_dir):
    data = tmp_path / "uidata"
    monkeypatch.setenv("LITREVIEW_DATA_DIR", str(data))
    monkeypatch.setenv("LITREVIEW_NO_WORKER_AUTOSTART", "1")
    monkeypatch.setenv("MIN_FREE_DISK_MIB", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    st.cache_resource.clear()
    ctx = create_context()
    project = create_project(ctx, "UI project", "Retrofits and temperature")
    files = [make_text_pdf(pdf_dir / "a.pdf", PAPER_A), make_text_pdf(pdf_dir / "b.pdf", PAPER_B)]
    ingest_batch(ctx, project.id, [incoming(f) for f in files])
    Worker(ctx).run_until_idle(timeout=120)
    yield ctx, project
    st.cache_resource.clear()


PAGES = {
    "projects": ("litreview.ui.pages_project", "page_projects"),
    "upload": ("litreview.ui.pages_project", "page_upload"),
    "documents": ("litreview.ui.pages_project", "page_documents"),
    "settings": ("litreview.ui.pages_project", "page_settings"),
    "search": ("litreview.ui.pages_analysis", "page_search"),
    "matrix": ("litreview.ui.pages_analysis", "page_matrix"),
    "synthesis": ("litreview.ui.pages_analysis", "page_synthesis"),
    "writing": ("litreview.ui.pages_analysis", "page_writing"),
    "export": ("litreview.ui.pages_analysis", "page_export"),
    "about": ("litreview.ui.pages_project", "page_about"),
}


def _page(name: str) -> AppTest:
    module, fn = PAGES[name]
    script = "\n".join([f"from {module} import {fn}", f"{fn}()", ""])
    return AppTest.from_string(script, default_timeout=60)


@pytest.mark.parametrize(
    "name",
    [
        "projects",
        "upload",
        "documents",
        "settings",
        "search",
        "matrix",
        "synthesis",
        "writing",
        "export",
        "about",
    ],
)
def test_every_page_renders_without_errors(ui_env, name):
    at = _page(name).run()
    assert not at.exception, at.exception


def test_app_entry_point_renders(ui_env):
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception, at.exception


def test_upload_page_shows_limits_and_status(ui_env):
    at = _page("upload").run()
    text = " ".join(m.value for m in at.info) + " ".join(m.value for m in at.markdown)
    assert "100 files" in text and "100 MiB" in text
    labels = [m.label for m in at.metric]
    assert any("completed" in label for label in labels)
    completed = next(m for m in at.metric if "completed" in m.label)
    assert completed.value == "2"


def test_keyword_search_works_without_api_key(ui_env):
    at = _page("search").run()
    at.text_input(key="kw_q").input("households Wellington").run()
    assert not at.exception
    assert any("D001 p.2" in e.label for e in at.expander)
    # The AI tab explains the missing key instead of failing.
    assert any("no Anthropic API key" in w.value for w in at.warning)


def test_matrix_edit_is_saved_with_history(ui_env):
    ctx, project = ui_env
    at = _page("matrix").run()
    assert not at.exception
    from litreview import matrix

    docs = sorted({c.doc_id for c in matrix.get_cells(ctx, project.id)})
    assert docs  # heuristic metadata present
    from litreview.documents import list_documents

    d = list_documents(ctx, project.id)[0]
    matrix.researcher_edit(ctx, project.id, d.id, "sample_size", "120 households")
    at = _page("matrix").run()
    assert not at.exception
    hist = matrix.edit_history(ctx, project.id)
    assert hist[0]["new_value"] == "120 households"


def test_create_project_from_ui(ui_env):
    at = _page("projects").run()
    at.text_input[0].input("Second review")
    at.button[0].click().run()
    assert not at.exception
    ctx, _ = ui_env
    from litreview.projects import list_projects

    assert "Second review" in [p.name for p in list_projects(ctx)]


def test_exports_download_buttons_present(ui_env):
    at = _page("export").run()
    assert not at.exception
    labels = [b.label for b in at.get("download_button")]
    assert {"Matrix (CSV)", "Evidence records (CSV)"} <= set(labels)


def test_ai_run_panel_requires_explicit_approval(ui_env, monkeypatch):
    from test_ai import FakeProvider, smart_handler

    from litreview.analysis import runner

    monkeypatch.setattr(runner, "provider_factory", lambda: FakeProvider(smart_handler))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    at = _page("matrix").run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Est. cost" in labels and "AI calls" in labels
    start = next(b for b in at.button if b.label == "Start run")
    assert start.disabled  # cannot start before approving the spend


def test_branding_and_credit_on_every_page(ui_env):
    at = AppTest.from_file("../app.py", default_timeout=60).run()
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert "Tech Innovation Experts" in html and "www.tinx.co.nz" in html
    assert "ResearchMate Academy" in html and "Dr Abdollah Baghaei" in html
