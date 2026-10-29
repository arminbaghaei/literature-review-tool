"""Ingestion benchmark: import a folder of PDFs into a fresh project and process it.

    python benchmarks/ingest_benchmark.py --pdf-dir <folder> --data-dir <empty temp dir> [--workers 2]

Measures wall-clock time, peak resident memory of this process plus all extraction child
processes (sampled every 0.5 s), disk use of the data directory, page counts, OCR share and
failures. Results are printed and written to ``<data-dir>/benchmark.json``. The corpus and data dir
are private: keep them out of git.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litreview.config import load_settings  # noqa: E402
from litreview.context import create_context  # noqa: E402
from litreview.documents import list_documents  # noqa: E402
from litreview.ingest import files_from_folder, ingest_batch  # noqa: E402
from litreview.projects import create_project  # noqa: E402
from litreview.search import search  # noqa: E402
from litreview.worker import Worker  # noqa: E402


class TreeRss(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.me = psutil.Process(os.getpid())
        self.peak_total = 0
        self.peak_child = 0
        self.stop_flag = False

    def run(self) -> None:
        while not self.stop_flag:
            try:
                total = self.me.memory_info().rss
                for child in self.me.children(recursive=True):
                    try:
                        rss = child.memory_info().rss
                    except psutil.Error:
                        continue
                    total += rss
                    self.peak_child = max(self.peak_child, rss)
                self.peak_total = max(self.peak_total, total)
            except psutil.Error:
                pass
            time.sleep(0.5)


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf-dir", required=True, type=Path)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--label", default="benchmark")
    args = ap.parse_args()

    settings = replace(load_settings(args.data_dir), extraction_workers=args.workers)
    ctx = create_context(settings=settings)
    project = create_project(ctx, args.label)
    files = files_from_folder(args.pdf_dir)
    input_bytes = sum(f.size or 0 for f in files)

    sampler = TreeRss()
    sampler.start()
    t0 = time.time()
    reports = [
        ingest_batch(ctx, project.id, files[i : i + settings.max_files_per_batch])
        for i in range(0, len(files), settings.max_files_per_batch)
    ]
    t_import = time.time() - t0
    Worker(ctx).run_until_idle(timeout=args.timeout, poll_seconds=0.5)
    t_total = time.time() - t0
    sampler.stop_flag = True
    sampler.join(2)

    docs = list_documents(ctx, project.id)
    pages = sum(d.page_count or 0 for d in docs)
    ocr = sum(d.ocr_pages for d in docs)
    unreadable = sum(d.unreadable_pages for d in docs)
    low = sum(d.low_confidence_pages for d in docs)
    statuses: dict[str, int] = {}
    for d in docs:
        statuses[d.status] = statuses.get(d.status, 0) + 1
    t_search = time.time()
    hits = search(ctx, project.id, "results methods sample")
    search_ms = (time.time() - t_search) * 1000
    sizes = sorted((f.size or 0) for f in files)
    result = {
        "label": args.label,
        "host": {
            "os": platform.platform(),
            "cpu": platform.processor(),
            "logical_cpus": psutil.cpu_count(),
            "ram_gib": round(psutil.virtual_memory().total / 2**30, 1),
            "python": platform.python_version(),
        },
        "extraction_workers": args.workers,
        "files": len(files),
        "accepted": sum(r.accepted for r in reports),
        "duplicates": sum(r.duplicates for r in reports),
        "rejected": sum(r.rejected for r in reports),
        "input_mib": round(input_bytes / 2**20, 1),
        "file_size_mib_min_median_max": [
            round(sizes[0] / 2**20, 2),
            round(sizes[len(sizes) // 2] / 2**20, 2),
            round(sizes[-1] / 2**20, 2),
        ]
        if sizes
        else [],
        "documents_by_status": statuses,
        "failures": [
            {"file": d.filename, "status": d.status, "error": d.error}
            for d in docs
            if d.status in ("failed", "partial")
        ],
        "pages_total": pages,
        "pages_ocr": ocr,
        "pages_low_confidence": low,
        "pages_unreadable": unreadable,
        "scanned_share": round(ocr / pages, 3) if pages else 0,
        "import_seconds": round(t_import, 1),
        "total_seconds": round(t_total, 1),
        "pages_per_second": round(pages / t_total, 2) if t_total else 0,
        "peak_rss_total_mib": round(sampler.peak_total / 2**20),
        "peak_rss_single_child_mib": round(sampler.peak_child / 2**20),
        "data_dir_mib": round(dir_size(args.data_dir) / 2**20, 1),
        "db_mib": round(settings.db_path.stat().st_size / 2**20, 1),
        "search_ms": round(search_ms, 1),
        "search_hits": len(hits),
    }
    (args.data_dir / "benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
