"""Literature review matrix: configurable fields, cells, edits and provenance.

Cell statuses
-------------
- ``not_extracted``: no extraction has been run for this field yet.
- ``heuristic``: filled by non-AI heuristics (e.g. PDF metadata); always requires verification.
- ``reported``: value reported in the paper, with verified quotation evidence.
- ``not_reported``: the paper was read and the value was not reported.
- ``not_found``: the value was not found in the *extracted* text (pages may be unreadable).
- ``requires_verification``: a value exists but its evidence could not be verified.
- ``researcher``: entered or corrected by the researcher.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass

from litreview.context import AppContext
from litreview.db import now

MISSING_TEXT = {
    "not_extracted": "Not extracted yet",
    "not_reported": "Not reported",
    "not_found": "Not found in extracted text",
    "requires_verification": "Requires verification",
}

CELL_STATUSES = [
    "not_extracted",
    "heuristic",
    "reported",
    "not_reported",
    "not_found",
    "requires_verification",
    "researcher",
]

DEFAULT_FIELDS: list[tuple[str, str, str]] = [
    ("title", "Title", "Full title of the paper exactly as printed."),
    ("authors", "Authors", "Author names exactly as printed, separated by semicolons."),
    ("year", "Year", "Publication year as printed in the paper (not the PDF creation date)."),
    ("doi", "DOI", "DOI exactly as printed in the paper, if present."),
    ("research_aim", "Research aim / question", "The stated aim, objective or research question."),
    (
        "methods",
        "Methods / design",
        "Study design and main methods (e.g. RCT, cross-sectional survey, simulation, case study).",
    ),
    (
        "population",
        "Setting / population",
        "Population, participants, sample frame, buildings, or setting studied, incl. country.",
    ),
    (
        "sample_size",
        "Sample size",
        "Sample size as reported (n, units, and what was counted). Do not compute it yourself.",
    ),
    ("measures", "Measures / outcomes", "Main variables, outcomes or instruments measured."),
    (
        "findings",
        "Key findings",
        "Main reported results, keeping units, effect sizes, uncertainty and significance as stated.",
    ),
    (
        "limitations",
        "Limitations",
        "Limitations stated by the authors. Do not add your own critique here.",
    ),
]

FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


class MatrixError(Exception):
    pass


@dataclass
class Field:
    key: str
    label: str
    description: str
    position: int
    builtin: bool


@dataclass
class Cell:
    doc_id: str
    field_key: str
    value: str
    status: str
    source: str
    evidence: list[dict]
    reviewed: bool
    ai_suggestion: dict | None


def install_default_fields(conn: sqlite3.Connection, project_id: str) -> None:
    for position, (key, label, description) in enumerate(DEFAULT_FIELDS):
        conn.execute(
            "INSERT OR IGNORE INTO matrix_fields(project_id, key, label, description, position,"
            " builtin) VALUES (?, ?, ?, ?, ?, 1)",
            (project_id, key, label, description, position),
        )


def list_fields(ctx: AppContext, project_id: str) -> list[Field]:
    rows = ctx.db.query("SELECT * FROM matrix_fields WHERE project_id = ? ORDER BY position", (project_id,))
    return [Field(r["key"], r["label"], r["description"], r["position"], bool(r["builtin"])) for r in rows]


def add_field(ctx: AppContext, project_id: str, key: str, label: str, description: str) -> Field:
    key = key.strip().lower()
    if not FIELD_KEY_RE.match(key):
        raise MatrixError("Field key must be lowercase letters, digits or underscores (2-41 chars)")
    if not label.strip() or not description.strip():
        raise MatrixError("Field label and description are required")
    existing = {f.key for f in list_fields(ctx, project_id)}
    if key in existing:
        raise MatrixError(f"Field '{key}' already exists")
    position = len(existing)
    ctx.db.execute(
        "INSERT INTO matrix_fields(project_id, key, label, description, position, builtin)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (project_id, key, label.strip(), description.strip(), position),
    )
    return Field(key, label.strip(), description.strip(), position, False)


def remove_field(ctx: AppContext, project_id: str, key: str) -> None:
    row = ctx.db.query_one(
        "SELECT builtin FROM matrix_fields WHERE project_id = ? AND key = ?", (project_id, key)
    )
    if row is None:
        raise MatrixError("Field not found")
    if row["builtin"]:
        raise MatrixError("Built-in fields cannot be removed")
    with ctx.db.transaction() as c:
        c.execute("DELETE FROM matrix_fields WHERE project_id = ? AND key = ?", (project_id, key))
        c.execute("DELETE FROM matrix_cells WHERE project_id = ? AND field_key = ?", (project_id, key))


def _row_to_cell(row) -> Cell:
    return Cell(
        doc_id=row["doc_id"],
        field_key=row["field_key"],
        value=row["value"],
        status=row["status"],
        source=row["source"],
        evidence=json.loads(row["evidence"] or "[]"),
        reviewed=bool(row["reviewed"]),
        ai_suggestion=json.loads(row["ai_suggestion"]) if row["ai_suggestion"] else None,
    )


def get_cells(ctx: AppContext, project_id: str, doc_id: str | None = None) -> list[Cell]:
    if doc_id:
        rows = ctx.db.query(
            "SELECT * FROM matrix_cells WHERE project_id = ? AND doc_id = ?", (project_id, doc_id)
        )
    else:
        rows = ctx.db.query("SELECT * FROM matrix_cells WHERE project_id = ?", (project_id,))
    return [_row_to_cell(r) for r in rows]


def get_cell(ctx: AppContext, project_id: str, doc_id: str, field_key: str) -> Cell | None:
    row = ctx.db.query_one(
        "SELECT * FROM matrix_cells WHERE project_id = ? AND doc_id = ? AND field_key = ?",
        (project_id, doc_id, field_key),
    )
    return _row_to_cell(row) if row else None


def _record_history(
    conn: sqlite3.Connection,
    project_id: str,
    entity_id: str,
    field: str,
    old: str | None,
    new: str | None,
    by: str,
) -> None:
    conn.execute(
        "INSERT INTO edit_history(project_id, entity, entity_id, field, old_value, new_value,"
        " changed_by, changed_at) VALUES (?, 'matrix_cell', ?, ?, ?, ?, ?, ?)",
        (project_id, entity_id, field, old, new, by, now()),
    )


def researcher_edit(
    ctx: AppContext,
    project_id: str,
    doc_id: str,
    field_key: str,
    value: str,
    reviewed: bool = True,
) -> None:
    """Store a researcher's value. Researcher values are never overwritten by later AI runs."""
    value = value.strip()
    with ctx.db.transaction() as c:
        row = c.execute(
            "SELECT * FROM matrix_cells WHERE project_id = ? AND doc_id = ? AND field_key = ?",
            (project_id, doc_id, field_key),
        ).fetchone()
        old = row["value"] if row else None
        if row is not None and row["value"] == value and bool(row["reviewed"]) == reviewed:
            return
        if row is None:
            c.execute(
                "INSERT INTO matrix_cells(project_id, doc_id, field_key, value, status, source,"
                " evidence, reviewed, updated_at) VALUES (?, ?, ?, ?, 'researcher', 'researcher',"
                " '[]', ?, ?)",
                (project_id, doc_id, field_key, value, int(reviewed), now()),
            )
        else:
            status = row["status"] if row["value"] == value else "researcher"
            source = row["source"] if row["value"] == value else "researcher"
            c.execute(
                "UPDATE matrix_cells SET value = ?, status = ?, source = ?, reviewed = ?,"
                " updated_at = ? WHERE project_id = ? AND doc_id = ? AND field_key = ?",
                (value, status, source, int(reviewed), now(), project_id, doc_id, field_key),
            )
        _record_history(c, project_id, f"{doc_id}:{field_key}", "value", old, value, "researcher")


