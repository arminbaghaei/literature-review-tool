"""Cross-paper comparison, themes, conflicts and research gaps.

Synthesis is built from the source-linked evidence records of every eligible document (matrix
extraction must have run). Documents without records are listed as not covered rather than
silently ignored. If the records exceed the token budget they are synthesised in groups and the
group results are merged deterministically, with citations kept.
"""

from __future__ import annotations

import json

from litreview.ai.client import AIRequest, AIService, estimate_tokens
from litreview.ai.prompts import SYNTH_VERSION, SYSTEM_RULES, SynthesisOutput, synthesis_prompt
from litreview.analysis.common import dumps, matrix_records, page_lookup
from litreview.analysis.qa import _save_output
from litreview.citations import DocPages, format_citation
from litreview.context import AppContext
from litreview.db import new_id, now
from litreview.documents import eligible_documents, independent_study_count


def _check_refs(refs: list, lookup: dict[str, DocPages], problems: list[dict], where: str) -> list[dict]:
    good = []
    for ref in refs:
        label = ref.doc_label.strip().upper()
        doc = lookup.get(label)
        if doc is None:
            problems.append({"where": where, "citation": label, "reason": "document not in scope"})
            continue
        pages = [p for p in ref.pages if 1 <= p <= doc.page_count and p in doc.readable]
        dropped = [p for p in ref.pages if p not in pages]
        if dropped:
            problems.append(
                {
                    "where": where,
                    "citation": f"{label} p.{dropped[0]}",
                    "reason": "invalid or unreadable page",
                }
            )
        good.append({"doc_label": label, "pages": pages, "note": ref.note})
    return good


def _cite(refs: list[dict]) -> str:
    return " ".join(format_citation(r["doc_label"], r["pages"]) for r in refs)


def validate_synthesis(out: SynthesisOutput, lookup: dict[str, DocPages]) -> tuple[dict, list[dict]]:
    problems: list[dict] = []
    data = {
        "themes": [
            {
                "name": t.name,
                "description": t.description,
                "docs": _check_refs(t.docs, lookup, problems, f"theme '{t.name}'"),
            }
            for t in out.themes
        ],
        "comparisons": [
            {
                "aspect": c.aspect,
                "summary": c.summary,
                "docs": _check_refs(c.docs, lookup, problems, f"comparison '{c.aspect}'"),
                "comparability_caveats": c.comparability_caveats,
            }
            for c in out.comparisons
        ],
        "conflicts": [
            {
                "topic": c.topic,
                "positions": [
                    {
                        "position": p.position,
                        "docs": _check_refs(p.docs, lookup, problems, f"conflict '{c.topic}'"),
                    }
                    for p in c.positions
                ],
                "possible_explanations": c.possible_explanations,
            }
            for c in out.conflicts
        ],
        "gaps": [
            {
                "gap": g.gap,
                "rationale": g.rationale,
                "supporting_docs": _check_refs(g.supporting_docs, lookup, problems, f"gap '{g.gap[:40]}'"),
                "confidence": g.confidence,
            }
            for g in out.gaps
        ],
        "notes": out.notes,
    }
    return data, problems


def render_synthesis(data: dict, coverage_text: str, problems: list[dict]) -> str:
    md = [
        "# Cross-paper synthesis",
        "",
        f"*{coverage_text}*",
        "",
        "*AI-generated interpretation built from page-linked evidence records. Verify before use.*",
        "",
    ]
    md.append("## Themes")
    for t in data["themes"]:
        md.append(f"- **{t['name']}**: {t['description']} {_cite(t['docs'])}")
    md += ["", "## Comparisons"]
    for c in data["comparisons"]:
        md.append(f"- **{c['aspect']}**: {c['summary']} {_cite(c['docs'])}")
        if c["comparability_caveats"]:
            md.append(f"  - Comparability: {c['comparability_caveats']}")
    md += ["", "## Conflicting findings"]
    if not data["conflicts"]:
        md.append("- None identified in the evidence records.")
    for c in data["conflicts"]:
        md.append(f"- **{c['topic']}**")
        for p in c["positions"]:
            md.append(f"  - {p['position']} {_cite(p['docs'])}")
        md.append(f"  - *Possible explanations (interpretation):* {c['possible_explanations']}")
    md += ["", "## Potential research gaps (interpretation)"]
    for g in data["gaps"]:
        md.append(
            f"- **{g['gap']}** (confidence: {g['confidence']}). Basis: {g['rationale']} {_cite(g['supporting_docs'])}"
        )
    if data.get("notes"):
        md += ["", f"**Notes:** {data['notes']}"]
    if problems:
        md += ["", "## Citation problems removed during validation"]
        md += [f"- {p['where']}: {p['citation']} ({p['reason']})" for p in problems]
    return "\n".join(md)


def _merge(parts: list[SynthesisOutput]) -> SynthesisOutput:
    if len(parts) == 1:
        return parts[0]
    return SynthesisOutput(
        themes=[t for p in parts for t in p.themes],
        comparisons=[c for p in parts for c in p.comparisons],
        conflicts=[c for p in parts for c in p.conflicts],
        gaps=[g for p in parts for g in p.gaps],
        notes=" ".join(
            [
                f"Synthesised in {len(parts)} groups because the evidence exceeded one request; themes from "
                "different groups may overlap and should be merged by the researcher."
            ]
            + [p.notes for p in parts if p.notes]
        ),
    )


