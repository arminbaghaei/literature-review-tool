"""Filesystem storage for original PDFs.

Originals are stored under generated names (``<doc_id>.pdf``) so user-supplied filenames can never
cause path traversal or overwrite another document. Data is copied in bounded chunks to a
temporary file in the same directory and then moved into place atomically.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE = 8 * 1024 * 1024
PDF_SIGNATURE = b"%PDF-"


class StorageError(Exception):
    """Raised when a file cannot be stored safely."""


@dataclass(frozen=True)
class StagedFile:
    path: Path
    sha256: str
    size_bytes: int
    signature_ok: bool


def _check_id(value: str, what: str) -> str:
    if not value or not value.isalnum():
        raise StorageError(f"Invalid {what} id")
    return value


class FileStore:
    def __init__(self, data_dir: Path) -> None:
        self.root = data_dir / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        return self.root / _check_id(project_id, "project")

    def originals_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "originals"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def staging_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "staging"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def original_path(self, project_id: str, doc_id: str) -> Path:
        return self.originals_dir(project_id) / f"{_check_id(doc_id, 'document')}.pdf"

    def project_usage_bytes(self, project_id: str) -> int:
        base = self.project_dir(project_id)
        if not base.exists():
            return 0
        return sum(p.stat().st_size for p in base.rglob("*") if p.is_file())

    def free_disk_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def stage_stream(self, project_id: str, source: BinaryIO | Iterable[bytes], max_bytes: int) -> StagedFile:
        """Copy a stream into staging in bounded chunks, hashing as it goes.

        Raises StorageError once more than ``max_bytes`` arrive; the partial file is removed.
        """
        staging = self.staging_dir(project_id)
        fd, tmp_name = tempfile.mkstemp(dir=staging, suffix=".part")
        tmp = Path(tmp_name)
        digest = hashlib.sha256()
        size = 0
        head = b""
        if hasattr(source, "read"):
            reader = source

            def chunks() -> Iterable[bytes]:
                while True:
                    block = reader.read(CHUNK_SIZE)
                    if not block:
                        return
                    yield block

            iterator = chunks()
        else:
            iterator = iter(source)
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in iterator:
                    size += len(chunk)
                    if size > max_bytes:
                        raise StorageError(
                            f"File exceeds the per-file limit of {max_bytes / 1048576:.0f} MiB"
                        )
                    if len(head) < 1024:
                        head += chunk[: 1024 - len(head)]
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return StagedFile(
            path=tmp, sha256=digest.hexdigest(), size_bytes=size, signature_ok=PDF_SIGNATURE in head
        )

    def commit_staged(self, staged: StagedFile, project_id: str, doc_id: str) -> Path:
        target = self.original_path(project_id, doc_id)
        if target.exists():
            raise StorageError("Refusing to overwrite an existing document")
        os.replace(staged.path, target)
        return target

    def discard_staged(self, staged: StagedFile) -> None:
        staged.path.unlink(missing_ok=True)

    def delete_document_file(self, project_id: str, doc_id: str) -> None:
        self.original_path(project_id, doc_id).unlink(missing_ok=True)

    def delete_project_files(self, project_id: str) -> None:
        base = self.project_dir(project_id)
        if base.exists():
            shutil.rmtree(base)
