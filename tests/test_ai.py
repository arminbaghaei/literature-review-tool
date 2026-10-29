"""AI features with a fake provider (no network, no API key, no cost)."""

from __future__ import annotations

import json
import re

import pytest
from conftest import PAPER_A, PAPER_B, incoming, make_text_pdf

from litreview import export, jobs, matrix
from litreview.ai.client import (
    AINotConfigured,
    AIOutputInvalid,
    AIProviderError,
    AIRefused,
    AIRequest,
    AIService,
    BudgetExceeded,
    ProviderResponse,
    strict_schema,
)
from litreview.ai.prompts import AnswerOutput, ExtractionOutput
from litreview.analysis import qa, runner
from litreview.analysis.drafting import save_output_edit
from litreview.analysis.synthesis import list_themes, save_theme_edits
from litreview.documents import list_documents
from litreview.ingest import ingest_batch
from litreview.projects import update_project
from litreview.worker import Worker

SECRET = "sk-ant-test-SECRET-should-never-appear"


class FakeProvider:
    name = "fake"

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def complete(self, *, model, system, user, schema, max_tokens, effort):
        self.calls.append({"system": system, "user": user, "schema": schema})
        out = self.handler(system, user, schema)
        if isinstance(out, ProviderResponse):
            return out
        return ProviderResponse(json.dumps(out), 2000, 800, "end_turn", "req_test", model)


def label_of(user: str) -> str:
    m = re.search(r'label="(D\d{3})"', user) or re.search(r'doc_label="(D\d{3})"', user)
    return m.group(1) if m else "D001"


def smart_handler(system, user, schema):
    props = schema["properties"]
    label = label_of(user)
    if "fields" in props:  # extraction
        a = label == "D001"
        fields = [
            {
                "key": "title",
                "status": "reported",
                "value": "Effects of Insulation Retrofits on Indoor Temperature in New Zealand Homes"
                if a
                else "Heat Pump Use and Energy Bills in Auckland Rental Housing",
                "evidence": [
                    {
                        "page": 1,
                        "quote": "Effects of Insulation Retrofits" if a else "Heat Pump Use and Energy Bills",
                    }
                ],
            },
            {
                "key": "sample_size",
                "status": "reported",
                "value": "120 households" if a else "64 rental households",
                "evidence": [
                    {"page": 2, "quote": "120 households in Wellington" if a else "64 rental households"}
                ],
            },
            {
                "key": "findings",
                "status": "reported",
                "value": "1.2 C warmer (p<0.05)" if a else "No significant change",
                # Fabricated quote for D002: must be caught.
                "evidence": [{"page": 3, "quote": "1.2 degrees C warmer than control homes"}]
                if a
                else [{"page": 2, "quote": "heat pumps doubled comfort"}],
            },
            {"key": "limitations", "status": "not_reported", "value": "", "evidence": []},
        ]
        items = [
            {
                "category": "finding",
                "statement": "Retrofits warmer" if a else "No significant change",
                "evidence_type": "reported_result",
                "page": 3 if a else 2,
                "quote": "1.2 degrees C warmer than control homes"
                if a
                else "did not significantly change indoor temperature",
            }
        ]
        return {"fields": fields, "evidence_items": items}
    if "sentences" in props:
        return {
            "sentences": [
                {"text": "The study surveyed households.", "kind": "reported", "pages": [2]},
                {"text": "Bad page ref.", "kind": "reported", "pages": [99]},
            ]
        }
    if "relevance" in props:
        if label == "D002":
            return {"relevance": "not_relevant", "claims": [], "notes": ""}
        return {
            "relevance": "relevant",
            "notes": "",
            "claims": [
                {
                    "text": "Retrofits warmed homes",
                    "kind": "reported",
                    "citations": [
                        {"doc_label": label, "page": 3, "quote": "1.2 degrees C warmer than control homes"}
                    ],
                }
            ],
        }
    if "answer_status" in props:
        return {
            "answer_status": "answered",
            "conflicts_or_caveats": ["Only one city"],
            "missing_information": [],
            "claims": [
                {
                    "text": "Retrofitted homes were warmer",
                    "kind": "reported",
                    "citations": [
                        {"doc_label": "D001", "page": 3, "quote": "1.2 degrees C warmer than control homes"}
                    ],
                },
                {
                    "text": "Invented claim",
                    "kind": "reported",
                    "citations": [{"doc_label": "D777", "page": 1, "quote": "made up"}],
                },
            ],
        }
    if "themes" in props:
        return {
            "themes": [
                {
                    "name": "Thermal comfort",
                    "description": "Indoor temperature outcomes",
                    "docs": [
                        {"doc_label": "D001", "pages": [3], "note": ""},
                        {"doc_label": "D002", "pages": [2, 50], "note": ""},
                        {"doc_label": "D999", "pages": [1], "note": "fake"},
                    ],
                }
            ],
            "comparisons": [
                {
                    "aspect": "Design",
                    "summary": "Survey vs RCT",
                    "docs": [{"doc_label": "D001", "pages": [2], "note": ""}],
                    "comparability_caveats": "Different cities",
                }
            ],
            "conflicts": [
                {
                    "topic": "Effect on temperature",
                    "possible_explanations": "Different interventions",
                    "positions": [
                        {"position": "Warmer", "docs": [{"doc_label": "D001", "pages": [3], "note": ""}]},
                        {
                            "position": "No significant change",
                            "docs": [{"doc_label": "D002", "pages": [2], "note": ""}],
                        },
                    ],
                }
            ],
            "gaps": [
                {
                    "gap": "No longitudinal studies",
                    "rationale": "Both designs short",
                    "confidence": "medium",
                    "supporting_docs": [{"doc_label": "D001", "pages": [2], "note": ""}],
                }
            ],
            "notes": "",
        }
    if "markdown" in props:
        return {
            "markdown": 'Retrofits were associated with warmer homes, "1.2 degrees C warmer than control homes" [D001 p.3]. '
            "A trial found no significant change [D002 p.2]. Unsupported claim [D001 p.40].",
            "unsupported_points": ["Cost-effectiveness"],
        }
    raise AssertionError("unexpected schema")


