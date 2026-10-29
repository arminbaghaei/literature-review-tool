"""Worker single-instance lock and liveness."""

from __future__ import annotations

from litreview import jobs
from litreview.worker import WorkerLock


def test_worker_lock_is_exclusive(tmp_path):
    first = WorkerLock(tmp_path / "worker.lock")
    second = WorkerLock(tmp_path / "worker.lock")
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()
    assert second.acquire()
    second.release()


def test_worker_liveness(ctx):
    assert not jobs.worker_alive(ctx)
    jobs.register_worker(ctx, "w1")
    assert jobs.worker_alive(ctx)
    ctx.db.execute("UPDATE workers SET heartbeat_at = 0")
    assert not jobs.worker_alive(ctx)
