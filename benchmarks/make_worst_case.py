"""Build worst-case files near the configured per-file size target from a folder of real PDFs.

    python benchmarks/make_worst_case.py --source <pdf folder> --out <empty folder> [--target-mib 95]

Creates:
- ``worst_scanned.pdf``: pages of the source papers rasterised at 200 dpi (image-only, needs OCR on
  every page), grown until it reaches the target size.
- ``worst_text.pdf``: source papers concatenated into one long text-based PDF up to the target size.
The outputs contain third-party content: keep them private and out of git.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image


def scanned(sources: list[Path], out: Path, target: int) -> tuple[int, int]:
    images: list[Image.Image] = []
    size = 0
    for src in sources:
        pdf = pdfium.PdfDocument(str(src))
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                img = page.render(scale=200 / 72).to_pil().convert("L")
                page.close()
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=80)
                size += buf.tell()
                images.append(Image.open(io.BytesIO(buf.getvalue())))
                if size >= target:
                    break
        finally:
            pdf.close()
        if size >= target:
            break
    images[0].save(out, "PDF", resolution=200, save_all=True, append_images=images[1:])
    return len(images), out.stat().st_size


def text(sources: list[Path], out: Path, target: int) -> tuple[int, int]:
    merged = pdfium.PdfDocument.new()
    size = 0
    for src in sources:
        if size + src.stat().st_size > target:
            continue
        try:
            pdf = pdfium.PdfDocument(str(src))
        except pdfium.PdfiumError:
            continue
        merged.import_pages(pdf)
        pdf.close()
        size += src.stat().st_size
    pages = len(merged)
    merged.save(str(out))
    merged.close()
    return pages, out.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--target-mib", type=int, default=95)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    sources = sorted(args.source.glob("*.pdf"))
    target = args.target_mib * 1024 * 1024
    n, s = scanned(sources, args.out / "worst_scanned.pdf", target)
    print(f"worst_scanned.pdf: {n} pages, {s / 2**20:.1f} MiB")
    n, s = text(sorted(sources, key=lambda p: -p.stat().st_size), args.out / "worst_text.pdf", target)
    print(f"worst_text.pdf: {n} pages, {s / 2**20:.1f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
