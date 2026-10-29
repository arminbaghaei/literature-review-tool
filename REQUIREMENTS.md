# Requirements and acceptance targets

This file records scope and acceptance targets. Implementation progress lives in `PLAN.md`.
Setup, commands and measured limits live in `README.md`.

## Scope

A private, single-user literature review tool for one researcher working with about 100
research PDFs per project. It is not a systematic-review platform: database searching,
screening protocols, reporting standards (e.g. PRISMA) and meta-analysis are out of scope.
The researcher remains responsible for checking interpretations and approving outputs.

## User requirements (from the researcher)

1. Upload about 100 large research PDFs into one project, with progress, per-file processing
   status, failure reasons and retry.
2. Extract text with PDF page references, OCR where needed, detect duplicates, flag unreadable
   content.
3. Editable literature review matrix: title, authors, year, research aim, methods, population,
   sample size, findings, limitations.
4. Search papers; ask questions about one paper, selected papers, or the whole collection.
5. Compare studies, organise themes, identify conflicting findings, suggest research gaps
   supported by uploaded evidence.
6. Editable paper summaries and draft literature review sections with document + page citations.
7. Export matrix and evidence as CSV and JSON; draft review as Markdown.
8. Save projects and resume interrupted processing without re-uploading.
9. Never fabricate references, findings, quotations or missing values. Distinguish reported
   evidence from interpretation. Requests covering all papers track coverage of every eligible
   document.
10. Configurable Claude API integration. Upload, extraction and keyword search work without a key.
    Credentials stay out of git, logs, exports and the browser. Research data stays out of git.

## Proposed defaults (configurable; not verified capacity claims)

| Setting | Default | Source |
|---|---|---|
| `MAX_FILES_PER_BATCH` | 100 | CLAUDE.md proposed target |
| `MAX_FILE_SIZE_MIB` | 100 | CLAUDE.md proposed target |
| `MAX_BATCH_SIZE_MIB` | 10000 | CLAUDE.md proposed target |
| `MAX_PROJECT_STORAGE_MIB` | 20000 | Assumption |
| `MAX_PAGES_PER_DOCUMENT` | 3000 | Assumption |
| `DOCUMENT_TIMEOUT_SECONDS` | 3600 | Assumption |
| `EXTRACTION_WORKERS` | 2 | Assumption |
| `BROWSER_UPLOAD_SAFE_MIB` | 1500 | Assumption: in-memory upload budget |

"Large PDF" is assumed to mean up to 100 MiB and up to a few thousand pages, possibly scanned.

## Acceptance targets

- Complete path works: create project, upload, extract/OCR, index, search, source-linked analysis,
  edit matrix, export, reopen project.
- Valid, scanned, mixed, duplicate, corrupted, encrypted, empty, oversized, count-violating and
  total-size-violating inputs are handled explicitly; a bad file never erases completed work.
- Worker restart resumes interrupted extraction without duplicating pages or chunks.
- Every AI claim carries document and page citations that are validated programmatically.
- Exhaustive ("all papers") requests report per-document coverage.
- Benchmark: 100 representative PDFs plus a separate worst-case large-file test, with peak RAM,
  disk, duration and failures recorded in `README.md`.
