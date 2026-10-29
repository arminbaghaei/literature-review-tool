"""Projects, upload/processing and document pages."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from litreview import jobs
from litreview.documents import (
    get_pages,
    list_documents,
    remove_document,
    retry_document,
    set_duplicate_review,
    set_inclusion,
    set_study_group,
    status_counts,
)
from litreview.ingest import IncomingFile, files_from_folder, ingest_batch, ingest_log
from litreview.projects import ProjectError, create_project, delete_project, list_projects, update_project
from litreview.ui.common import (
    STATUS_ICON,
    current_project,
    fmt_mib,
    get_ctx,
    require_project,
    select_project,
    source_viewer,
)
from litreview.worker import ensure_worker_running


def page_projects() -> None:
    ctx = get_ctx()
    st.title("Projects")
    st.caption(
        "Each project holds one review corpus. Projects, documents and processing state are saved on "
        f"this computer in `{ctx.settings.data_dir}` and survive restarts."
    )
    projects = list_projects(ctx)
    current = current_project()
    with st.form("new_project", clear_on_submit=True):
        st.subheader("New project")
        name = st.text_input("Project name")
        question = st.text_area("Review question (optional, editable later)")
        if st.form_submit_button("Create project", type="primary"):
            try:
                p = create_project(ctx, name, question)
                select_project(p.id)
                st.success(f"Created '{p.name}'.")
                st.rerun()
            except ProjectError as exc:
                st.error(str(exc))
    if not projects:
        return
    st.subheader("Open a project")
    for p in projects:
        counts = status_counts(ctx, p.id)
        total = sum(counts.values())
        cols = st.columns([4, 3, 1])
        cols[0].markdown(f"**{p.name}**" + ("  ← current" if current and current.id == p.id else ""))
        cols[1].caption(f"{total} documents · " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
        if cols[2].button("Open", key=f"open_{p.id}"):
            select_project(p.id)
            st.rerun()


def page_settings() -> None:
    ctx = get_ctx()
    project = require_project()
    from litreview.analysis.runner import make_ai

    st.title("Project settings")
    with st.form("settings"):
        name = st.text_input("Project name", project.name)
        question = st.text_area(
            "Review question",
            project.review_question,
            help="Used as context for AI extraction and synthesis.",
        )
        budget = st.number_input(
            "AI spending limit for this project (USD)",
            min_value=0.0,
            value=float(project.ai_budget_usd),
            step=1.0,
        )
        if st.form_submit_button("Save", type="primary"):
            update_project(ctx, project.id, name=name, review_question=question, ai_budget_usd=budget)
            st.success("Saved.")
            st.rerun()

    st.subheader("AI configuration")
    ai = make_ai(ctx)
    s = ctx.settings
    st.write(
        {
            "API key configured": ai.available(),
            "Model": s.ai_model,
            "Effort": s.ai_effort,
            "Max output tokens per call": s.ai_max_output_tokens,
            "Section budget (tokens)": s.ai_section_token_budget,
            "Price (USD per million tokens, in/out)": s.price_for(s.ai_model),
        }
    )
    st.caption("The API key is read from the environment only and is never displayed, stored or exported.")
    usage = ai.usage_summary(project.id)
    if usage:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("AI calls", usage.get("calls", 0))
        c2.metric("Failed / refused", usage.get("failed") or 0)
        c3.metric(
            "Tokens in/out",
            f"{(usage.get('inp') or 0) / 1000:,.0f}k / {(usage.get('outp') or 0) / 1000:,.0f}k",
        )
        c4.metric("Spent", f"${usage.get('cost') or 0:.2f} of ${project.ai_budget_usd:.2f}")
    calls = ctx.db.query(
        "SELECT created_at, purpose, model, status, input_tokens, output_tokens, cost_usd, error FROM ai_calls"
        " WHERE project_id = ? ORDER BY id DESC LIMIT 200",
        (project.id,),
    )
    if calls:
        df = pd.DataFrame([dict(r) for r in calls])
        df["created_at"] = pd.to_datetime(df["created_at"], unit="s")
        with st.expander("AI call log (most recent 200)"):
            st.dataframe(df, hide_index=True)

    st.subheader("Limits in force")
    st.write(
        {
            "Files per batch": s.max_files_per_batch,
            "Per-file size (MiB)": s.max_file_size_mib,
            "Per-batch size (MiB)": s.max_batch_size_mib,
            "Project storage quota (MiB)": s.max_project_storage_mib,
            "Pages per document": s.max_pages_per_document,
            "Per-document timeout (s)": s.document_timeout_seconds,
            "Parallel extraction processes": s.extraction_workers,
            "OCR": "enabled (RapidOCR, local)" if s.ocr_enabled else "disabled",
        }
    )
    st.subheader("Delete project")
    st.caption(
        "Deletes this project's PDFs, extracted text, index, matrix, outputs and logs from this computer. "
        "Text already sent to the Claude API is subject to Anthropic's data retention policy. Backups or "
        "sync copies outside the data folder are not touched."
    )
    confirm = st.text_input("Type the project name to confirm deletion", key="del_confirm")
    if st.button("Delete project permanently", disabled=confirm != project.name):
        delete_project(ctx, project.id)
        st.session_state.pop("project_id", None)
        st.query_params.clear()
        st.success("Project deleted.")
        st.rerun()


class _UploadedSource:
    """Adapter so a Streamlit UploadedFile can be streamed into storage in chunks."""

    def __init__(self, uploaded) -> None:
        self._f = uploaded

    def __enter__(self):
        self._f.seek(0)
        return self._f

    def __exit__(self, *exc) -> None:
        return None


def _show_report(report) -> None:
    if report.batch_error:
        st.error(f"Batch rejected: {report.batch_error}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Accepted", report.accepted)
    c2.metric("Duplicates (not stored again)", report.duplicates)
    c3.metric("Rejected", report.rejected)
    problems = [o for o in report.outcomes if o.outcome != "accepted"]
    if problems:
        st.dataframe(
            pd.DataFrame([{"file": o.filename, "outcome": o.outcome, "reason": o.reason} for o in problems]),
            hide_index=True,
        )


def page_upload() -> None:
    ctx = get_ctx()
    project = require_project()
    s = ctx.settings
    st.title("Upload and processing")
    st.info(
        f"Limits: up to **{s.max_files_per_batch} files** per batch, **{s.max_file_size_mib} MiB** per file, "
        f"**{s.max_batch_size_mib:,} MiB** per batch, **{s.max_pages_per_document:,} pages** per document; project "
        f"storage quota **{s.max_project_storage_mib:,} MiB**. You can add more papers later in further batches."
    )
    ensure_worker_running(ctx)

    tab_browser, tab_folder = st.tabs(["Upload from browser", "Import from a folder (large batches)"])
    with tab_browser:
        st.caption(
            f"Browser uploads are held in this app's memory until they are saved. For batches above about "
            f"{s.browser_upload_safe_mib:,} MiB, use folder import instead, which streams files from disk."
        )
        n = st.session_state.get("uploader_n", 0)
        files = st.file_uploader(
            "Select PDF files", type=["pdf"], accept_multiple_files=True, key=f"uploader_{n}"
        )
        if files:
            total = sum(f.size for f in files)
            st.write(f"{len(files)} file(s) selected, {fmt_mib(total)}.")
            if total > s.browser_upload_safe_mib * 1048576:
                st.warning("This selection is large for an in-memory upload. Consider folder import.")
            if st.button("Add to project", type="primary", key="add_upload"):
                incoming = [IncomingFile(f.name, f.size, (lambda f=f: _UploadedSource(f))) for f in files]
                with st.spinner("Saving files..."):
                    report = ingest_batch(ctx, project.id, incoming)
                st.session_state["last_report"] = report
                st.session_state["uploader_n"] = n + 1  # clears the widget and frees its memory
                ensure_worker_running(ctx)
                st.rerun()
    with tab_folder:
        st.caption(
            "Enter a folder on this computer that contains the PDFs. Files are copied in 8 MiB chunks without "
            "loading whole files into memory. Originals in the folder are not modified."
        )
        folder = st.text_input("Folder path", placeholder=r"C:\Users\me\Documents\Papers")
        recursive = st.checkbox("Include subfolders")
        if folder:
            try:
                found = files_from_folder(Path(folder.strip().strip('"')), recursive)
                st.write(f"Found {len(found)} PDF(s), {fmt_mib(sum(f.size or 0 for f in found))}.")
                if found and st.button("Import folder", type="primary"):
                    progress = st.progress(0.0, text="Importing...")
                    reports = []
                    step = s.max_files_per_batch
                    for i in range(0, len(found), step):
                        reports.append(ingest_batch(ctx, project.id, found[i : i + step]))
                        progress.progress(
                            min(1.0, (i + step) / len(found)),
                            text=f"Imported {min(i + step, len(found))}/{len(found)}",
                        )
                    merged = reports[0]
                    for r in reports[1:]:
                        merged.outcomes += r.outcomes
                    st.session_state["last_report"] = merged
                    ensure_worker_running(ctx)
                    st.rerun()
            except FileNotFoundError as exc:
                st.error(str(exc))

    if "last_report" in st.session_state:
        st.subheader("Last batch")
        _show_report(st.session_state["last_report"])

    processing_status(project.id)

    with st.expander("Upload history"):
        log = ingest_log(ctx, project.id)
        if log:
            df = pd.DataFrame(log)[["created_at", "filename", "outcome", "reason", "size_bytes"]]
            df["created_at"] = pd.to_datetime(df["created_at"], unit="s")
            st.dataframe(df, hide_index=True)


@st.fragment(run_every=3)
def processing_status(project_id: str) -> None:
    ctx = get_ctx()
    st.subheader("Processing status")
    worker_ok = jobs.worker_alive(ctx)
    docs = list_documents(ctx, project_id)
    counts = status_counts(ctx, project_id)
    cols = st.columns(7)
    for col, key in zip(
        cols, ["queued", "processing", "completed", "partial", "failed", "cancelled"], strict=False
    ):
        col.metric(f"{STATUS_ICON[key]} {key}", counts.get(key, 0))
    cols[6].metric("Total", len(docs))
    total_pages = sum(d.page_count or 0 for d in docs if d.status in ("processing", "queued"))
    done_pages = sum(d.pages_done for d in docs if d.status in ("processing", "queued"))
    active = counts.get("queued", 0) + counts.get("processing", 0)
    if active:
        frac = (len(docs) - active) / len(docs) if docs else 0
        st.progress(
            frac,
            text=f"{len(docs) - active}/{len(docs)} documents finished · {done_pages}/{total_pages or '?'} pages of active documents",
        )
    if not worker_ok:
        # Restart automatically (rate-limited inside ensure_worker_running), e.g. after a crash.
        started = ensure_worker_running(ctx)
        st.warning(
            "The background worker is not running"
            + ("; starting it now." if started else ".")
            + " Processing resumes from the last saved page when it starts."
        )
    elif not active:
        st.caption("Worker idle. All queued work is finished.")
    if not docs:
        return
    rows = []
    for d in docs:
        progress = f"{d.pages_done}/{d.page_count}" if d.page_count else ""
        rows.append(
            {
                "label": d.label,
                "file": d.filename,
                "status": f"{STATUS_ICON.get(d.status, '')} {d.status}",
                "pages": progress,
                "coverage": d.coverage_note if d.status in ("completed", "partial") else "",
                "size": fmt_mib(d.size_bytes),
                "message": d.error or "",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, height=min(600, 38 + 35 * len(rows)))
    failed = [d for d in docs if d.status in ("failed", "cancelled")]
    c1, c2 = st.columns(2)
    if failed and c1.button(f"Retry {len(failed)} failed/cancelled document(s)"):
        for d in failed:
            retry_document(ctx, project_id, d.id)
        ensure_worker_running(ctx)
        st.rerun()
    if active and c2.button("Cancel processing"):
        n = jobs.request_cancel(ctx, project_id)
        st.info(f"Cancellation requested for {n} job(s). Completed pages are kept.")


def page_documents() -> None:
    ctx = get_ctx()
    project = require_project()
    st.title("Documents and sources")
    docs = list_documents(ctx, project.id)
    if not docs:
        st.info("No documents yet. Add PDFs on the Upload page.")
        return
    dups = [d for d in docs if d.duplicate_review == "pending"]
    if dups:
        with st.expander(f"⚠️ {len(dups)} probable duplicate publication(s) need review", expanded=True):
            for d in dups:
                st.write(f"**{d.label}** {d.filename} — possible duplicate of {d.probable_duplicate_of}")
                c1, c2, c3 = st.columns(3)
                if c1.button("Not a duplicate", key=f"nd_{d.id}"):
                    set_duplicate_review(ctx, project.id, d.id, "not_duplicate")
                    st.rerun()
                if c2.button("Same study (keep, group)", key=f"ss_{d.id}"):
                    other = (d.probable_duplicate_of or "").split(":")[0]
                    set_duplicate_review(ctx, project.id, d.id, "same_study", study_group=f"study-{other}")
                    set_study_group(
                        ctx, project.id, next(x.id for x in docs if x.label == other), f"study-{other}"
                    )
                    st.rerun()
                if c3.button("Duplicate (exclude)", key=f"dd_{d.id}"):
                    set_duplicate_review(ctx, project.id, d.id, "duplicate")
                    st.rerun()

    table = pd.DataFrame(
        [
            {
                "label": d.label,
                "file": d.filename,
                "status": d.status,
                "pages": d.page_count or 0,
                "coverage": d.coverage_note,
                "OCR pages": d.ocr_pages,
                "unreadable": d.unreadable_pages,
                "tables/stats pages": d.flagged_pages,
                "included": d.included,
                "study group": d.study_group or "",
                "note": d.error or d.exclusion_reason or "",
            }
            for d in docs
        ]
    )
    st.dataframe(table, hide_index=True)
    st.caption(
        "Pages flagged for tables/statistics or OCR are potential extraction failure points: verify numbers there "
        "against the original page image."
    )

    st.subheader("Source viewer")
    opts = {d.id: f"{d.label} · {d.filename}" for d in docs}
    doc_id = st.selectbox("Document", list(opts), format_func=opts.get, key="viewer_doc")
    doc = next(d for d in docs if d.id == doc_id)
    pages = get_pages(ctx, doc.id)
    if pages:
        page_no = st.number_input("PDF page", min_value=1, max_value=len(pages), value=1, key="viewer_page")
        bad = [p["page_no"] for p in pages if p["quality"] in ("unreadable", "ocr_low_conf", "low_text")]
        if bad:
            st.caption(
                f"Pages needing attention: {', '.join(map(str, bad[:60]))}{' ...' if len(bad) > 60 else ''}"
            )
        source_viewer(project.id, doc.id, int(page_no), key="docviewer")
    else:
        st.info(f"No extracted pages. Status: {doc.status}. {doc.error or ''}")

    with st.expander("Inclusion, study grouping and removal"):
        inc = st.checkbox("Include this document in analyses", value=doc.included, key=f"inc_{doc.id}")
        reason = st.text_input("Exclusion reason", value=doc.exclusion_reason or "", key=f"reason_{doc.id}")
        group = st.text_input(
            "Study group (papers reporting the same study share a group and are not counted as independent)",
            value=doc.study_group or "",
            key=f"group_{doc.id}",
        )
        if st.button("Save", key=f"save_doc_{doc.id}"):
            set_inclusion(ctx, project.id, doc.id, inc, reason)
            set_study_group(ctx, project.id, doc.id, group)
            st.success("Saved.")
            st.rerun()
        if st.button("Remove document and its derived data", key=f"rm_{doc.id}"):
            remove_document(ctx, project.id, doc.id)
            st.rerun()
    st.caption(f"SHA-256 {doc.sha256} · {fmt_mib(doc.size_bytes)} · stored as {doc.id}.pdf")
