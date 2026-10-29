"""Split page text into search chunks.

Chunks never cross a page boundary, so every search hit maps to exactly one PDF page. Splits
prefer paragraph and sentence boundaries; a small overlap keeps a result next to its
qualifications. The most recent heading-like line is carried as section context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_CHARS = 1400
MAX_CHARS = 2000
OVERLAP_CHARS = 200

SECTION_WORDS = (
    "abstract|introduction|background|literature review|related work|method|methods|methodology|"
    "materials and methods|study design|participants|data|results|findings|discussion|"
    "limitations|conclusion|conclusions|references|acknowledg|appendix"
)
HEADING_RE = re.compile(rf"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:{SECTION_WORDS})[a-z ]{{0,40}}\s*$", re.IGNORECASE)
NUMBERED_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z ,:-]{2,80}$")


@dataclass
class Chunk:
    page_no: int
    seq: int
    section: str
    text: str


def detect_heading(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return None
    if HEADING_RE.match(stripped) or NUMBERED_HEADING_RE.match(stripped):
        return stripped
    return None


def _split_long(paragraph: str) -> list[str]:
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    parts: list[str] = []
    buf = ""
    for sentence in sentences:
        while len(sentence) > MAX_CHARS:
            parts.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if len(buf) + len(sentence) + 1 > TARGET_CHARS and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf} {sentence}".strip()
    if buf:
        parts.append(buf)
    return parts


def chunk_pages(pages: list[tuple[int, str]]) -> list[Chunk]:
    """``pages`` is a list of ``(page_no, text)``. Returns page-bounded chunks."""
    chunks: list[Chunk] = []
    section = ""
    for page_no, text in pages:
        if not text or not text.strip():
            continue
        paragraphs: list[tuple[str, str]] = []  # (section, paragraph)
        current: list[str] = []
        for line in text.splitlines():
            heading = detect_heading(line)
            if heading:
                if current:
                    paragraphs.append((section, " ".join(current)))
                    current = []
                section = heading
                continue
            if not line.strip():
                if current:
                    paragraphs.append((section, " ".join(current)))
                    current = []
                continue
            current.append(line.strip())
        if current:
            paragraphs.append((section, " ".join(current)))

        seq = 0
        buf = ""
        buf_section = section
        for para_section, para in paragraphs:
            for piece in _split_long(para):
                if buf and len(buf) + len(piece) + 1 > TARGET_CHARS:
                    chunks.append(Chunk(page_no, seq, buf_section, buf))
                    seq += 1
                    tail = buf[-OVERLAP_CHARS:]
                    cut = tail.find(" ")
                    buf = tail[cut + 1 :] if cut >= 0 else ""
                if not buf:
                    buf_section = para_section
                buf = f"{buf}\n{piece}".strip() if buf else piece
        if buf:
            chunks.append(Chunk(page_no, seq, buf_section, buf))
    return chunks