@pytest.fixture
def fake(monkeypatch):
    provider = FakeProvider(smart_handler)
    monkeypatch.setattr(runner, "provider_factory", lambda: provider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    return provider


@pytest.fixture
def loaded(ctx, project, pdf_dir):
    files = [make_text_pdf(pdf_dir / "a.pdf", PAPER_A), make_text_pdf(pdf_dir / "b.pdf", PAPER_B)]
    ingest_batch(ctx, project.id, [incoming(f) for f in files])
    Worker(ctx).run_until_idle(timeout=120)
    update_project(ctx, project.id, ai_budget_usd=5.0)
    return project


def _run(ctx, project, kind, params=None, doc_ids=None, budget=1.0):
    est = runner.estimate(ctx, project.id, kind, params or {}, doc_ids)
    run_id = runner.start_run(ctx, project.id, kind, params or {}, doc_ids, budget, est["estimated_cost_usd"])
    Worker(ctx).run_until_idle(timeout=120)
    return jobs.get_run(ctx, run_id)


def test_missing_key_reports_not_configured(ctx, project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    ai = AIService(ctx)
    assert not ai.available()
    with pytest.raises(AINotConfigured):
        ai.call(project.id, AIRequest("x", "s", "u", AnswerOutput, "v"))
    with pytest.raises(runner.RunError):
        runner.start_run(ctx, project.id, runner.RUN_MATRIX, {}, None, 1.0, 0.1)


def test_budget_cache_refusal_truncation_and_invalid_output(ctx, project):
    update_project(ctx, project.id, ai_budget_usd=1.0)
    good = {"answer_status": "answered", "claims": [], "conflicts_or_caveats": [], "missing_information": []}
    state = {"mode": "ok"}

    def handler(system, user, schema):
        if state["mode"] == "refuse":
            return ProviderResponse("", 100, 0, "refusal", None, "claude-opus-5-5")
        if state["mode"] == "truncate":
            return ProviderResponse(
                '{"answer_status": "ans', 100, 16000, "max_tokens", None, "claude-opus-5-5"
            )
        if state["mode"] == "garbage":
            return ProviderResponse("not json", 100, 10, "end_turn", None, "claude-opus-5-5")
        return good

    provider = FakeProvider(handler)
    ai = AIService(ctx, provider)
    req = AIRequest("t", "sys", "question one", AnswerOutput, "v1")
    first = ai.call(project.id, req)
    assert not first.cached and first.cost_usd > 0
    again = ai.call(project.id, req)
    assert again.cached and again.cost_usd == 0 and len(provider.calls) == 1  # no double charge

    for mode, exc in (("refuse", AIRefused), ("truncate", AIOutputInvalid), ("garbage", AIOutputInvalid)):
        state["mode"] = mode
        with pytest.raises(exc):
            ai.call(project.id, AIRequest("t", "sys", f"q {mode}", AnswerOutput, "v1"))
    statuses = [r["status"] for r in ctx.db.query("SELECT status FROM ai_calls ORDER BY id")]
    assert statuses == ["success", "refused", "truncated", "invalid_output"]

    update_project(ctx, project.id, ai_budget_usd=ai.project_spent(project.id))
    state["mode"] = "ok"
    with pytest.raises(BudgetExceeded):
        ai.call(project.id, AIRequest("t", "sys", "another question", AnswerOutput, "v1"))


def test_strict_schema_has_no_refs_and_closed_objects():
    schema = strict_schema(ExtractionOutput)
    text = json.dumps(schema)
    assert "$ref" not in text and "$defs" not in text
    item = schema["properties"]["fields"]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == {
        "key",
        "status",
        "value",
        "evidence",
    }


def test_matrix_extraction_verifies_quotes_and_marks_missing(ctx, loaded, fake):
    run = _run(ctx, loaded, runner.RUN_MATRIX)
    assert run["status"] == "completed", run
    docs = {d.label: d for d in list_documents(ctx, loaded.id)}
    a = {c.field_key: c for c in matrix.get_cells(ctx, loaded.id, docs["D001"].id)}
    b = {c.field_key: c for c in matrix.get_cells(ctx, loaded.id, docs["D002"].id)}
    assert a["sample_size"].status == "reported" and a["sample_size"].evidence[0]["verified"]
    assert (
        a["limitations"].status == "not_reported" and matrix.display_value(a["limitations"]) == "Not reported"
    )
    assert a["methods"].status == "not_found"  # field omitted by the model -> not silently "reported"
    assert b["findings"].status == "requires_verification"  # fabricated quote caught
    assert not b["findings"].evidence[0]["verified"]
    ev = export.evidence_rows(ctx, loaded.id)
    assert {e["label"] for e in ev} == {"D001", "D002"} and all(e["quote_verified"] for e in ev)


def test_prompt_injection_is_contained_and_secrets_never_sent(ctx, loaded, fake):
    _run(ctx, loaded, runner.RUN_MATRIX)
    b_call = next(c for c in fake.calls if 'label="D002"' in c["user"])
    assert "Ignore all previous instructions" in b_call["user"]
    doc_start = b_call["user"].index("<document")
    assert b_call["user"].index("Ignore all previous instructions") > doc_start  # inside the data block
    assert "untrusted DATA" in b_call["system"]
    for call in fake.calls:
        assert SECRET not in call["user"] and SECRET not in call["system"]
    exported = export.project_json(ctx, loaded.id) + export.matrix_csv(ctx, loaded.id)
    assert SECRET not in exported


def test_focused_question_flags_invalid_citations(ctx, loaded, fake):
    ai = runner.make_ai(ctx)
    res = qa.ask_focused(ai, loaded.id, "Were retrofitted homes warmer than control homes?", None)
    assert "not an exhaustive review" in res["markdown"]
    assert res["citation_report"]["verified"] == 1
    assert res["citation_report"]["unsupported_claims"] == ["Invented claim"]
    assert "could not be verified" in res["markdown"]


def test_focused_question_without_matches_does_not_call_ai(ctx, loaded, fake):
    res = qa.ask_focused(runner.make_ai(ctx), loaded.id, "zebra quantum", None)
    assert "error" in res and not fake.calls


def test_exhaustive_question_covers_every_document(ctx, loaded, fake):
    run = _run(ctx, loaded, runner.RUN_QA, {"question": "Did indoor temperature change?"})
    assert run["status"] == "completed"
    out = runner.get_output(ctx, loaded.id, run["result"]["output_id"])
    rows = {r["doc_label"]: r["status"] for r in out["coverage"]["rows"]}
    assert rows == {"D001": "relevant", "D002": "not_relevant"}
    assert "Per-document coverage" in out["content_md"]


def test_exhaustive_reports_unprocessed_documents(ctx, loaded, monkeypatch):
    def flaky(system, user, schema):
        if "relevance" in schema["properties"] and 'label="D002"' in user:
            raise AIProviderError("API error (HTTP 400).", False)
        return smart_handler(system, user, schema)

    provider = FakeProvider(flaky)
    monkeypatch.setattr(runner, "provider_factory", lambda: provider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    run = _run(ctx, loaded, runner.RUN_QA, {"question": "Did indoor temperature change?"})
    assert run["status"] == "completed_with_failures"
    out = runner.get_output(ctx, loaded.id, run["result"]["output_id"])
    rows = {r["doc_label"]: r["status"] for r in out["coverage"]["rows"]}
    assert rows["D002"].startswith("not processed")


def test_rate_limit_is_retried_then_reported(ctx, loaded, monkeypatch):
    attempts = {"n": 0}

    def limited(system, user, schema):
        attempts["n"] += 1
        raise AIProviderError("Rate limited by the API after retries. Try again later.", True)

    monkeypatch.setattr(runner, "provider_factory", lambda: FakeProvider(limited))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    docs = list_documents(ctx, loaded.id)
    run = _run(ctx, loaded, runner.RUN_MATRIX, doc_ids=[docs[0].id])
    assert attempts["n"] == jobs.MAX_AUTO_ATTEMPTS
    assert run["status"] == "completed_with_failures"
    assert "Rate limited" in run["result"]["failures"][0]["error"]


def test_run_budget_stops_remaining_documents(ctx, loaded, fake):
    run = _run(ctx, loaded, runner.RUN_MATRIX, budget=0.0001)
    assert run["status"] == "stopped_budget"
    failed = ctx.db.query("SELECT error FROM jobs WHERE run_id = ? AND status = 'failed'", (run["id"],))
    assert failed and all("budget" in r["error"].lower() for r in failed)


def test_synthesis_and_draft_validate_citations(ctx, loaded, fake):
    _run(ctx, loaded, runner.RUN_MATRIX)
    syn = _run(ctx, loaded, runner.RUN_SYNTHESIS, {"focus": "temperature"})
    out = runner.get_output(ctx, loaded.id, syn["result"]["output_id"])
    problems = out["citation_report"]["problems"]
    assert any(p["citation"] == "D999" for p in problems) and any("p.50" in p["citation"] for p in problems)
    assert "Conflicting findings" in out["content_md"] and "[D002 p.2]" in out["content_md"]
    themes = list_themes(ctx, loaded.id)
    assert {t["label"] for t in themes} == {"D001", "D002"}

    # Researcher edits to themes survive the next AI synthesis.
    save_theme_edits(
        ctx,
        loaded.id,
        [{"theme": "My theme", "description": "", "doc_label": "D001", "pages": "3", "note": ""}],
    )
    _run(ctx, loaded, runner.RUN_SYNTHESIS, {"focus": "temperature again"})
    names = {t["name"] for t in list_themes(ctx, loaded.id)}
    assert "My theme" in names and "Thermal comfort" in names

    draft = _run(
        ctx,
        loaded,
        runner.RUN_DRAFT,
        {"section_title": "Thermal outcomes", "synthesis_output_id": syn["result"]["output_id"]},
    )
    out = runner.get_output(ctx, loaded.id, draft["result"]["output_id"])
    report = out["citation_report"]
    assert report["valid"] == 2 and report["invalid"][0]["citation"] == "[D001 p.40]"
    assert not report["quotes_unverified"]
    assert "Sources cited" in out["content_md"] and "**D001**" in out["content_md"]
    assert "Cost-effectiveness" in out["content_md"]

    report2 = save_output_edit(ctx, loaded.id, out["id"], out["content_md"] + "\n\nNew claim [D003 p.1].")
    assert any("D003" in i["citation"] for i in report2["invalid"])
    md = export.output_markdown(ctx, loaded.id, runner.get_output(ctx, loaded.id, out["id"]))
    assert md.startswith("<!-- Exported") and "researcher_edited" in md


def test_summaries_flag_bad_pages_and_respect_researcher_text(ctx, loaded, fake):
    from litreview.analysis.summaries import get_summary, save_researcher_summary

    docs = list_documents(ctx, loaded.id)
    save_researcher_summary(ctx, loaded.id, docs[1].id, "My own summary")
    run = _run(ctx, loaded, runner.RUN_SUMMARIES)
    assert run["status"] == "completed"
    s1 = get_summary(ctx, loaded.id, docs[0].id)
    assert (
        "[D001 p.2]" in s1["text"] and "no valid page citation" in s1["text"] and "AI-generated" in s1["text"]
    )
    s2 = get_summary(ctx, loaded.id, docs[1].id)
    assert s2["text"] == "My own summary" and s2["ai_suggestion"]


def test_exports_include_coverage_and_status(ctx, loaded, fake):
    _run(ctx, loaded, runner.RUN_MATRIX)
    csv_text = export.matrix_csv(ctx, loaded.id)
    header = csv_text.splitlines()[0]
    for col in (
        "doc_id",
        "label",
        "coverage",
        "sample_size__status",
        "sample_size__pages",
        "sample_size__reviewed",
    ):
        assert col in header
    data = json.loads(export.project_json(ctx, loaded.id))
    assert data["coverage"]["documents"] == 2 and data["matrix"][0]["sample_size__evidence"]
    assert export.evidence_csv(ctx, loaded.id).startswith("evidence_id,")


def test_unexpected_finalize_crash_marks_run_failed(ctx, loaded, fake, monkeypatch):
    from litreview.analysis import synthesis

    def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    _run(ctx, loaded, runner.RUN_MATRIX)
    monkeypatch.setattr(synthesis, "run_synthesis", boom)
    run = _run(ctx, loaded, runner.RUN_SYNTHESIS, {"focus": "x"})
    assert run["status"] == "failed" and "unexpected" in run["error"]
