"""Shared application services (settings, database, file store)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from litreview.config import Settings, load_settings
from litreview.db import Database
from litreview.filestore import FileStore


@dataclass
class AppContext:
    settings: Settings
    db: Database
    files: FileStore


def create_context(data_dir: Path | None = None, settings: Settings | None = None) -> AppContext:
    settings = settings or load_settings(data_dir)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return AppContext(settings=settings, db=Database(settings.db_path), files=FileStore(settings.data_dir))
