"""Streamlit entry point: ``python -m streamlit run app.py``."""

from __future__ import annotations

import hmac
import logging
import os

import streamlit as st

from litreview.ui.common import current_project, get_ctx
from litreview.ui.pages_analysis import page_export, page_matrix, page_search, page_synthesis, page_writing
from litreview.ui.pages_project import page_documents, page_projects, page_settings, page_upload
from litreview.worker import ensure_worker_running

logging.basicConfig(level=logging.WARNING)

st.set_page_config(page_title="Literature Review Tool", page_icon="📚", layout="wide")


def password_gate() -> None:
    """Optional single-user password (set LITREVIEW_APP_PASSWORD before exposing the app on a network)."""
    expected = os.environ.get("LITREVIEW_APP_PASSWORD")
    if not expected or st.session_state.get("authed"):
        return
    st.title("Literature Review Tool")
    pw = st.text_input("Password", type="password")
    if pw:
        if hmac.compare_digest(pw.encode(), expected.encode()):
            st.session_state["authed"] = True
            st.rerun()
        st.error("Incorrect password")
    st.stop()


password_gate()
ctx = get_ctx()
ensure_worker_running(ctx)

project = current_project()
with st.sidebar:
    st.markdown("### 📚 Literature Review")
    st.caption(f"Project: **{project.name}**" if project else "No project yet")

nav = st.navigation(
    {
        "Project": [
            st.Page(page_projects, title="Projects", icon="🗂️", default=True),
            st.Page(page_upload, title="Upload & processing", icon="⬆️", url_path="upload"),
            st.Page(page_documents, title="Documents & sources", icon="📄", url_path="documents"),
        ],
        "Analyse": [
            st.Page(page_search, title="Search & ask", icon="🔎", url_path="search"),
            st.Page(page_matrix, title="Review matrix", icon="🧮", url_path="matrix"),
            st.Page(page_synthesis, title="Compare & themes", icon="🧩", url_path="synthesis"),
            st.Page(page_writing, title="Summaries & draft", icon="✍️", url_path="writing"),
        ],
        "Output": [
            st.Page(page_export, title="Export", icon="💾", url_path="export"),
            st.Page(page_settings, title="Settings", icon="⚙️", url_path="settings"),
        ],
    }
)
with st.sidebar:
    st.caption(
        "Research assistant only: verify AI output against the sources. This tool does not perform a "
        "complete systematic review."
    )
nav.run()