def set_reviewed(ctx: AppContext, project_id: str, doc_id: str, field_key: str, reviewed: bool) -> None:
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE matrix_cells SET reviewed = ? WHERE project_id = ? AND doc_id = ? AND field_key = ?",
            (int(reviewed), project_id, doc_id, field_key),
        )
        _record_history(c, project_id, f"{doc_id}:{field_key}", "reviewed", None, str(reviewed), "researcher")


def machine_write(
    ctx: AppContext,
    project_id: str,
    doc_id: str,
    field_key: str,
    value: str,
    status: str,
    source: str,
    evidence: list[dict],
    model: str | None = None,
    prompt_version: str | None = None,
) -> str:
    """Write an AI or heuristic value.

    Returns ``"written"`` or ``"suggested"``. If the researcher has edited or reviewed the cell, the
    new value is stored only as ``ai_suggestion`` so reviewed work is never overwritten silently.
    """
    if status not in CELL_STATUSES:
        raise MatrixError(f"Unknown status {status}")
    evidence_json = json.dumps(evidence, ensure_ascii=False)
    with ctx.db.transaction() as c:
        row = c.execute(
            "SELECT * FROM matrix_cells WHERE project_id = ? AND doc_id = ? AND field_key = ?",
            (project_id, doc_id, field_key),
        ).fetchone()
        protected = row is not None and (row["source"] == "researcher" or row["reviewed"])
        # Heuristic values never replace AI-extracted values.
        if row is not None and source == "heuristic" and row["source"] == "ai":
            return "skipped"
        if protected:
            suggestion = json.dumps(
                {"value": value, "status": status, "evidence": evidence, "model": model},
                ensure_ascii=False,
            )
            c.execute(
                "UPDATE matrix_cells SET ai_suggestion = ?, updated_at = ? WHERE project_id = ?"
                " AND doc_id = ? AND field_key = ?",
                (suggestion, now(), project_id, doc_id, field_key),
            )
            return "suggested"
        if row is None:
            c.execute(
                "INSERT INTO matrix_cells(project_id, doc_id, field_key, value, status, source,"
                " evidence, reviewed, model, prompt_version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    project_id,
                    doc_id,
                    field_key,
                    value,
                    status,
                    source,
                    evidence_json,
                    model,
                    prompt_version,
                    now(),
                ),
            )
        else:
            c.execute(
                "UPDATE matrix_cells SET value = ?, status = ?, source = ?, evidence = ?,"
                " model = ?, prompt_version = ?, ai_suggestion = NULL, updated_at = ?"
                " WHERE project_id = ? AND doc_id = ? AND field_key = ?",
                (
                    value,
                    status,
                    source,
                    evidence_json,
                    model,
                    prompt_version,
                    now(),
                    project_id,
                    doc_id,
                    field_key,
                ),
            )
        _record_history(
            c, project_id, f"{doc_id}:{field_key}", "value", row["value"] if row else None, value, source
        )
        return "written"


