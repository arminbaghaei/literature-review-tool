"""Persisted job queue.

Job statuses: ``queued`` -> ``running`` -> ``done`` | ``failed`` | ``cancelled``.
``cancel_requested`` asks a running job to stop at its next checkpoint.
Jobs left ``running`` by a dead worker are re-queued by :func:`recover_stale_jobs`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from litreview.context import AppContext
from litreview.db import new_id, now

ACTIVE_STATUSES = ("queued", "running", "cancel_requested")
WORKER_STALE_SECONDS = 30
MAX_AUTO_ATTEMPTS = 3

JOB_EXTRACT = "extract_pdf"
JOB_AI_EXTRACT = "ai_extract_doc"
JOB_AI_SUMMARY = "ai_summary_doc"
JOB_AI_QA_DOC = "ai_qa_doc"
JOB_RUN_FINALIZE = "run_finalize"


@dataclass
class Job:
    id: int
    project_id: str
    run_id: str | None
    kind: str
    doc_id: str | None
    status: str
    attempts: int
    progress: int
    total: int
    error: str | None


def _row_to_job(row) -> Job:
    return Job(
        id=row["id"],
        project_id=row["project_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        doc_id=row["doc_id"],
        status=row["status"],
        attempts=row["attempts"],
        progress=row["progress"],
        total=row["total"],
        error=row["error"],
    )


def enqueue(
    conn: sqlite3.Connection,
    project_id: str,
    kind: str,
    doc_id: str | None = None,
    run_id: str | None = None,
) -> int:
    existing = conn.execute(
        "SELECT id FROM jobs WHERE project_id = ? AND kind = ? AND IFNULL(doc_id, '') = ?"
        " AND IFNULL(run_id, '') = ? AND status IN ('queued', 'running', 'cancel_requested')",
        (project_id, kind, doc_id or "", run_id or ""),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        "INSERT INTO jobs(project_id, run_id, kind, doc_id, status, created_at)"
        " VALUES (?, ?, ?, ?, 'queued', ?)",
        (project_id, run_id, kind, doc_id, now()),
    )
    return int(cur.lastrowid)


def claim_next(ctx: AppContext, kinds: tuple[str, ...], worker_pid: int) -> Job | None:
    placeholders = ",".join("?" for _ in kinds)
    with ctx.db.transaction() as c:
        row = c.execute(
            f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN ({placeholders}) ORDER BY id LIMIT 1",
            kinds,
        ).fetchone()
        if row is None:
            return None
        c.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, worker_pid = ?,"
            " started_at = ?, heartbeat_at = ?, error = NULL WHERE id = ?",
            (worker_pid, now(), now(), row["id"]),
        )
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    return _row_to_job(row)


def get_job(ctx: AppContext, job_id: int) -> Job | None:
    row = ctx.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return _row_to_job(row) if row else None


def heartbeat(ctx: AppContext, job_id: int, progress: int | None = None, total: int | None = None) -> str:
    """Update job heartbeat/progress and return the current status (to detect cancellation)."""
    with ctx.db.transaction() as c:
        if progress is not None and total is not None:
            c.execute(
                "UPDATE jobs SET heartbeat_at = ?, progress = ?, total = ? WHERE id = ?",
                (now(), progress, total, job_id),
            )
        else:
            c.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (now(), job_id))
        row = c.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return row["status"] if row else "cancelled"


def finish(
    ctx: AppContext, job_id: int, status: str, error: str | None = None, result: dict | None = None
) -> None:
    ctx.db.execute(
        "UPDATE jobs SET status = ?, error = ?, result = ?, finished_at = ? WHERE id = ?",
        (status, error, json.dumps(result) if result is not None else None, now(), job_id),
    )


def request_cancel(ctx: AppContext, project_id: str, job_ids: list[int] | None = None) -> int:
    """Cancel queued jobs immediately and ask running jobs to stop at the next checkpoint."""
    with ctx.db.transaction() as c:
        filt = ""
        params: list = [project_id]
        if job_ids:
            filt = f" AND id IN ({','.join('?' for _ in job_ids)})"
            params += job_ids
        n1 = c.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ?, error = 'Cancelled by user'"
            " WHERE project_id = ? AND status = 'queued'" + filt,
            [now(), *params],
        ).rowcount
        n2 = c.execute(
            "UPDATE jobs SET status = 'cancel_requested' WHERE project_id = ? AND status = 'running'" + filt,
            params,
        ).rowcount
        c.execute(
            "UPDATE documents SET status = 'cancelled', updated_at = ? WHERE project_id = ? AND"
            " status = 'queued' AND id IN (SELECT doc_id FROM jobs WHERE project_id = ? AND"
            " kind = ? AND status = 'cancelled')",
            (now(), project_id, project_id, JOB_EXTRACT),
        )
    return n1 + n2


def recover_stale_jobs(ctx: AppContext, stale_seconds: float = WORKER_STALE_SECONDS) -> int:
    """Re-queue jobs whose worker stopped heartbeating (crash, restart, power loss).

    Extraction jobs resume from the last committed page. Jobs that have exhausted their automatic
    attempts are marked failed so a poison file cannot loop forever; the user can retry them.
    """
    cutoff = now() - stale_seconds
    recovered = 0
    with ctx.db.transaction() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE status IN ('running', 'cancel_requested') AND"
            " (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (cutoff,),
        ).fetchall()
        for row in rows:
            if row["status"] == "cancel_requested":
                c.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                    (now(), row["id"]),
                )
                if row["kind"] == JOB_EXTRACT and row["doc_id"]:
                    c.execute(
                        "UPDATE documents SET status = 'cancelled', updated_at = ? WHERE id = ?",
                        (now(), row["doc_id"]),
                    )
            elif row["attempts"] >= MAX_AUTO_ATTEMPTS:
                c.execute(
                    "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                    (now(), "Interrupted repeatedly (worker crash or timeout); retry manually", row["id"]),
                )
                if row["kind"] == JOB_EXTRACT and row["doc_id"]:
                    c.execute(
                        "UPDATE documents SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                        ("Processing was interrupted repeatedly", now(), row["doc_id"]),
                    )
            else:
                c.execute("UPDATE jobs SET status = 'queued', worker_pid = NULL WHERE id = ?", (row["id"],))
                recovered += 1
    return recovered


def retry_job(ctx: AppContext, project_id: str, job_id: int) -> None:
    with ctx.db.transaction() as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ? AND project_id = ?", (job_id, project_id)).fetchone()
        if row is None or row["status"] not in ("failed", "cancelled"):
            return
        c.execute(
            "UPDATE jobs SET status = 'queued', attempts = 0, error = NULL, finished_at = NULL WHERE id = ?",
            (job_id,),
        )
        if row["kind"] == JOB_EXTRACT and row["doc_id"]:
            c.execute(
                "UPDATE documents SET status = 'queued', error = NULL, updated_at = ? WHERE id = ?",
                (now(), row["doc_id"]),
            )


def list_jobs(ctx: AppContext, project_id: str, kinds: tuple[str, ...] | None = None) -> list[Job]:
    if kinds:
        placeholders = ",".join("?" for _ in kinds)
        rows = ctx.db.query(
            f"SELECT * FROM jobs WHERE project_id = ? AND kind IN ({placeholders}) ORDER BY id",
            (project_id, *kinds),
        )
    else:
        rows = ctx.db.query("SELECT * FROM jobs WHERE project_id = ? ORDER BY id", (project_id,))
    return [_row_to_job(r) for r in rows]


def job_counts(ctx: AppContext, project_id: str, kinds: tuple[str, ...]) -> dict[str, int]:
    placeholders = ",".join("?" for _ in kinds)
    rows = ctx.db.query(
        f"SELECT status, COUNT(*) AS n FROM jobs WHERE project_id = ? AND kind IN ({placeholders})"
        " GROUP BY status",
        (project_id, *kinds),
    )
    return {r["status"]: r["n"] for r in rows}


# ---- Runs (multi-document AI work with an approved budget) -------------------------------------


def create_run(
    ctx: AppContext,
    project_id: str,
    kind: str,
    params: dict,
    doc_ids: list[str],
    job_kind: str,
    estimated_cost_usd: float,
    approved_budget_usd: float,
    finalize: bool,
) -> str:
    run_id = new_id()
    with ctx.db.transaction() as c:
        c.execute(
            "INSERT INTO runs(id, project_id, kind, status, params, approved_budget_usd,"
            " estimated_cost_usd, created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
            (
                run_id,
                project_id,
                kind,
                json.dumps(params),
                approved_budget_usd,
                estimated_cost_usd,
                now(),
                now(),
            ),
        )
        for doc_id in doc_ids:
            enqueue(c, project_id, job_kind, doc_id=doc_id, run_id=run_id)
        if finalize:
            # Finalize job waits until all per-document jobs of the run are finished.
            c.execute(
                "INSERT INTO jobs(project_id, run_id, kind, status, created_at)"
                " VALUES (?, ?, ?, 'waiting', ?)",
                (project_id, run_id, JOB_RUN_FINALIZE, now()),
            )
    return run_id


def get_run(ctx: AppContext, run_id: str) -> dict | None:
    row = ctx.db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    if row is None:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"] or "{}")
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def list_runs(ctx: AppContext, project_id: str) -> list[dict]:
    rows = ctx.db.query("SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
    out = []
    for row in rows:
        d = dict(row)
        d["params"] = json.loads(d["params"] or "{}")
        out.append(d)
    return out


def release_waiting_finalizers(ctx: AppContext) -> None:
    """Queue finalize jobs whose per-document jobs have all finished."""
    with ctx.db.transaction() as c:
        rows = c.execute(
            "SELECT j.id, j.run_id FROM jobs j WHERE j.status = 'waiting' AND j.kind = ?",
            (JOB_RUN_FINALIZE,),
        ).fetchall()
        for row in rows:
            pending = c.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE run_id = ? AND kind != ? AND status IN"
                " ('queued', 'running', 'cancel_requested')",
                (row["run_id"], JOB_RUN_FINALIZE),
            ).fetchone()["n"]
            if pending == 0:
                c.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (row["id"],))


def set_run_status(
    ctx: AppContext, run_id: str, status: str, error: str | None = None, result: dict | None = None
) -> None:
    ctx.db.execute(
        "UPDATE runs SET status = ?, error = COALESCE(?, error), result = COALESCE(?, result),"
        " updated_at = ? WHERE id = ?",
        (status, error, json.dumps(result) if result is not None else None, now(), run_id),
    )


def cancel_run(ctx: AppContext, project_id: str, run_id: str) -> None:
    with ctx.db.transaction() as c:
        c.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ?, error = 'Cancelled by user'"
            " WHERE run_id = ? AND project_id = ? AND status IN ('queued', 'waiting')",
            (now(), run_id, project_id),
        )
        c.execute(
            "UPDATE jobs SET status = 'cancel_requested' WHERE run_id = ? AND project_id = ? AND"
            " status = 'running'",
            (run_id, project_id),
        )
        c.execute(
            "UPDATE runs SET status = 'cancelled', updated_at = ? WHERE id = ? AND project_id = ?",
            (now(), run_id, project_id),
        )


# ---- Worker liveness ---------------------------------------------------------------------------


def register_worker(ctx: AppContext, worker_id: str) -> None:
    ctx.db.execute(
        "INSERT OR REPLACE INTO workers(id, pid, started_at, heartbeat_at) VALUES (?, ?, ?, ?)",
        (worker_id, os.getpid(), now(), now()),
    )


def worker_heartbeat(ctx: AppContext, worker_id: str) -> None:
    ctx.db.execute("UPDATE workers SET heartbeat_at = ? WHERE id = ?", (now(), worker_id))


def unregister_worker(ctx: AppContext, worker_id: str) -> None:
    ctx.db.execute("DELETE FROM workers WHERE id = ?", (worker_id,))


def worker_alive(ctx: AppContext, stale_seconds: float = WORKER_STALE_SECONDS) -> bool:
    row = ctx.db.query_one(
        "SELECT MAX(heartbeat_at) AS hb FROM workers WHERE heartbeat_at > ?", (now() - stale_seconds,)
    )
    return bool(row and row["hb"])
