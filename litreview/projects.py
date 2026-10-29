"""Project records: create, list, update and delete."""

from __future__ import annotations

from dataclasses import dataclass

from litreview.context import AppContext
from litreview.db import new_id, now
from litreview.matrix import install_default_fields


class ProjectError(Exception):
    pass


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    review_question: str
    ai_budget_usd: float
    created_at: float


def _row_to_project(row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        review_question=row["review_question"],
        ai_budget_usd=row["ai_budget_usd"],
        created_at=row["created_at"],
    )


def create_project(ctx: AppContext, name: str, review_question: str = "") -> Project:
    name = name.strip()
    if not name:
        raise ProjectError("Project name is required")
    if len(name) > 200:
        raise ProjectError("Project name is too long")
    project_id = new_id()
    ts = now()
    with ctx.db.transaction() as c:
        c.execute(
            "INSERT INTO projects(id, name, review_question, ai_budget_usd, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, name, review_question.strip(), ctx.settings.ai_default_budget_usd, ts, ts),
        )
        install_default_fields(c, project_id)
    ctx.files.originals_dir(project_id)
    return get_project(ctx, project_id)


def list_projects(ctx: AppContext) -> list[Project]:
    rows = ctx.db.query("SELECT * FROM projects ORDER BY updated_at DESC")
    return [_row_to_project(r) for r in rows]


def get_project(ctx: AppContext, project_id: str) -> Project:
    row = ctx.db.query_one("SELECT * FROM projects WHERE id = ?", (project_id,))
    if row is None:
        raise ProjectError("Project not found")
    return _row_to_project(row)


def update_project(
    ctx: AppContext,
    project_id: str,
    *,
    name: str | None = None,
    review_question: str | None = None,
    ai_budget_usd: float | None = None,
) -> Project:
    project = get_project(ctx, project_id)
    if ai_budget_usd is not None and ai_budget_usd < 0:
        raise ProjectError("Budget cannot be negative")
    ctx.db.execute(
        "UPDATE projects SET name = ?, review_question = ?, ai_budget_usd = ?, updated_at = ? WHERE id = ?",
        (
            (name or project.name).strip(),
            project.review_question if review_question is None else review_question.strip(),
            project.ai_budget_usd if ai_budget_usd is None else float(ai_budget_usd),
            now(),
            project_id,
        ),
    )
    return get_project(ctx, project_id)


def touch_project(ctx: AppContext, project_id: str) -> None:
    ctx.db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now(), project_id))


def delete_project(ctx: AppContext, project_id: str) -> None:
    """Delete a project, its documents, derived records and local files.

    Running jobs are cancelled first. The FTS index rows are removed by the chunk delete trigger.
    Data already sent to an external AI provider is subject to that provider's retention policy and
    cannot be deleted from here.
    """
    get_project(ctx, project_id)
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE project_id = ?"
            " AND status IN ('queued', 'running', 'cancel_requested')",
            (now(), project_id),
        )
        # Delete chunks explicitly so the FTS delete trigger fires for each row.
        c.execute("DELETE FROM chunks WHERE project_id = ?", (project_id,))
        c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    ctx.files.delete_project_files(project_id)
