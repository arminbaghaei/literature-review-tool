"""SQLite storage for project records.

One database file holds all projects; every query is scoped by ``project_id``. WAL mode lets the
Streamlit app and the worker processes read and write concurrently.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    review_question TEXT NOT NULL DEFAULT '',
    ai_budget_usd REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    page_count INTEGER,
    pages_done INTEGER NOT NULL DEFAULT 0,
    text_pages INTEGER NOT NULL DEFAULT 0,
    ocr_pages INTEGER NOT NULL DEFAULT 0,
    low_confidence_pages INTEGER NOT NULL DEFAULT 0,
    unreadable_pages INTEGER NOT NULL DEFAULT 0,
    flagged_pages INTEGER NOT NULL DEFAULT 0,
    extraction_version TEXT,
    pdf_metadata TEXT,
    probable_duplicate_of TEXT,
    duplicate_review TEXT NOT NULL DEFAULT 'none',
    study_group TEXT,
    included INTEGER NOT NULL DEFAULT 1,
    exclusion_reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (project_id, sha256),
    UNIQUE (project_id, label)
);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_id);

CREATE TABLE IF NOT EXISTS pages (
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    page_label TEXT,
    method TEXT NOT NULL,
    quality TEXT NOT NULL,
    ocr_confidence REAL,
    flags TEXT NOT NULL DEFAULT '',
    char_count INTEGER NOT NULL,
    extraction_version TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_no)
);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    section TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    chunker_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, section, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section) VALUES (new.id, new.text, new.section);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section)
    VALUES ('delete', old.id, old.text, old.section);
END;

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    size_bytes INTEGER,
    outcome TEXT NOT NULL,
    reason TEXT,
    doc_id TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    approved_budget_usd REAL,
    estimated_cost_usd REAL,
    spent_usd REAL NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    doc_id TEXT,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    progress INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result TEXT,
    worker_pid INTEGER,
    heartbeat_at REAL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    heartbeat_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS matrix_fields (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    position INTEGER NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, key)
);

CREATE TABLE IF NOT EXISTS matrix_cells (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    field_key TEXT NOT NULL,
    value TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    reviewed INTEGER NOT NULL DEFAULT 0,
    ai_suggestion TEXT,
    model TEXT,
    prompt_version TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_id, doc_id, field_key)
);

CREATE TABLE IF NOT EXISTS edit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    entity TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_by TEXT NOT NULL,
    changed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    statement TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    page_no INTEGER,
    quote TEXT,
    quote_verified INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    prompt_version TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_doc ON evidence(doc_id);

CREATE TABLE IF NOT EXISTS summaries (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    citation_report TEXT,
    ai_suggestion TEXT,
    model TEXT,
    prompt_version TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (project_id, doc_id)
);

CREATE TABLE IF NOT EXISTS themes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS theme_docs (
    theme_id TEXT NOT NULL REFERENCES themes(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    pages TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    PRIMARY KEY (theme_id, doc_id)
);

CREATE TABLE IF NOT EXISTS outputs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    request TEXT NOT NULL DEFAULT '{}',
    content_md TEXT NOT NULL DEFAULT '',
    content_json TEXT,
    coverage TEXT,
    citation_report TEXT,
    source TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id TEXT,
    purpose TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    request_id TEXT,
    settings TEXT,
    response TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ai_calls_hash ON ai_calls(project_id, input_hash);
"""


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> float:
    return time.time()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    finally:
        conn.close()


class Database:
    """Opens short-lived connections, so one instance is safe to share across threads."""

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        init_db(db_path)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.path)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self.conn() as c:
            return list(c.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.transaction() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid or cur.rowcount
