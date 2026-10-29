"""Accept uploaded or imported PDFs into a project.

Every file in a batch ends with exactly one recorded outcome: ``accepted``, ``duplicate`` or
``rejected`` (with a reason). Nothing is dropped silently. Accepted files are queued for
extraction by the worker; parser-level validation (corruption, encryption, page limits) happens
there and is reported as a document status.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from litreview.context import AppContext
from litreview.db import new_id, now
from litreview.filestore import StorageError
from litreview.jobs import JOB_EXTRACT, enqueue

MIB = 1024 * 1024


@dataclass
class IncomingFile:
    name: str
    size: int | None
    open: Callable[[], BinaryIO]


@dataclass
class FileOutcome:
    filename: str
    outcome: str  # accepted | duplicate | rejected
    reason: str = ""
    doc_id: str | None = None
    label: str | None = None
    size_bytes: int | None = None


@dataclass
class BatchReport:
    batch_id: str
    outcomes: list[FileOutcome] = field(default_factory=list)
    batch_error: str | None = None

    def count(self, outcome: str) -> int:
        return sum(1 for o in self.outcomes if o.outcome == outcome)

    @property
    def accepted(self) -> int:
        return self.count("accepted")

    @property
    def duplicates(self) -> int:
        return self.count("duplicate")

    @property
    def rejected(self) -> int:
        return self.count("rejected")


def safe_display_name(name: str) -> str:
    """Strip any path components and control characters from a user-supplied filename."""
    name = name.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:255] or "unnamed.pdf"


def _next_label(conn, project_id: str) -> str:
    row = conn.execute(
        "SELECT MAX(CAST(SUBSTR(label, 2) AS INTEGER)) AS n FROM documents WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    n = (row["n"] or 0) + 1
    return f"D{n:03d}"


def validate_batch(ctx: AppContext, files: list[IncomingFile]) -> str | None:
    """Return a batch-level error message, or None if the batch may proceed."""
    s = ctx.settings
    if not files:
        return "No files selected"
    if len(files) > s.max_files_per_batch:
        return (
            f"Batch has {len(files)} files; the limit is {s.max_files_per_batch} per batch. "
            "Split the batch; earlier uploads are kept."
        )
    declared = sum(f.size or 0 for f in files)
    if declared > s.max_batch_size_bytes:
        return f"Batch totals {declared / MIB:,.0f} MiB; the limit is {s.max_batch_size_mib:,} MiB per batch."
    return None


def ingest_batch(ctx: AppContext, project_id: str, files: list[IncomingFile]) -> BatchReport:
    report = BatchReport(batch_id=new_id())
    batch_error = validate_batch(ctx, files)
    if batch_error:
        report.batch_error = batch_error
        for f in files:
            report.outcomes.append(
                FileOutcome(safe_display_name(f.name), "rejected", batch_error, size_bytes=f.size)
            )
        _log(ctx, project_id, report)
        return report

    usage = ctx.files.project_usage_bytes(project_id)
    received_total = 0
    for f in files:
        name = safe_display_name(f.name)
        outcome = _ingest_one(ctx, project_id, f, name, usage, received_total)
        if outcome.outcome == "accepted" and outcome.size_bytes:
            usage += outcome.size_bytes
        received_total += outcome.size_bytes or 0
        report.outcomes.append(outcome)
    _log(ctx, project_id, report)
    return report


def _ingest_one(
    ctx: AppContext,
    project_id: str,
    f: IncomingFile,
    name: str,
    usage: int,
    received_total: int,
) -> FileOutcome:
    s = ctx.settings
    if f.size is not None:
        if f.size == 0:
            return FileOutcome(name, "rejected", "Empty file (0 bytes)", size_bytes=0)
        if f.size > s.max_file_size_bytes:
            return FileOutcome(
                name,
                "rejected",
                f"File is {f.size / MIB:,.1f} MiB; the per-file limit is {s.max_file_size_mib} MiB",
                size_bytes=f.size,
            )
        if usage + f.size > s.max_project_storage_bytes:
            return FileOutcome(
                name,
                "rejected",
                f"Project storage quota of {s.max_project_storage_mib:,} MiB would be exceeded",
                size_bytes=f.size,
            )
        free = ctx.files.free_disk_bytes()
        if free - f.size < s.min_free_disk_mib * MIB:
            return FileOutcome(
                name,
                "rejected",
                f"Insufficient disk space: {free / MIB:,.0f} MiB free, {s.min_free_disk_mib} MiB "
                "must remain free",
                size_bytes=f.size,
            )
    try:
        with f.open() as stream:
            staged = ctx.files.stage_stream(project_id, stream, s.max_file_size_bytes)
    except StorageError as exc:
        return FileOutcome(name, "rejected", str(exc), size_bytes=f.size)
    except OSError as exc:
        return FileOutcome(name, "rejected", f"Could not read or store file: {exc.strerror or exc}")

    if staged.size_bytes == 0:
        ctx.files.discard_staged(staged)
        return FileOutcome(name, "rejected", "Empty file (0 bytes)", size_bytes=0)
    if received_total + staged.size_bytes > s.max_batch_size_bytes:
        ctx.files.discard_staged(staged)
        return FileOutcome(name, "rejected", "Batch total size limit reached", size_bytes=staged.size_bytes)
    if not staged.signature_ok:
        ctx.files.discard_staged(staged)
        return FileOutcome(
            name,
            "rejected",
            "Not a PDF: the file does not start with a PDF signature",
            size_bytes=staged.size_bytes,
        )

    doc_id = new_id()
    try:
        with ctx.db.transaction() as c:
            dup = c.execute(
                "SELECT id, label, original_filename FROM documents WHERE project_id = ? AND sha256 = ?",
                (project_id, staged.sha256),
            ).fetchone()
            if dup:
                ctx.files.discard_staged(staged)
                return FileOutcome(
                    name,
                    "duplicate",
                    f"Identical content to {dup['label']} ({dup['original_filename']}); not stored again",
                    doc_id=dup["id"],
                    label=dup["label"],
                    size_bytes=staged.size_bytes,
                )
            label = _next_label(c, project_id)
            ctx.files.commit_staged(staged, project_id, doc_id)
            c.execute(
                "INSERT INTO documents(id, project_id, label, original_filename, stored_name, sha256,"
                " size_bytes, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued',"
                " ?, ?)",
                (
                    doc_id,
                    project_id,
                    label,
                    name,
                    f"{doc_id}.pdf",
                    staged.sha256,
                    staged.size_bytes,
                    now(),
                    now(),
                ),
            )
            enqueue(c, project_id, JOB_EXTRACT, doc_id=doc_id)
    except Exception:
        # Roll back the stored file if the record could not be written.
        ctx.files.discard_staged(staged)
        ctx.files.delete_document_file(project_id, doc_id)
        raise
    return FileOutcome(name, "accepted", "", doc_id=doc_id, label=label, size_bytes=staged.size_bytes)


def _log(ctx: AppContext, project_id: str, report: BatchReport) -> None:
    with ctx.db.transaction() as c:
        for o in report.outcomes:
            c.execute(
                "INSERT INTO ingest_log(project_id, batch_id, filename, size_bytes, outcome, reason,"
                " doc_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, report.batch_id, o.filename, o.size_bytes, o.outcome, o.reason, o.doc_id, now()),
            )


def files_from_folder(folder: Path, recursive: bool = False) -> list[IncomingFile]:
    """List PDFs in a local folder for streamed import (no full-file buffering in memory)."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder}")
    pattern = "**/*" if recursive else "*"
    paths = sorted(p for p in folder.glob(pattern) if p.is_file() and p.suffix.lower() == ".pdf")
    return [IncomingFile(name=p.name, size=p.stat().st_size, open=(lambda p=p: p.open("rb"))) for p in paths]


def ingest_log(ctx: AppContext, project_id: str, limit: int = 500) -> list[dict]:
    rows = ctx.db.query(
        "SELECT * FROM ingest_log WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)
    )
    return [dict(r) for r in rows]
