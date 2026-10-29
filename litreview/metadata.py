"""Heuristic bibliographic metadata and probable-duplicate detection (no AI).

Heuristic values are always stored with status ``heuristic`` and must be verified by the
researcher or replaced by evidence-backed AI extraction. Only text printed in the paper is used;
the PDF file creation date is never used as a publication year.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
YEAR_CONTEXT_RE = re.compile(
    r"(?:©|\(c\)|copyright|published(?: online)?|accepted|received|available online)"
    r"[^\n]{0,60}?\b((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
JUNK_TITLES = re.compile(
    r"^(untitled|microsoft word|document\d*|title|paper|article|manuscript|\S+\.(docx?|pdf|tex))",
    re.IGNORECASE,
)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def find_doi(text: str) -> str | None:
    m = DOI_RE.search(text)
    if not m:
        return None
    return m.group(1).rstrip(".,;)]")


def heuristic_metadata(pdf_meta: dict, pages: list[tuple[int, str]]) -> dict[str, dict]:
    """Return ``{field_key: {"value": str, "page": int|None, "basis": str}}``."""
    out: dict[str, dict] = {}
    title = _clean(pdf_meta.get("Title", ""))
    if title and len(title) >= 12 and not JUNK_TITLES.match(title):
        out["title"] = {"value": title, "page": None, "basis": "PDF metadata Title field"}
    author = _clean(pdf_meta.get("Author", ""))
    if author and len(author) >= 3 and not JUNK_TITLES.match(author):
        out["authors"] = {"value": author, "page": None, "basis": "PDF metadata Author field"}
    for page_no, text in pages[:2]:
        doi = find_doi(text)
        if doi:
            out["doi"] = {"value": doi, "page": page_no, "basis": "DOI pattern printed in paper"}
            break
    for page_no, text in pages[:2]:
        m = YEAR_CONTEXT_RE.search(text)
        if m:
            out["year"] = {
                "value": m.group(1),
                "page": page_no,
                "basis": f"Year near '{_clean(m.group(0))[:60]}'",
            }
            break
    if "title" not in out and pages:
        page_no, text = pages[0]
        for line in text.splitlines()[:15]:
            line = _clean(line)
            if (
                20 <= len(line) <= 250
                and not DOI_RE.search(line)
                and not line.lower().startswith(
                    ("journal", "vol", "http", "www", "doi", "issn", "contents lists", "received")
                )
            ):
                out["title"] = {
                    "value": line,
                    "page": page_no,
                    "basis": "First substantial line on page 1 (guess)",
                }
                break
    return out


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (title or "").lower()).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def is_probable_duplicate(a: dict, b: dict) -> str | None:
    """Compare two metadata dicts (title, doi, year). Return a reason string or None."""
    doi_a, doi_b = (a.get("doi") or "").lower(), (b.get("doi") or "").lower()
    if doi_a and doi_a == doi_b:
        return f"Same DOI ({doi_a})"
    sim = title_similarity(a.get("title", ""), b.get("title", ""))
    if sim >= 0.9 and len(normalise_title(a.get("title", ""))) >= 20:
        return f"Very similar titles ({sim:.0%})"
    return None
