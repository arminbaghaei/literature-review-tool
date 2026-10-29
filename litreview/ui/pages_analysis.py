"""Search/ask, matrix, synthesis, writing and export pages."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from litreview import export, matrix
from litreview.ai.client import AIError
from litreview.analysis import qa, runner
from litreview.analysis.drafting import save_output_edit
from litreview.analysis.summaries import get_summary, save_researcher_summary
from litreview.analysis.synthesis import list_themes, save_theme_edits
from litreview.documents import eligible_documents, list_documents
from litreview.search import search
from litreview.ui.common import (
    ai_status_banner,
    citation_navigator,
    doc_options,
    doc_scope_picker,
    get_ctx,
    require_project,
    run_panel,
    runs_monitor,
    source_viewer,
)

STATUS_BADGE = {
    "not_extracted": "·",
    "heuristic": "🟡 heuristic",
    "reported": "🟢 reported",
    "not_reported": "⚪ not reported",
    "not_found": "🟠 not found",
    "requires_verification": "🔴 verify",
    "researcher": "🔵 researcher",
}


# ---- Search & ask ---------------------------------------------------------------------------------


def page_search() -> None:
    ctx = get_ctx()
    project = require_project()
    st.title("Search and ask")
    tab_search, tab_ask = st.tabs(["Keyword search (no AI)", "Ask a question (AI)"])
    with tab_search:
        q = st.text_input("Search terms", placeholder='e.g. "indoor temperature" insulation', key="kw_q")
        c1, c2 = st.columns(2)
        mode = c1.radio("Match", ["any word", "all words"], horizontal=True, key="kw_mode")
        scope = doc_scope_picker(project.id, "kw_scope")
        limit = c2.slider("Max results", 10, 200, 50)
        if q:
            hits = search(
                ctx, project.id, q, doc_ids=scope, limit=limit, mode="all" if mode == "all words" else "any"
            )
            st.caption(f"{len(hits)} passage(s). Ranked by BM25 keyword relevance.")
            for i, h in enumerate(hits):
                with st.expander(
                    f"{h.label} p.{h.page_no} · {h.filename}" + (f" · {h.section}" if h.section else "")
                ):
                    st.markdown(h.snippet.replace("[", "**").replace("]", "**"))
                    if st.toggle("Open page", key=f"hit_{i}"):
                        source_viewer(project.id, h.doc_id, h.page_no, key=f"hitv_{i}")

    with tab_ask:
        if not ai_status_banner():
            return
        question = st.text_area("Question", key="qa_q")
        scope = doc_scope_picker(project.id, "qa_scope")
        exhaustive = st.toggle(
            "Exhaustive: examine every paper in scope individually (tracks coverage; slower, costs more)",
            key="qa_exh",
        )
        if not question.strip():
            st.caption(
                "Focused mode answers from the top keyword-matched passages. Use exhaustive mode for 'all papers' questions."
            )
        elif exhaustive:
            run_panel(project, runner.RUN_QA, {"question": question.strip()}, scope, key="qa_run")
        else:
            st.caption("Focused mode: one AI call over the top keyword-matched passages (not exhaustive).")
            if st.button("Ask", type="primary"):
                try:
                    with st.spinner("Asking Claude..."):
                        res = qa.ask_focused(runner.make_ai(ctx), project.id, question.strip(), scope)
                    st.session_state["qa_last"] = res
                except AIError as exc:
                    st.error(exc.user_message)
        res = st.session_state.get("qa_last")
        if res:
            if "error" in res:
                st.warning(res["error"])
            else:
                st.markdown(res["markdown"])
                citation_navigator(project.id, res["markdown"], key="qa_nav")
        runs_monitor(project.id, (runner.RUN_QA,))
        show_outputs(project.id, "qa", "Previous answers")


def show_outputs(project_id: str, kind: str, title: str) -> None:
    ctx = get_ctx()
    outs = runner.list_outputs(ctx, project_id, kind)
    if not outs:
        return
    st.subheader(title)
    opts = {
        o[
            "id"
        ]: f"{o['title'][:80]} · {pd.to_datetime(o['created_at'], unit='s'):%Y-%m-%d %H:%M} · {o['source']}"
        for o in outs
    }
    out_id = st.selectbox("Open", list(opts), format_func=opts.get, key=f"out_{kind}")
    out = runner.get_output(ctx, project_id, out_id)
    if not out:
        return
    edit = st.toggle("Edit", key=f"edit_{kind}_{out_id}")
    if edit:
        new = st.text_area("Markdown", out["content_md"], height=500, key=f"ta_{kind}_{out_id}")
        if st.button("Save edits and re-check citations", key=f"save_{kind}_{out_id}"):
            report = save_output_edit(ctx, project_id, out_id, new)
            st.success(f"Saved. Citations: {report['valid']} valid, {len(report['invalid'])} invalid.")
            for item in report["invalid"]:
                st.warning(f"{item['citation']}: {item['reason']}")
            for q in report["quotes_unverified"]:
                st.warning(f'Quotation not found on the cited page: "{q["quote"][:100]}" {q["citation"]}')
    else:
        st.markdown(out["content_md"])
    report = out.get("citation_report")
    if report:
        with st.expander("Citation check"):
            st.json(report)
    if out.get("coverage"):
        with st.expander("Coverage"):
            st.json(out["coverage"])
    citation_navigator(project_id, out["content_md"], key=f"nav_{kind}_{out_id}")
    st.download_button(
        "Download Markdown",
        export.output_markdown(ctx, project_id, out),
        file_name=f"{kind}_{out_id[:8]}.md",
        mime="text/markdown",
        key=f"dl_{kind}_{out_id}",
    )


# ---- Matrix ---------------------------------------------------------------------------------------


def page_matrix() -> None:
    ctx = get_ctx()
    project = require_project()
    st.title("Literature review matrix")
    st.caption(
        "Edit values directly; your edits are saved with history and are never overwritten by later AI runs "
        "(new AI output becomes a suggestion). Status legend: 🟢 reported with verified quote · 🔴 requires "
        "verification · ⚪ not reported · 🟠 not found in extracted text · 🟡 heuristic (unverified) · 🔵 researcher."
    )
    fields = matrix.list_fields(ctx, project.id)
    docs = list_documents(ctx, project.id)
    if not docs:
        st.info("No documents yet.")
        return
    cells = {(c.doc_id, c.field_key): c for c in matrix.get_cells(ctx, project.id)}
    view = st.radio("View", ["Values (editable)", "Status"], horizontal=True)
    rows = []
    for d in docs:
        row = {"_doc_id": d.id, "label": d.label, "file": d.filename, "processing": d.status}
        for f in fields:
            cell = cells.get((d.id, f.key))
            row[f.key] = (
                matrix.display_value(cell)
                if view.startswith("Values")
                else STATUS_BADGE.get(cell.status if cell else "not_extracted")
            )
        rows.append(row)
    df = pd.DataFrame(rows)
    col_cfg = {
        f.key: st.column_config.TextColumn(f.label, help=f.description, width="medium") for f in fields
    }
    col_cfg["_doc_id"] = None
    if view.startswith("Values"):
        edited = st.data_editor(
            df,
            hide_index=True,
            column_config=col_cfg,
            disabled=["label", "file", "processing"],
            key="matrix_editor",
            height=min(700, 38 + 35 * len(df)),
        )
        if st.button("Save matrix edits", type="primary"):
            changed = 0
            for i, row in edited.iterrows():
                orig = df.iloc[i]
                for f in fields:
                    if str(row[f.key]) != str(orig[f.key]):
                        matrix.researcher_edit(ctx, project.id, row["_doc_id"], f.key, str(row[f.key] or ""))
                        changed += 1
            st.success(f"Saved {changed} change(s).")
            st.rerun()
    else:
        st.dataframe(df, hide_index=True, column_config=col_cfg)

    st.subheader("Evidence for a cell")
    opts = doc_options(docs)
    c1, c2 = st.columns(2)
    doc_id = c1.selectbox("Document", list(opts), format_func=opts.get, key="ev_doc")
    fkeys = {f.key: f.label for f in fields}
    fkey = c2.selectbox("Field", list(fkeys), format_func=fkeys.get, key="ev_field")
    cell = cells.get((doc_id, fkey))
    if cell is None:
        st.caption("Not extracted yet.")
    else:
        st.markdown(f"**Value:** {matrix.display_value(cell)}")
        st.caption(
            f"Status: {STATUS_BADGE.get(cell.status)} · source: {cell.source} · reviewed: {cell.reviewed}"
        )
        for j, ev in enumerate(cell.evidence):
            label = f"p.{ev.get('page')}" if ev.get("page") else "PDF metadata"
            flag = "✅ verified quote" if ev.get("verified") else "⚠️ unverified"
            st.markdown(f"- {label}: {flag} — “{ev.get('quote') or ev.get('basis', '')}”")
            if ev.get("page") and st.toggle(f"Open p.{ev['page']}", key=f"evopen_{j}"):
                source_viewer(project.id, doc_id, int(ev["page"]), quote=ev.get("quote"), key=f"evv_{j}")
        cc1, cc2 = st.columns(2)
        if cc1.button("Mark reviewed" if not cell.reviewed else "Mark not reviewed"):
            matrix.set_reviewed(ctx, project.id, doc_id, fkey, not cell.reviewed)
            st.rerun()
        if cell.ai_suggestion:
            st.info(
                f"New AI suggestion (not applied because you edited/reviewed this cell): {cell.ai_suggestion['value']}"
            )
            if cc2.button("Accept suggestion"):
                matrix.accept_suggestion(ctx, project.id, doc_id, fkey)
                st.rerun()

    with st.expander("Customise extraction fields"):
        with st.form("add_field", clear_on_submit=True):
            key = st.text_input("Key (lowercase, e.g. climate_zone)")
            label = st.text_input("Column label")
            desc = st.text_area("What should be extracted?")
            if st.form_submit_button("Add field"):
                try:
                    matrix.add_field(ctx, project.id, key, label, desc)
                    st.rerun()
                except matrix.MatrixError as exc:
                    st.error(str(exc))
        custom = [f for f in fields if not f.builtin]
        if custom:
            rm = st.selectbox("Remove a custom field", [f.key for f in custom])
            if st.button("Remove field"):
                matrix.remove_field(ctx, project.id, rm)
                st.rerun()

    st.subheader("Run AI extraction")
    st.caption(
        "Processes every selected paper individually (long papers in sections) and records page-linked evidence. "
        "Researcher-edited cells are kept."
    )
    scope = doc_scope_picker(project.id, "mx_scope")
    run_panel(project, runner.RUN_MATRIX, {}, scope, key="mx_run")
    runs_monitor(project.id, (runner.RUN_MATRIX,))

    with st.expander("Edit history"):
        hist = matrix.edit_history(ctx, project.id)
        if hist:
            h = pd.DataFrame(hist)[
                ["changed_at", "entity", "entity_id", "field", "old_value", "new_value", "changed_by"]
            ]
            h["changed_at"] = pd.to_datetime(h["changed_at"], unit="s")
            st.dataframe(h, hide_index=True)


# ---- Synthesis ------------------------------------------------------------------------------------


def page_synthesis() -> None:
    ctx = get_ctx()
    project = require_project()
    st.title("Compare, themes, conflicts and gaps")
    st.caption(
        "Built from the matrix evidence records of every eligible paper in scope (run matrix extraction first). "
        "Papers without records are listed as not covered. Gaps and explanations are interpretations."
    )
    focus = st.text_input(
        "Focus (optional)", placeholder="e.g. effects on indoor temperature in rental housing"
    )
    scope = doc_scope_picker(project.id, "syn_scope")
    run_panel(project, runner.RUN_SYNTHESIS, {"focus": focus}, scope, key="syn_run")
    runs_monitor(project.id, (runner.RUN_SYNTHESIS,))
    show_outputs(project.id, "synthesis", "Synthesis outputs")

    st.subheader("Themes (editable)")
    themes = list_themes(ctx, project.id)
    df = pd.DataFrame(
        [
            {
                "theme": t["name"],
                "description": t["description"],
                "doc_label": t["label"] or "",
                "pages": t["pages"] or "",
                "note": t["note"] or "",
                "source": t["source"],
            }
            for t in themes
        ]
        or [{"theme": "", "description": "", "doc_label": "", "pages": "", "note": "", "source": ""}]
    )
    edited = st.data_editor(df, num_rows="dynamic", hide_index=True, disabled=["source"], key="themes_editor")
    if st.button("Save themes"):
        save_theme_edits(ctx, project.id, edited.to_dict("records"))
        st.success("Themes saved as researcher-owned.")
        st.rerun()


# ---- Writing --------------------------------------------------------------------------------------


def page_writing() -> None:
    ctx = get_ctx()
    project = require_project()
    st.title("Summaries and draft review")
    tab_sum, tab_draft = st.tabs(["Paper summaries", "Draft review sections"])
    with tab_sum:
        docs = eligible_documents(ctx, project.id)
        if not docs:
            st.info("No processed documents yet.")
        else:
            opts = doc_options(docs)
            doc_id = st.selectbox("Paper", list(opts), format_func=opts.get, key="sum_doc")
            summary = get_summary(ctx, project.id, doc_id)
            text = st.text_area(
                "Summary (editable)", summary["text"] if summary else "", height=260, key=f"sum_{doc_id}"
            )
            if summary:
                st.caption(
                    f"Source: {summary['source']}"
                    + (f" · model {summary['model']}" if summary.get("model") else "")
                )
                if summary.get("citation_report"):
                    rep = json.loads(summary["citation_report"])
                    if rep.get("invalid_page_refs"):
                        st.warning(
                            f"{len(rep['invalid_page_refs'])} sentence(s) had invalid page references (marked in text)."
                        )
                if summary.get("ai_suggestion"):
                    with st.expander("New AI summary (not applied because you edited this summary)"):
                        st.markdown(summary["ai_suggestion"])
            if st.button("Save summary"):
                save_researcher_summary(ctx, project.id, doc_id, text)
                st.success("Saved.")
                st.rerun()
            if text:
                citation_navigator(project.id, text, key="sum_nav")
        st.subheader("Generate summaries (AI)")
        scope = doc_scope_picker(project.id, "sum_scope")
        run_panel(project, runner.RUN_SUMMARIES, {}, scope, key="sum_run")
        runs_monitor(project.id, (runner.RUN_SUMMARIES,))

    with tab_draft:
        title = st.text_input("Section title", "Literature review", key="dr_title")
        instructions = st.text_area("Instructions (structure, emphasis, word count)", key="dr_instr")
        syn = runner.list_outputs(ctx, project.id, "synthesis")
        syn_opts = {"": "(none)"} | {o["id"]: o["title"][:80] for o in syn}
        syn_id = st.selectbox(
            "Use a synthesis output as structure (optional)", list(syn_opts), format_func=syn_opts.get
        )
        scope = doc_scope_picker(project.id, "dr_scope")
        run_panel(
            project,
            runner.RUN_DRAFT,
            {"section_title": title, "instructions": instructions, "synthesis_output_id": syn_id or None},
            scope,
            key="dr_run",
        )
        runs_monitor(project.id, (runner.RUN_DRAFT,))
        show_outputs(project.id, "draft", "Draft sections")


# ---- Export ---------------------------------------------------------------------------------------


def page_export() -> None:
    ctx = get_ctx()
    project = require_project()
    st.title("Export")
    st.caption(
        "Exports include document IDs, labels, PDF page locations, processing coverage and review status. "
        "They never contain your API key. Exported files are private research data: store them accordingly."
    )
    safe = "".join(ch if ch.isalnum() else "_" for ch in project.name)[:40]
    c1, c2 = st.columns(2)
    c1.download_button("Matrix (CSV)", export.matrix_csv(ctx, project.id), f"{safe}_matrix.csv", "text/csv")
    c1.download_button(
        "Evidence records (CSV)", export.evidence_csv(ctx, project.id), f"{safe}_evidence.csv", "text/csv"
    )
    c2.download_button(
        "Matrix + evidence + summaries + themes (JSON)",
        export.project_json(ctx, project.id),
        f"{safe}_project.json",
        "application/json",
    )
    st.subheader("Draft review and other outputs (Markdown)")
    outs = runner.list_outputs(ctx, project.id)
    if not outs:
        st.caption("No AI outputs yet.")
    for o in outs:
        out = runner.get_output(ctx, project.id, o["id"])
        st.download_button(
            f"{o['kind']}: {o['title'][:70]}",
            export.output_markdown(ctx, project.id, out),
            f"{safe}_{o['kind']}_{o['id'][:8]}.md",
            "text/markdown",
            key=f"exp_{o['id']}",
        )