def run_synthesis(ai: AIService, project_id: str, run: dict) -> dict:
    ctx = ai.ctx
    params = run["params"]
    docs = eligible_documents(ctx, project_id, params.get("doc_ids"))
    records, missing = matrix_records(ctx, project_id, docs)
    if not records:
        raise ValueError("No evidence records. Run matrix extraction first.")
    covered = [r["doc_label"] for r in records]
    coverage_text = (
        f"Built from evidence records of {len(records)} of {len(docs)} eligible documents "
        f"({independent_study_count([d for d in docs if d.label in covered])} independent studies)."
    )
    if missing:
        coverage_text += f" Not covered (no matrix extraction yet): {', '.join(missing)}."
    excluded = params.get("excluded", [])
    if excluded:
        coverage_text += f" Not eligible: {', '.join(excluded)}."
    project = ctx.db.query_one("SELECT review_question FROM projects WHERE id = ?", (project_id,))
    budget = ctx.settings.ai_synthesis_token_budget
    groups, cur, used = [], [], 0
    for rec in records:
        t = estimate_tokens(dumps(rec))
        if cur and used + t > budget:
            groups.append(cur)
            cur, used = [], 0
        cur.append(rec)
        used += t
    groups.append(cur)
    outputs, model = [], None
    for i, group in enumerate(groups, start=1):
        note = coverage_text if len(groups) == 1 else f"{coverage_text} Group {i} of {len(groups)}."
        req = AIRequest(
            purpose=f"synthesis:{i}",
            system=SYSTEM_RULES,
            user=synthesis_prompt(
                project["review_question"] if project else "", dumps(group), note, params.get("focus", "")
            ),
            output_model=SynthesisOutput,
            prompt_version=SYNTH_VERSION,
            expected_output_tokens=8000,
        )
        result = ai.call(project_id, req, run_id=run["id"])
        outputs.append(result.data)
        model = result.model
    lookup = page_lookup(ctx, [d for d in docs if d.label in covered])
    data, problems = validate_synthesis(_merge(outputs), lookup)
    md = render_synthesis(data, coverage_text, problems)
    coverage = {
        "covered": covered,
        "missing_extraction": missing,
        "excluded": excluded,
        "text": coverage_text,
        "groups": len(groups),
    }
    out_id = _save_output(
        ctx,
        project_id,
        "synthesis",
        params.get("focus") or "Cross-paper synthesis",
        params,
        md,
        data,
        coverage,
        {"problems": problems},
        model,
        SYNTH_VERSION,
    )
    save_ai_themes(ctx, project_id, data["themes"])
    return {"output_id": out_id, "groups": len(groups)}


def save_ai_themes(ctx: AppContext, project_id: str, themes: list[dict]) -> None:
    """Replace previous AI themes; researcher-created or researcher-edited themes are kept."""
    labels = {
        r["label"]: r["id"]
        for r in ctx.db.query("SELECT id, label FROM documents WHERE project_id = ?", (project_id,))
    }
    with ctx.db.transaction() as c:
        c.execute("DELETE FROM themes WHERE project_id = ? AND source = 'ai'", (project_id,))
        for t in themes:
            theme_id = new_id()
            c.execute(
                "INSERT INTO themes(id, project_id, name, description, source, created_at) VALUES (?, ?, ?, ?, 'ai', ?)",
                (theme_id, project_id, t["name"], t["description"], now()),
            )
            for ref in t["docs"]:
                if ref["doc_label"] in labels:
                    c.execute(
                        "INSERT OR IGNORE INTO theme_docs(theme_id, doc_id, pages, note, source) VALUES (?, ?, ?, ?, 'ai')",
                        (
                            theme_id,
                            labels[ref["doc_label"]],
                            ",".join(map(str, ref["pages"])),
                            ref.get("note", ""),
                        ),
                    )


def list_themes(ctx: AppContext, project_id: str) -> list[dict]:
    rows = ctx.db.query(
        "SELECT t.id, t.name, t.description, t.source, d.label, td.pages, td.note, td.source AS link_source"
        " FROM themes t LEFT JOIN theme_docs td ON td.theme_id = t.id LEFT JOIN documents d ON d.id = td.doc_id"
        " WHERE t.project_id = ? ORDER BY t.created_at, d.label",
        (project_id,),
    )
    return [dict(r) for r in rows]


def save_theme_edits(ctx: AppContext, project_id: str, rows: list[dict]) -> None:
    """Persist an edited theme table: rows of {theme, description, doc_label, pages, note}.

    Saved themes become researcher-owned (``source = 'researcher'``) so later AI runs keep them.
    """
    labels = {
        r["label"]: r["id"]
        for r in ctx.db.query("SELECT id, label FROM documents WHERE project_id = ?", (project_id,))
    }
    old = list_themes(ctx, project_id)
    with ctx.db.transaction() as c:
        c.execute("DELETE FROM themes WHERE project_id = ?", (project_id,))
        ids: dict[str, str] = {}
        for row in rows:
            name = (row.get("theme") or "").strip()
            if not name:
                continue
            if name not in ids:
                ids[name] = new_id()
                c.execute(
                    "INSERT INTO themes(id, project_id, name, description, source, created_at) VALUES (?, ?, ?, ?, 'researcher', ?)",
                    (ids[name], project_id, name, (row.get("description") or "").strip(), now()),
                )
            label = (row.get("doc_label") or "").strip().upper()
            if label in labels:
                c.execute(
                    "INSERT OR REPLACE INTO theme_docs(theme_id, doc_id, pages, note, source) VALUES (?, ?, ?, ?, 'researcher')",
                    (ids[name], labels[label], str(row.get("pages") or ""), str(row.get("note") or "")),
                )
        c.execute(
            "INSERT INTO edit_history(project_id, entity, entity_id, field, old_value, new_value, changed_by, changed_at)"
            " VALUES (?, 'themes', 'all', 'themes', ?, ?, 'researcher', ?)",
            (project_id, json.dumps(old)[:20000], json.dumps(rows)[:20000], now()),
        )