def accept_suggestion(ctx: AppContext, project_id: str, doc_id: str, field_key: str) -> None:
    cell = get_cell(ctx, project_id, doc_id, field_key)
    if cell is None or cell.ai_suggestion is None:
        raise MatrixError("No AI suggestion for this cell")
    s = cell.ai_suggestion
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE matrix_cells SET value = ?, status = ?, source = 'ai', evidence = ?,"
            " reviewed = 0, ai_suggestion = NULL, updated_at = ? WHERE project_id = ? AND"
            " doc_id = ? AND field_key = ?",
            (
                s["value"],
                s["status"],
                json.dumps(s.get("evidence", []), ensure_ascii=False),
                now(),
                project_id,
                doc_id,
                field_key,
            ),
        )
        _record_history(c, project_id, f"{doc_id}:{field_key}", "value", cell.value, s["value"], "researcher")


def display_value(cell: Cell | None) -> str:
    if cell is None:
        return MISSING_TEXT["not_extracted"]
    if cell.value:
        return cell.value
    return MISSING_TEXT.get(cell.status, "")


def edit_history(ctx: AppContext, project_id: str, limit: int = 200) -> list[dict]:
    rows = ctx.db.query(
        "SELECT * FROM edit_history WHERE project_id = ? ORDER BY id DESC LIMIT ?",
        (project_id, limit),
    )
    return [dict(r) for r in rows]
