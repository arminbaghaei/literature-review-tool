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
2. [ ] Config, DB, filestore, projects
3. [ ] Ingest + extraction + OCR + chunking + search
4. [ ] Jobs + worker with recovery and cancellation
5. [ ] AI adapter, citations, matrix, analysis features
6. [ ] Exports
7. [ ] Streamlit UI
8. [ ] Tests, ruff, CI
9. [ ] Launch smoke test and UI tests
10. [ ] Benchmarks: 100 real PDFs + worst-case large file; README results
11. [ ] Push to GitHub (private repository)

## Validation steps

`python -m pytest`, `python -m ruff check .`, `python -m ruff format --check .`, app launch smoke
test, browser-driven upload test, benchmark script.
