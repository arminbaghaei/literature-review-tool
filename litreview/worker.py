"""Background worker.

Start with ``python -m litreview.worker`` (the Streamlit app starts one automatically).

- PDF extraction: one child process per document, at most ``EXTRACTION_WORKERS`` at once, each
  with a wall-clock timeout. A crashing or hanging parser only fails its own document.
- AI jobs: a bounded thread pool (``AI_WORKERS``); calls are I/O bound.
- State lives in SQLite. The worker heartbeats every job it supervises; if the worker dies, the
  next worker re-queues those jobs and extraction resumes from the last committed page.
- A lock file guarantees a single worker per data directory.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from litreview import jobs
from litreview.context import AppContext, create_context
from litreview.db import new_id, now

log = logging.getLogger("litreview.worker")

AI_JOB_KINDS = (jobs.JOB_AI_EXTRACT, jobs.JOB_AI_SUMMARY, jobs.JOB_AI_QA_DOC, jobs.JOB_RUN_FINALIZE)
CANCEL_GRACE_SECONDS = 20
LOCK_WAIT_SECONDS = 20
_last_spawn = -1e9


class WorkerLock:
    """Exclusive, process-lifetime lock on ``<data_dir>/worker.lock``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - held for the process lifetime
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


@dataclass
class ActiveExtraction:
    job_id: int
    doc_id: str
    project_id: str
    process: mp.Process
    started: float
    cancel_seen_at: float | None = None


class Worker:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.worker_id = new_id()
        self.active: dict[int, ActiveExtraction] = {}
        self.ai_pool = ThreadPoolExecutor(max_workers=ctx.settings.ai_workers)
        self.ai_futures: dict[int, Future] = {}
        self._mp = mp.get_context("spawn")

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        jobs.register_worker(self.ctx, self.worker_id)
        # Any job marked running belongs to a dead worker: we hold the single-worker lock.
        recovered = jobs.recover_stale_jobs(self.ctx, stale_seconds=0)
        if recovered:
            log.info("Re-queued %d interrupted job(s)", recovered)

    def stop(self) -> None:
        for active in list(self.active.values()):
            if active.process.is_alive():
                active.process.terminate()
                active.process.join(5)
        self.ai_pool.shutdown(wait=True, cancel_futures=False)
        jobs.unregister_worker(self.ctx, self.worker_id)

    def run_forever(self, poll_seconds: float = 1.0, idle_exit_seconds: float | None = None) -> None:
        self.start()
        idle_since = time.monotonic()
        try:
            while True:
                busy = self.tick()
                if busy:
                    idle_since = time.monotonic()
                elif idle_exit_seconds is not None and time.monotonic() - idle_since > idle_exit_seconds:
                    return
                time.sleep(poll_seconds)
        finally:
            self.stop()

    def run_until_idle(self, timeout: float = 600, poll_seconds: float = 0.2) -> None:
        """Process until no work remains (used by tests and the CLI)."""
        self.start()
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if not self.tick():
                    return
                time.sleep(poll_seconds)
            raise TimeoutError("Worker did not become idle in time")
        finally:
            self.stop()

    # -- main loop -------------------------------------------------------------------------------

    def tick(self) -> bool:
        jobs.worker_heartbeat(self.ctx, self.worker_id)
        self._supervise_extractions()
        self._reap_ai()
        jobs.release_waiting_finalizers(self.ctx)
        while len(self.active) < self.ctx.settings.extraction_workers:
            job = jobs.claim_next(self.ctx, (jobs.JOB_EXTRACT,), os.getpid())
            if job is None:
                break
            self._start_extraction(job)
        while len(self.ai_futures) < self.ctx.settings.ai_workers:
            job = jobs.claim_next(self.ctx, AI_JOB_KINDS, os.getpid())
            if job is None:
                break
            self.ai_futures[job.id] = self.ai_pool.submit(self._run_ai_job, job)
        if self.active or self.ai_futures:
            return True
        waiting = self.ctx.db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued', 'running', 'cancel_requested')"
        )
        return bool(waiting and waiting["n"])

    def _start_extraction(self, job: jobs.Job) -> None:
        from litreview.pdf_extract import run_extraction_child

        process = self._mp.Process(
            target=run_extraction_child,
            args=(self.ctx.settings, job.project_id, job.doc_id, job.id),
            daemon=True,
        )
        process.start()
        self.active[job.id] = ActiveExtraction(job.id, job.doc_id, job.project_id, process, time.monotonic())

    def _supervise_extractions(self) -> None:
        timeout = self.ctx.settings.document_timeout_seconds
        for job_id, active in list(self.active.items()):
            proc = active.process
            job = jobs.get_job(self.ctx, job_id)
            status = job.status if job else "cancelled"
            if proc.is_alive():
                elapsed = time.monotonic() - active.started
                if elapsed > timeout:
                    self._kill(
                        active,
                        "failed",
                        f"Timed out after {timeout} s; completed pages kept, retry to resume",
                    )
                    continue
                if status == "cancel_requested":
                    active.cancel_seen_at = active.cancel_seen_at or time.monotonic()
                    if time.monotonic() - active.cancel_seen_at > CANCEL_GRACE_SECONDS:
                        self._kill(active, "cancelled", "Cancelled by user; completed pages kept")
                        continue
                # Keep the job's heartbeat fresh while the child is alive.
                self.ctx.db.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (now(), job_id))
                continue
            proc.join(1)
            del self.active[job_id]
            if status in ("running", "cancel_requested"):
                # The child died without recording an outcome (hard crash, out of memory).
                code = proc.exitcode
                msg = f"PDF parser process exited unexpectedly (exit code {code}); retry or check the file"
                jobs.finish(self.ctx, job_id, "failed", error=msg)
                self.ctx.db.execute(
                    "UPDATE documents SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                    (msg, now(), active.doc_id),
                )

    def _kill(self, active: ActiveExtraction, status: str, message: str) -> None:
        active.process.terminate()
        active.process.join(10)
        if active.process.is_alive():
            active.process.kill()
            active.process.join(5)
        del self.active[active.job_id]
        jobs.finish(self.ctx, active.job_id, status, error=message)
        doc_status = "cancelled" if status == "cancelled" else "failed"
        self.ctx.db.execute(
            "UPDATE documents SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (doc_status, message, now(), active.doc_id),
        )

    def _run_ai_job(self, job: jobs.Job) -> None:
        from litreview.analysis.runner import handle_ai_job

        try:
            handle_ai_job(self.ctx, job)
        except Exception as exc:
            log.exception("AI job %s failed", job.id)
            message = f"{type(exc).__name__}: {exc}"
            jobs.finish(self.ctx, job.id, "failed", error=message)
            if job.kind == jobs.JOB_RUN_FINALIZE and job.run_id:
                jobs.set_run_status(self.ctx, job.run_id, "failed", error=message)

    def _reap_ai(self) -> None:
        for job_id, fut in list(self.ai_futures.items()):
            if fut.done():
                del self.ai_futures[job_id]
            else:
                self.ctx.db.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (now(), job_id))


