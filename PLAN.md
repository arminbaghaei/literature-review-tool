# Implementation plan

Status legend: [ ] not started, [~] in progress, [x] done and tested.

## Starting point

The repository contained only `CLAUDE.md`. No existing stack, so the default from CLAUDE.md applies:
modular Python 3.13 + Streamlit, SQLite for records, filesystem for documents.

## Assumptions

- Deployment: local, single-user Windows 11 workstation, app bound to `localhost`. Network
  exposure requires the optional password gate plus HTTPS reverse proxy (see README).
- "Large PDF": up to 100 MiB and up to ~3000 pages, possibly scanned (see REQUIREMENTS.md).
- Data directory defaults to `%LOCALAPPDATA%\LitReviewTool\data`, outside the git checkout and
  outside OneDrive (SQLite and sync clients conflict). Override with `LITREVIEW_DATA_DIR`.
- OCR uses RapidOCR (ONNX Runtime, Apache-2.0, pip-installable). No Tesseract install needed.
- PDF parsing uses pypdfium2 (Apache-2.0/BSD). PyMuPDF was rejected because of its AGPL licence.
- AI: official `anthropic` Python SDK, model configurable, default `claude-opus-5-5`.

## Architecture

```
app.py                      Streamlit entry point: auth gate, navigation, worker autostart
litreview/config.py         Settings from environment, limits, prices
litreview/db.py             SQLite schema, connections (WAL), migrations
litreview/filestore.py      Safe names, streamed atomic writes, quota and disk checks
litreview/projects.py       Project CRUD and deletion
litreview/ingest.py         Batch validation, signature check, hashing, duplicate detection
litreview/pdf_extract.py    Page-level extraction, page labels, quality flags, OCR fallback
litreview/ocr.py            OCR adapter (RapidOCR or unavailable)
litreview/metadata.py       Heuristic bibliographic metadata, probable-duplicate detection
litreview/chunking.py       Page-bounded chunks with section context
litreview/search.py         SQLite FTS5 keyword search (BM25)
litreview/jobs.py           Persisted job queue, runs, recovery, cancellation
litreview/worker.py         Bounded worker: process-per-document extraction + AI threads
litreview/citations.py      Citation parsing, page bounds, quotation verification
litreview/matrix.py         Fields, cells, edit history, researcher-protection
litreview/ai/client.py      Provider adapter, budget, cache, usage records, retries
litreview/ai/prompts.py     Versioned prompts and JSON schemas
litreview/analysis/*.py     Per-paper extraction, summaries, Q&A, synthesis, drafting
litreview/export.py         CSV / JSON / Markdown export
litreview/ui/*.py           Streamlit pages
litreview/cli.py            Folder import, worker, benchmark helpers
tests/                      pytest suite with synthetic fixtures
benchmarks/                 Ingestion benchmark script (corpus not tracked)
```

## Key design decisions

- **Uploads.** Streamlit's uploader is memory-backed (verified in
  `streamlit/runtime/memory_uploaded_file_manager.py`). Browser uploads are streamed to disk one
  file at a time and the widget is cleared afterwards, but the browser batch still occupies RAM.
  The UI warns above `BROWSER_UPLOAD_SAFE_MIB`. For 10 GB batches, a **folder import** streams
  files from a local folder in 8 MiB chunks with no full-file buffering.
- **Worker.** Separate process (`python -m litreview.worker`), auto-started by the app, guarded by
  a lock file and heartbeat. Jobs live in SQLite. Each PDF is extracted in its own child process
  with a timeout, so a crashing or hanging parser cannot take down the worker. Pages are
  committed incrementally, so restart resumes from the last committed page.
- **Coverage.** Every document records pages total / text / OCR / low-confidence / unreadable.
  A document with unreadable pages is `partial`, never `completed`.
- **AI.** Structured JSON outputs validated with pydantic. Every value carries evidence
  (page + quote); quotes are verified against stored page text. Unverified evidence downgrades a
  value to `Requires verification`. Researcher edits are never overwritten; new AI output is
  stored as a suggestion. Calls are cached by input hash so resume does not re-bill.
- **Exhaustive requests.** "All papers" questions and synthesis run per document and record a
  coverage row for every eligible document before the reduce step.
- **Budget.** Cost estimated before paid runs, explicit approval required, per-project spending
  limit enforced before each call.

## Failure cases handled

Empty file, non-PDF with `.pdf` name, corrupted PDF, password-protected PDF, page-limit exceeded,
oversized file, batch count / total size exceeded, storage quota, low disk, exact duplicate,
probable duplicate, scanned pages without OCR, low OCR confidence, worker crash or restart,
document timeout, cancellation, missing API key, rate limit / API errors, refusals, truncated
model output, invalid citations, prompt injection in document text.

## Steps and progress

1. [x] Repository inspection, REQUIREMENTS.md, PLAN.md
2. [x] Config, DB, filestore, projects
3. [x] Ingest + extraction + OCR + chunking + search
4. [x] Jobs + worker with recovery and cancellation
5. [x] AI adapter, citations, matrix, analysis features (tested with a fake provider only)
6. [x] Exports
7. [x] Streamlit UI
8. [x] Tests, ruff, CI (GitHub Actions, Windows + Ubuntu)
9. [x] Launch smoke test, AppTest UI tests, Playwright browser end-to-end script
10. [x] Benchmarks: 100 real PDFs + worst-case large files; results in README
11. [x] Push to GitHub (private repository arminbaghaei/literature-review-tool)

## Decisions made during implementation

- The default Streamlit page is served at the site root; other pages have URL paths.
- Worker autostart can be disabled with `LITREVIEW_NO_WORKER_AUTOSTART=1` (tests, services).
- An unexpected crash in a run's final step marks the run `failed` instead of leaving it running.
- Worst-case generator merges PDFs with pypdfium2 (pypdf failed on link annotations).
- Worker crash recovery bug found by the browser test (restart raced the dying worker's lock) and fixed:
  the worker waits up to 20 s for the lock and the status panel restarts a missing worker.
- Browser uploads measured at ~2.3x batch size in server RAM, so large batches use folder import.

## Unfinished work and next steps

- Live Claude API validation: no API key was available during development. All AI paths are tested
  against a fake provider. Next step: run matrix extraction on 5 papers with a real key and compare
  against a manually checked sample (metadata, sample sizes, quotations, citations).
- Retrieval quality evaluation with known relevant passages on a real corpus.
- Semantic/hybrid retrieval is not implemented (keyword BM25 only).
- OCR throughput (~9.7 s/page) limits large scanned corpora; consider parallel page OCR or lower DPI.
- Heuristic title guess uses only the first line; wrapped titles are truncated (labelled heuristic).

## Validation steps

`python -m pytest`, `python -m ruff check .`, `python -m ruff format --check .`, app launch smoke
test, browser-driven upload test, benchmark script.
