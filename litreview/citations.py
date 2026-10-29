"""Citation parsing and programmatic validation.

Citation format used everywhere: ``[D001 p.3]``, ``[D001 pp.3-4]``, several separated by ``;``
inside one bracket: ``[D001 p.3; D004 p.7]``.

Validation checks that the document exists in the allowed scope, the page is within bounds and
was readable, and that quotations match the stored page text. A valid page reference does not
prove semantic support: the researcher must still check the claim against the source.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

CITATION_BLOCK_RE = re.compile(r"\[((?:D\d{3,}\s+pp?\.\s*\d+(?:\s*[-–]\s*\d+)?\s*;?\s*)+)\]")
CITATION_ITEM_RE = re.compile(r"(D\d{3,})\s+pp?\.\s*(\d+)(?:\s*[-–]\s*(\d+))?")
QUOTE_RE = re.compile(r"[\"“]([^\"”]{12,400})[\"”]\s*(\[[^\]]+\])")

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "ﬁ": "fi",
        "ﬂ": "fl",
    }
)


def normalise(text: str) -> str:
    text = html.unescape(text or "")
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE)
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)  # de-hyphenate line breaks
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def verify_quote(page_text: str, quote: str) -> bool:
    """True if the quotation appears verbatim (after whitespace/typography normalisation).

    An ellipsis (``...`` or ``…``) in the quote is treated as an omission: each part must appear,
    in order, on the page.
    """
    if not quote or not quote.strip():
        return False
    page = normalise(page_text)
    parts = [normalise(p) for p in re.split(r"\.\.\.|…", quote) if p.strip()]
    if not parts:
        return False
    pos = 0
    for part in parts:
        if len(part) < 4:
            continue
        idx = page.find(part, pos)
        if idx < 0:
            # also accept the quote without hyphenation differences
            idx = page.replace("- ", "").find(part.replace("- ", ""), 0)
            if idx < 0:
                return False
        pos = idx
    return True


@dataclass
class CitationRef:
    label: str
    page_start: int
    page_end: int
    raw: str

    @property
    def pages(self) -> list[int]:
        return list(range(self.page_start, self.page_end + 1))


def parse_citations(text: str) -> list[CitationRef]:
    refs: list[CitationRef] = []
    for block in CITATION_BLOCK_RE.finditer(text or ""):
        for item in CITATION_ITEM_RE.finditer(block.group(1)):
            start = int(item.group(2))
            end = int(item.group(3)) if item.group(3) else start
            if end < start:
                start, end = end, start
            refs.append(CitationRef(item.group(1), start, end, block.group(0)))
    return refs


def format_citation(label: str, pages: list[int]) -> str:
    pages = sorted(set(p for p in pages if isinstance(p, int) and p > 0))
    if not pages:
        return f"[{label}]"
    if len(pages) == 1:
        return f"[{label} p.{pages[0]}]"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"[{label} pp.{pages[0]}-{pages[-1]}]"
    return "[" + "; ".join(f"{label} p.{p}" for p in pages) + "]"


@dataclass
class CitationReport:
    total: int = 0
    valid: int = 0
    invalid: list[dict] = field(default_factory=list)
    uncited_paragraphs: list[str] = field(default_factory=list)
    quotes_checked: int = 0
    quotes_unverified: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "valid": self.valid,
            "invalid": self.invalid,
            "uncited_paragraphs": self.uncited_paragraphs,
            "quotes_checked": self.quotes_checked,
            "quotes_unverified": self.quotes_unverified,
        }

    @property
    def ok(self) -> bool:
        return not self.invalid and not self.quotes_unverified


@dataclass
class DocPages:
    """Lookup used for validation: label -> (page_count, readable pages, page text getter)."""

    page_count: int
    readable: set[int]
    texts: dict[int, str]


def validate_text(
    text: str,
    docs: dict[str, DocPages],
    require_paragraph_citations: bool = True,
) -> CitationReport:
    report = CitationReport()
    for ref in parse_citations(text):
        report.total += 1
        doc = docs.get(ref.label)
        if doc is None:
            report.invalid.append({"citation": ref.raw, "reason": f"{ref.label} is not in the cited scope"})
            continue
        bad = [p for p in ref.pages if p < 1 or p > doc.page_count]
        if bad:
            report.invalid.append(
                {"citation": ref.raw, "reason": f"Page {bad[0]} outside 1-{doc.page_count} for {ref.label}"}
            )
            continue
        unreadable = [p for p in ref.pages if p not in doc.readable]
        if unreadable:
            report.invalid.append(
                {"citation": ref.raw, "reason": f"Page {unreadable[0]} of {ref.label} has no readable text"}
            )
            continue
        report.valid += 1
    for m in QUOTE_RE.finditer(text or ""):
        quote, cite = m.group(1), m.group(2)
        refs = parse_citations(cite)
        if not refs:
            continue
        report.quotes_checked += 1
        ok = False
        for ref in refs:
            doc = docs.get(ref.label)
            if doc and any(verify_quote(doc.texts.get(p, ""), quote) for p in ref.pages):
                ok = True
                break
        if not ok:
            report.quotes_unverified.append({"quote": quote[:200], "citation": cite})
    if require_paragraph_citations:
        for para in re.split(r"\n\s*\n", text or ""):
            stripped = para.strip()
            if not stripped or stripped.startswith("#") or len(stripped) < 80:
                continue
            if stripped.startswith(">") or stripped.startswith("|"):
                continue
            if not parse_citations(stripped):
                report.uncited_paragraphs.append(stripped[:160])
    return report
