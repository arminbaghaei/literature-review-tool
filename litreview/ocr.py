"""OCR adapter. Uses RapidOCR (ONNX Runtime) when installed; otherwise reports unavailability.

OCR runs locally. No document content leaves the machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class OcrResult:
    text: str
    confidence: float  # mean line confidence, 0-1
    line_count: int


class OcrEngine:
    name = "none"

    @property
    def available(self) -> bool:
        return False

    def recognise(self, image: Any) -> OcrResult:  # pragma: no cover - interface
        raise RuntimeError("OCR is not available")


class RapidOcrEngine(OcrEngine):
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr import RapidOCR  # imported lazily: model load takes ~1 s

        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        self._engine = RapidOCR()

    @property
    def available(self) -> bool:
        return True

    def recognise(self, image: Any) -> OcrResult:
        import numpy as np

        result = self._engine(np.asarray(image.convert("RGB")))
        texts = list(result.txts or ())
        scores = [float(s) for s in (result.scores or ())]
        boxes = result.boxes if result.boxes is not None else []
        if not texts:
            return OcrResult("", 0.0, 0)
        # Order lines top-to-bottom, then left-to-right, using each box's top-left corner.
        order = list(range(len(texts)))
        if len(boxes) == len(texts):
            order.sort(key=lambda i: (round(float(boxes[i][0][1]) / 12), float(boxes[i][0][0])))
        lines = [texts[i] for i in order]
        conf = sum(scores) / len(scores) if scores else 0.0
        return OcrResult("\n".join(lines), conf, len(lines))


_engine: OcrEngine | None = None


def get_engine(enabled: bool = True) -> OcrEngine:
    """Return a process-wide OCR engine, or a null engine if OCR is disabled or not installed."""
    global _engine
    if not enabled:
        return OcrEngine()
    if _engine is None:
        try:
            _engine = RapidOcrEngine()
        except Exception as exc:  # ImportError or model load failure
            log.warning("OCR unavailable: %s", type(exc).__name__)
            _engine = OcrEngine()
    return _engine
