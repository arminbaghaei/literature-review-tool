"""Keyword search over page-bounded chunks using SQLite FTS5 (BM25 ranking). Works offline."""

from __future__ import annotations

import re
from dataclasses import dataclass

from litreview.context import AppContext

INDEX_VERSION = "fts5-porter-1"

STOPWORDS = set(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "about",
        "across",
        "all",
        "any",
        "between",
        "compare",
        "paper",
        "papers",
        "study",
        "studies",
        "report",
        "reported",
        "show",
        "shows",
        "find",
        "found",
        "tell",
        "me",
        "please",
    ]
)
TOKEN_RE = re.compile(r"[\w][\w\-']*", re.UNICODE)


@dataclass
class SearchHit:
    chunk_id: int
    doc_id: str
    label: str
    filename: str
    page_no: int
    section: str
    text: str
    snippet: str
    score: float


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def build_fts_query(query: str, mode: str = "any") -> str:
    """Turn free text into a safe FTS5 query.

    Quoted phrases are kept as phrases. Other words become quoted terms (so user input can never
    inject FTS operators). ``mode`` is ``any`` (OR) or ``all`` (AND).
    """
    phrases = re.findall(r'"([^"]+)"', query)
    rest = re.sub(r'"[^"]+"', " ", query)
    parts = [_quote(p.strip()) for p in phrases if p.strip()]
    words = [w.lower() for w in TOKEN_RE.findall(rest)]
    keep = [w for w in words if w not in STOPWORDS and len(w) > 1] or words
    parts += [_quote(w) for w in dict.fromkeys(keep)]
    joiner = " AND " if mode == "all" else " OR "
    return joiner.join(parts)


def search(
    ctx: AppContext,
    project_id: str,
    query: str,
    doc_ids: list[str] | None = None,
    limit: int = 50,
    mode: str = "any",
    per_doc_limit: int | None = None,
) -> list[SearchHit]:
    fts = build_fts_query(query, mode)
    if not fts:
        return []
    params: list = [fts, project_id]
    doc_filter = ""
    if doc_ids:
        doc_filter = f" AND c.doc_id IN ({','.join('?' for _ in doc_ids)})"
        params += list(doc_ids)
    fetch = limit * 5 if per_doc_limit else limit
    params.append(fetch)
    sql = (
        "SELECT c.id, c.doc_id, c.page_no, c.section, c.text, d.label, d.original_filename,"
        " snippet(chunks_fts, 0, '[', ']', ' … ', 24) AS snip, bm25(chunks_fts) AS score"
        " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
        " JOIN documents d ON d.id = c.doc_id"
        " WHERE chunks_fts MATCH ? AND c.project_id = ? AND d.included = 1"
        + doc_filter
        + " ORDER BY score LIMIT ?"
    )
    rows = ctx.db.query(sql, params)
    hits: list[SearchHit] = []
    per_doc: dict[str, int] = {}
    for r in rows:
        if per_doc_limit:
            if per_doc.get(r["doc_id"], 0) >= per_doc_limit:
                continue
            per_doc[r["doc_id"]] = per_doc.get(r["doc_id"], 0) + 1
        hits.append(
            SearchHit(
                chunk_id=r["id"],
                doc_id=r["doc_id"],
                label=r["label"],
                filename=r["original_filename"],
                page_no=r["page_no"],
                section=r["section"],
                text=r["text"],
                snippet=r["snip"],
                score=-float(r["score"]),
            )
        )
        if len(hits) >= limit:
            break
    return hits
