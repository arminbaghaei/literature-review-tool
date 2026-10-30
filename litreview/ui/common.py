"""Shared UI helpers: context, current project, source viewer, AI run panel."""

from __future__ import annotations

import html
import io
import math
import time

import streamlit as st

from litreview import jobs
from litreview.ai.client import AIError
from litreview.analysis import runner
from litreview.citations import normalise, parse_citations
from litreview.context import AppContext, create_context
from litreview.documents import Document, doc_by_label, get_page, list_documents
from litreview.projects import Project, ProjectError, get_project, list_projects, touch_project

STATUS_ICON = {
    "queued": "⏳",
    "processing": "⚙️",
    "completed": "✅",
    "partial": "⚠️",
    "failed": "❌",
    "cancelled": "⏹️",
}


@st.cache_resource
def get_ctx() -> AppContext:
    return create_context()


def current_project() -> Project | None:
    ctx = get_ctx()
    pid = st.query_params.get("project") or st.session_state.get("project_id")
    if pid:
        try:
            project = get_project(ctx, pid)
            st.session_state["project_id"] = project.id
            return project
        except ProjectError:
            st.session_state.pop("project_id", None)
    projects = list_projects(ctx)
    if projects:
        st.session_state["project_id"] = projects[0].id
        return projects[0]
    return None


def select_project(project_id: str) -> None:
    st.session_state["project_id"] = project_id
    st.query_params["project"] = project_id
    touch_project(get_ctx(), project_id)


def require_project() -> Project:
    project = current_project()
    if project is None:
        st.info("Create a project first on the **Projects** page.")
        st.stop()
    return project


def fmt_mib(n: int | float | None) -> str:
    if not n:
        return "0 MiB"
    return f"{n / 1048576:,.1f} MiB"


def doc_options(docs: list[Document]) -> dict[str, str]:
    return {d.id: f"{d.label} · {d.filename}" for d in docs}


def render_page_image(
    ctx: AppContext, project_id: str, doc_id: str, page_no: int, scale: float = 1.4
) -> bytes | None:
    import pypdfium2 as pdfium

    path = ctx.files.original_path(project_id, doc_id)
    if not path.exists():
        return None
    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_no - 1]
        try:
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            page.close()
    finally:
        pdf.close()


def highlight(text: str, quote: str | None) -> str:
    """Return HTML of page text with the quote highlighted when it can be located."""
    safe = html.escape(text)
    if quote:
        target = html.escape(quote.strip())
        idx = safe.find(target)
        if idx < 0:
            # fall back to a case-insensitive search on a normalised form
            low = safe.lower()
            idx = low.find(target.lower())
        if idx >= 0:
            safe = (
                safe[:idx] + "<mark>" + safe[idx : idx + len(target)] + "</mark>" + safe[idx + len(target) :]
            )
    return "<div style='white-space: pre-wrap; font-size: 0.9rem; line-height: 1.4'>" + safe + "</div>"


def source_viewer(
    project_id: str, doc_id: str, page_no: int, quote: str | None = None, key: str = "sv"
) -> None:
    """Show one PDF page: rendered image and extracted text side by side, quote highlighted."""
    ctx = get_ctx()
    page = get_page(ctx, doc_id, page_no)
    if page is None:
        st.warning(f"Page {page_no} has not been extracted.")
        return
    meta = f"PDF page {page_no}"
    if page["page_label"]:
        meta += f" (printed label {page['page_label']})"
    meta += f" · extraction: {page['method']} · quality: {page['quality']}"
    if page["ocr_confidence"] is not None:
        meta += f" · OCR confidence {page['ocr_confidence']:.2f}"
    if page["flags"]:
        meta += f" · flags: {page['flags']}"
    st.caption(meta)
    if quote:
        found = normalise(quote) in normalise(page["text"])
        st.caption(
            "✅ Quotation found on this page." if found else "⚠️ Quotation not found verbatim on this page."
        )
    left, right = st.columns(2)
    with left:
        if st.toggle("Show original page image", value=True, key=f"{key}_img"):
            img = render_page_image(ctx, project_id, doc_id, page_no)
            if img:
                st.image(img, width="stretch")
            else:
                st.warning("Original PDF not found in storage.")
    with right:
        st.markdown(highlight(page["text"], quote), unsafe_allow_html=True)


def citation_navigator(project_id: str, text: str, key: str) -> None:
    """List the citations found in a text and open any of them in the source viewer."""
    refs = parse_citations(text)
    if not refs:
        return
    ctx = get_ctx()
    options = []
    seen = set()
    for r in refs:
        for p in r.pages:
            k = (r.label, p)
            if k not in seen:
                seen.add(k)
                options.append(k)
    with st.expander(f"Open cited sources ({len(options)} page references)"):
        choice = st.selectbox(
            "Citation", options, format_func=lambda k: f"{k[0]} p.{k[1]}", key=f"{key}_cite"
        )
        if choice:
            doc = doc_by_label(ctx, project_id, choice[0])
            if doc is None:
                st.error(f"{choice[0]} does not exist in this project (invalid citation).")
            elif not doc.page_count or choice[1] > doc.page_count:
                st.error(f"{choice[0]} has no page {choice[1]} (invalid citation).")
            else:
                st.caption(f"{doc.label} · {doc.filename}")
                source_viewer(project_id, doc.id, choice[1], key=f"{key}_viewer")