def ensure_worker_running(ctx: AppContext) -> bool:
    """Start a detached worker process if none is alive. Returns True if one was started.

    Set ``LITREVIEW_NO_WORKER_AUTOSTART=1`` to manage the worker yourself (tests, services).
    """
    global _last_spawn
    if os.environ.get("LITREVIEW_NO_WORKER_AUTOSTART") == "1" or jobs.worker_alive(ctx):
        return False
    # A freshly started worker needs a few seconds to register; avoid spawning duplicates meanwhile.
    if time.monotonic() - _last_spawn < LOCK_WAIT_SECONDS + 5:
        return False
    _last_spawn = time.monotonic()
    log_dir = ctx.settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "litreview.worker", "--data-dir", str(ctx.settings.data_dir)]
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    with open(log_dir / "worker.log", "ab") as out:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
            **kwargs,
        )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Literature review background worker")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--idle-exit", type=float, default=None, help="Exit after N idle seconds")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx = create_context(args.data_dir)
    lock = WorkerLock(ctx.settings.data_dir / "worker.lock")
    # A previous worker may still be shutting down (e.g. just killed); wait briefly for its lock.
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while not lock.acquire():
        if time.monotonic() > deadline:
            log.info("Another worker is already running for %s", ctx.settings.data_dir)
            return 0
        time.sleep(1)
    try:
        log.info("Worker started (pid %s)", os.getpid())
        Worker(ctx).run_forever(idle_exit_seconds=args.idle_exit)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
