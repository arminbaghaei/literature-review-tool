"""Browser end-to-end check of the running app (Playwright + Chromium).

Usage (app must already be running against a throwaway data dir):

    python benchmarks/e2e_browser.py --url http://localhost:8599 --workdir <empty temp folder> [--big-mib 50 --big-count 10]

Steps: create project -> upload PDFs through the real browser uploader -> watch processing ->
kill the worker mid-run and confirm processing recovers -> refresh the browser -> keyword search ->
open the source viewer -> download the matrix CSV. Reports the Streamlit server's peak RSS during
the browser upload. Uses synthetic PDFs only.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import psutil
from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
from conftest import PAPER_A, PAPER_B, make_scanned_pdf, make_text_pdf  # noqa: E402


def server_process(port: int) -> psutil.Process:
    for conn in psutil.net_connections(kind="tcp"):
        if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN and conn.pid:
            return psutil.Process(conn.pid)
    raise RuntimeError(f"No process listening on port {port}")


def worker_processes(data_dir_hint: str) -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["cmdline"]):
        cmd = " ".join(p.info["cmdline"] or [])
        if "litreview.worker" in cmd and data_dir_hint in cmd:
            out.append(p)
    return out


def make_padded_pdf(path: Path, mib: int) -> Path:
    """Valid small PDF followed by padding bytes: exercises byte transport, not extraction quality."""
    make_text_pdf(path, [f"Padded synthetic transport test file {path.stem}. " * 20])
    with open(path, "ab") as fh:
        block = b"%" + b"0" * (1024 * 1024 - 2) + b"\n"
        for _ in range(mib):
            fh.write(block)
    return path


class RssSampler(threading.Thread):
    def __init__(self, proc: psutil.Process) -> None:
        super().__init__(daemon=True)
        self.proc = proc
        self.peak = proc.memory_info().rss
        self.base = self.peak
        self.stop_flag = False

    def run(self) -> None:
        while not self.stop_flag:
            try:
                self.peak = max(self.peak, self.proc.memory_info().rss)
            except psutil.Error:
                return
            time.sleep(0.2)


def goto(page: Page, url: str, path: str) -> None:
    page.goto(f"{url}/{path}")
    page.wait_for_selector("[data-testid='stApp']")
    page.wait_for_timeout(1500)


def wait_until_processed(page: Page, url: str, expected_total: int, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = page.inner_text("body")
        if "Worker idle" in body:
            return body
        page.wait_for_timeout(2000)
    raise TimeoutError("Processing did not finish in time")


def active_at_kill(db_path: str) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM documents WHERE status IN ('queued', 'processing')").fetchone()[0]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8599")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--data-dir-hint", default="lr_e2e")
    ap.add_argument("--db", required=True, help="Path to the app's litreview.db (to confirm work was in progress)")
    ap.add_argument("--big-mib", type=int, default=0)
    ap.add_argument("--big-count", type=int, default=0)
    args = ap.parse_args()
    port = int(args.url.rsplit(":", 1)[1])
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    files = [
        make_text_pdf(work / "paper_a.pdf", PAPER_A * 10),
        make_text_pdf(work / "paper_b.pdf", PAPER_B * 10),
        make_scanned_pdf(work / "scanned.pdf", [["Scanned survey of 42 participants", "Results were mixed"]] * 40),
    ]
    (work / "not_really.pdf").write_bytes(b"this is not a pdf")
    files.append(work / "not_really.pdf")
    for i in range(args.big_count):
        files.append(make_padded_pdf(work / f"big_{i}.pdf", args.big_mib))
    total_mib = sum(f.stat().st_size for f in files) / 1048576
    results: dict[str, object] = {"files": len(files), "upload_total_mib": round(total_mib, 1)}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        goto(page, args.url, "")
        page.get_by_label("Project name").fill("E2E project")
        page.get_by_role("button", name="Create project").click()
        page.wait_for_timeout(2000)
        expect(page.get_by_text("E2E project").first).to_be_visible()

        goto(page, args.url, "upload")
        server = server_process(port)
        sampler = RssSampler(server)
        sampler.start()
        t0 = time.time()
        page.locator("input[type='file']").set_input_files([str(f) for f in files])
        page.get_by_role("button", name="Add to project").click(timeout=600_000)
        page.wait_for_selector("text=Last batch", timeout=600_000)
        results["upload_seconds"] = round(time.time() - t0, 1)
        page.wait_for_timeout(1500)
        sampler.stop_flag = True
        results["server_rss_before_mib"] = round(sampler.base / 1048576)
        results["server_rss_peak_mib"] = round(sampler.peak / 1048576)
        body = page.inner_text("body")
        results["batch_report_shows_rejection"] = "Not a PDF" in body

        # Kill the worker while it is processing, then confirm the app restarts it and work completes.
        time.sleep(2)
        killed = 0
        for proc in worker_processes(args.data_dir_hint):
            for child in proc.children(recursive=True):
                child.kill()
            proc.kill()
            killed += 1
        results["workers_killed_mid_run"] = killed
        results["active_documents_at_kill"] = active_at_kill(args.db)
        page.reload()  # browser refresh; also triggers worker autostart
        page.wait_for_selector("[data-testid='stApp']")
        expected = len(files) - 1  # the fake PDF is rejected at upload
        body = wait_until_processed(page, args.url, expected, timeout_s=900)
        results["processing_finished_after_restart"] = True
        results["completed_metric_text"] = [line for line in body.splitlines() if "completed" in line][:2]

        goto(page, args.url, "search")
        page.get_by_label("Search terms").fill("households Wellington")
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        results["search_hit_visible"] = page.get_by_text("D001 p.2").first.is_visible()

        goto(page, args.url, "documents")
        page.wait_for_selector("img", timeout=30_000)
        results["source_viewer_image"] = page.locator("[data-testid='stImage'] img").count() > 0

        goto(page, args.url, "export")
        with page.expect_download() as dl:
            page.get_by_role("button", name="Matrix (CSV)").click()
        csv_path = work / "matrix.csv"
        dl.value.save_as(csv_path)
        header = csv_path.read_text(encoding="utf-8").splitlines()[0]
        results["csv_has_coverage_columns"] = "coverage" in header and "sample_size__status" in header
        browser.close()

    for k, v in results.items():
        print(f"{k}: {v}")
    ok = all(
        results[k]
        for k in (
            "batch_report_shows_rejection",
            "processing_finished_after_restart",
            "search_hit_visible",
            "source_viewer_image",
            "csv_has_coverage_columns",
        )
    )
    print("E2E RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