def ai_status_banner() -> bool:
    ai = runner.make_ai(get_ctx())
    if not ai.available():
        st.warning(
            "AI features are off: no Anthropic API key is configured. Upload, extraction, browsing "
            "and keyword search still work. To enable AI, run `scripts/set-api-key.ps1` (local) or add "
            "`ANTHROPIC_API_KEY` to the app's secrets (Streamlit Cloud); see README."
        )
        return False
    return True


def run_panel(project: Project, kind: str, params: dict, doc_ids: list[str] | None, key: str) -> None:
    """Estimate cost, ask for explicit approval, then start a background AI run."""
    ctx = get_ctx()
    if not ai_status_banner():
        return
    ai = runner.make_ai(ctx)
    try:
        est = runner.estimate(ctx, project.id, kind, params, doc_ids)
    except (AIError, runner.RunError, ValueError) as exc:
        st.error(getattr(exc, "user_message", str(exc)))
        return
    remaining = max(0.0, ai.project_budget(project.id) - ai.project_spent(project.id))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Documents", est["documents"])
    c2.metric("AI calls", est["calls"])
    c3.metric("Est. tokens", f"{(est['input_tokens'] + est['output_tokens']) / 1000:,.0f}k")
    c4.metric("Est. cost", f"${est['estimated_cost_usd']:.2f}")
    st.caption(
        f"Model `{est['model']}`. Estimates are approximate (input over-estimated; output includes an allowance "
        f"for model reasoning). Project budget remaining: ${remaining:.2f}. Already-completed calls are reused, not re-billed. "
        "Document text in scope is sent to Anthropic's API."
    )
    for note in est["notes"]:
        st.info(note)
    if est["calls"] == 0:
        st.info("Nothing to process.")
        return
    default = min(remaining, max(0.05, math.ceil(est["estimated_cost_usd"] * 150) / 100))
    budget = st.number_input(
        "Maximum spend for this run (USD)",
        min_value=0.0,
        max_value=float(remaining),
        value=float(default),
        step=0.5,
        key=f"{key}_budget",
    )
    approve = st.checkbox(
        f"I approve sending this content to the Claude API and spending up to ${budget:.2f}", key=f"{key}_ok"
    )
    if st.button("Start run", type="primary", disabled=not approve, key=f"{key}_start"):
        try:
            run_id = runner.start_run(
                ctx, project.id, kind, params, doc_ids, budget, est["estimated_cost_usd"]
            )
        except runner.RunError as exc:
            st.error(str(exc))
            return
        from litreview.worker import ensure_worker_running

        ensure_worker_running(ctx)
        st.session_state[f"{key}_last_run"] = run_id
        st.success("Run started in the background. Progress appears below; you can leave this page.")


@st.fragment(run_every=4)
def runs_monitor(project_id: str, kinds: tuple[str, ...]) -> None:
    ctx = get_ctx()
    runs = [r for r in jobs.list_runs(ctx, project_id) if r["kind"] in kinds][:8]
    if not runs:
        return
    st.markdown("**Recent runs**")
    for r in runs:
        prog = runner.run_progress(ctx, r["id"])
        done = sum(v for k, v in prog["counts"].items() if k in ("done", "failed", "cancelled"))
        total = prog["total"]
        label = runner.RUN_LABELS.get(r["kind"], r["kind"])
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
        cols = st.columns([4, 2, 2, 1])
        detail = (
            f"{done}/{total} documents" if total else (prog["finalize"]["status"] if prog["finalize"] else "")
        )
        cols[0].write(f"{label} · {when}")
        cols[1].write(f"**{r['status']}** · {detail}")
        cols[2].write(f"${r['spent_usd']:.3f} of ${r['approved_budget_usd'] or 0:.2f}")
        if r["status"] == "running" and cols[3].button("Cancel", key=f"cancel_{r['id']}"):
            jobs.cancel_run(ctx, project_id, r["id"])
            st.rerun()
        if total and r["status"] == "running":
            st.progress(done / total if total else 0.0)
        if r["error"]:
            st.caption(f"⚠️ {r['error']}")
        failures = (r.get("result") and isinstance(r["result"], str) and r["result"]) or None
        if failures:
            import json

            res = json.loads(failures)
            if res.get("failures"):
                with st.expander(f"{len(res['failures'])} document(s) not processed"):
                    st.dataframe(res["failures"], hide_index=True)


def doc_scope_picker(project_id: str, key: str, label: str = "Scope") -> list[str] | None:
    """Return None for the whole collection, or a list of selected document ids."""
    docs = [d for d in list_documents(get_ctx(), project_id) if d.eligible]
    mode = st.radio(
        label, ["All eligible papers", "Selected papers", "One paper"], horizontal=True, key=f"{key}_mode"
    )
    opts = doc_options(docs)
    if mode == "Selected papers":
        return st.multiselect("Papers", list(opts), format_func=opts.get, key=f"{key}_multi") or []
    if mode == "One paper":
        one = st.selectbox("Paper", list(opts), format_func=opts.get, key=f"{key}_one")
        return [one] if one else []
    return None
