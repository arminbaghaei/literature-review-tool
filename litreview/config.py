"""Application settings read from environment variables.

All limits are configurable. Defaults are design targets from REQUIREMENTS.md, not verified
capacity claims. Secrets (the API key) are never stored on the Settings object's repr.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024 * 1024

# Extraction pipeline versions. Bump when behaviour changes so cached results are invalidated.
EXTRACTION_VERSION = "pdfium-1"
OCR_VERSION = "rapidocr-1"
CHUNKER_VERSION = "chunk-1"

# USD per million tokens (input, output). Override with LITREVIEW_PRICE_<MODEL>="in,out".
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


def _default_data_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "LitReviewTool" / "data"
    return Path.home() / ".local" / "share" / "litreview" / "data"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"Environment variable {name} must be positive")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_files_per_batch: int = 100
    max_file_size_mib: int = 100
    max_batch_size_mib: int = 10000
    max_project_storage_mib: int = 20000
    min_free_disk_mib: int = 1024
    max_pages_per_document: int = 3000
    document_timeout_seconds: int = 3600
    extraction_workers: int = 2
    ai_workers: int = 2
    browser_upload_safe_mib: int = 1500
    ocr_enabled: bool = True
    ocr_dpi: int = 200
    ocr_min_confidence: float = 0.80
    min_chars_per_page: int = 40
    # AI
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "medium"
    ai_max_output_tokens: int = 16000
    ai_section_token_budget: int = 60000
    ai_synthesis_token_budget: int = 250000
    ai_default_budget_usd: float = 10.0
    ai_max_retries: int = 3
    ai_timeout_seconds: int = 600
    ai_refusal_fallback: bool = True
    prices: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mib * MIB

    @property
    def max_batch_size_bytes(self) -> int:
        return self.max_batch_size_mib * MIB

    @property
    def max_project_storage_bytes(self) -> int:
        return self.max_project_storage_mib * MIB

    @property
    def db_path(self) -> Path:
        return self.data_dir / "litreview.db"

    def price_for(self, model: str) -> tuple[float, float] | None:
        return self.prices.get(model)


def api_key_configured() -> bool:
    """True when an Anthropic credential is available in the environment.

    Only presence is checked; the value is never read into application state or logs.
    """
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def load_settings(data_dir: Path | None = None) -> Settings:
    prices = dict(DEFAULT_PRICES)
    for key, raw in os.environ.items():
        if key.startswith("LITREVIEW_PRICE_"):
            model = key.removeprefix("LITREVIEW_PRICE_").lower().replace("_", "-")
            parts = raw.split(",")
            if len(parts) == 2:
                prices[model] = (float(parts[0]), float(parts[1]))
    resolved = data_dir or Path(os.environ.get("LITREVIEW_DATA_DIR") or _default_data_dir())
    return Settings(
        data_dir=Path(resolved).expanduser().resolve(),
        max_files_per_batch=_env_int("MAX_FILES_PER_BATCH", 100),
        max_file_size_mib=_env_int("MAX_FILE_SIZE_MIB", 100),
        max_batch_size_mib=_env_int("MAX_BATCH_SIZE_MIB", 10000),
        max_project_storage_mib=_env_int("MAX_PROJECT_STORAGE_MIB", 20000),
        min_free_disk_mib=_env_int("MIN_FREE_DISK_MIB", 1024),
        max_pages_per_document=_env_int("MAX_PAGES_PER_DOCUMENT", 3000),
        document_timeout_seconds=_env_int("DOCUMENT_TIMEOUT_SECONDS", 3600),
        extraction_workers=_env_int("EXTRACTION_WORKERS", 2),
        ai_workers=_env_int("AI_WORKERS", 2),
        browser_upload_safe_mib=_env_int("BROWSER_UPLOAD_SAFE_MIB", 1500),
        ocr_enabled=_env_bool("OCR_ENABLED", True),
        ocr_dpi=_env_int("OCR_DPI", 200),
        ocr_min_confidence=_env_float("OCR_MIN_CONFIDENCE", 0.80),
        min_chars_per_page=_env_int("MIN_CHARS_PER_PAGE", 40),
        ai_model=os.environ.get("LITREVIEW_MODEL", "claude-opus-5-5"),
        ai_effort=os.environ.get("LITREVIEW_EFFORT", "medium"),
        ai_max_output_tokens=_env_int("LITREVIEW_MAX_OUTPUT_TOKENS", 16000),
        ai_section_token_budget=_env_int("LITREVIEW_SECTION_TOKENS", 60000),
        ai_synthesis_token_budget=_env_int("LITREVIEW_SYNTHESIS_TOKENS", 250000),
        ai_default_budget_usd=_env_float("LITREVIEW_DEFAULT_BUDGET_USD", 10.0),
        ai_max_retries=_env_int("LITREVIEW_AI_MAX_RETRIES", 3),
        ai_timeout_seconds=_env_int("LITREVIEW_AI_TIMEOUT_SECONDS", 600),
        ai_refusal_fallback=_env_bool("LITREVIEW_REFUSAL_FALLBACK", True),
        prices=prices,
    )
