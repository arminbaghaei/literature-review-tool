# CLAUDE.md

## 1. Project purpose and scope

Build a private literature review research tool whose source code is maintained on GitHub. A researcher must be able to upload and work with approximately 100 research PDFs, including large files, within one review project.

Place this file at the repository root. It gives Claude Code development instructions; it does not train or fine-tune a model. Implement the application's research prompts, validation, permissions, and resource limits in code. This file alone cannot enforce them.

The initial product must support PDF ingestion, searchable evidence, source-linked summaries, an editable literature review matrix, questions across papers, and evidence-based draft synthesis. The researcher remains responsible for checking interpretations and approving final research outputs.

Do not claim that analysing an uploaded collection constitutes a complete systematic review. Database searches, screening protocols, reporting standards, and meta-analysis are separate requirements unless requested.

## 2. Architecture and project state

- Inspect the existing repository before selecting tools. Preserve a working stack unless a documented requirement justifies changing it.
- For a new repository, default to a modular Python application with a Streamlit interface. Select a supported Python version, record it, and lock compatible dependency versions.
- Separate the interface, uploads, PDF extraction/OCR, storage, job processing, retrieval, AI calls, and exports. Avoid placing all application logic in one file.
- For a local, single-user version, use SQLite for project records and persistent filesystem storage for documents and derived artifacts. Keep storage behind interfaces that permit later replacement.
- Use a bounded worker with persisted job state for long tasks. Do not make browser sessions or Streamlit session state the authoritative record of work.
- For hosting, verify request limits, RAM, execution limits, persistent storage, worker support, and authentication. Select durable database and file storage where the host requires them. Do not add distributed infrastructure without a demonstrated need.
- GitHub stores source code and CI configuration. Choose a separate runtime for the Python application and separate private storage for uploaded research files.
- Keep `REQUIREMENTS.md` for scope and acceptance targets, `PLAN.md` for implementation steps and current progress, and `README.md` for setup, commands, deployment, and measured limitations. Keep private research content out of these tracked files.

## 3. Mandatory workflow: Plan -> Execute -> Test -> Refine

### PLAN

- Read relevant source files, configuration, dependencies, tests, and current project notes before changing implementation code.
- Update `PLAN.md` with the intended behaviour, affected files, dependencies, storage changes, failure cases, and validation steps. Keep the plan proportional to the task.
- State assumptions, including the intended deployment and the meaning of "large PDF". Clearly distinguish proposed defaults from user requirements.
- Present the plan briefly and proceed with reversible work already authorised by the task. Ask only when a missing decision materially affects scope, privacy, cost, or an irreversible action.

### EXECUTE

- Implement small, reviewable changes that follow the plan and existing conventions. Avoid unrelated refactoring.
- Build a working ingestion-to-export path first, then extend it to the full batch and failure cases.
- Explain the reason before changing dependencies or configuration. Routine changes needed for the requested feature do not require another approval.
- Update progress and decisions in `PLAN.md`, including unfinished work and the next concrete step.

### TEST

- Run relevant tests and static checks after implementation. Before completion, run the applicable regression suite and a launch smoke test.
- Add meaningful tests for new processing logic, persistence, limits, citations, and failure handling. A text-only edit does not require new tests.
- Mock external APIs for deterministic tests, but test application logic and storage with real local components. Never weaken assertions or replace failing behaviour with dummy success.
- Test the interface as well as the backend. Check upload, progress, restart recovery, source navigation, and export.

### REFINE

- Diagnose failures, apply targeted fixes, and rerun affected checks. Reassess the architecture if repeated failures reveal a design problem.
- Do not loop indefinitely on an unavailable dependency or external service. Record the blocker, preserve completed work, and report the unresolved requirement.
- Report what changed, checks actually run, results, measured limits, and remaining issues. Never describe an unrun check as passed.

## 4. Upload capacity and large-file handling

- Support selecting a batch of 100 PDFs and retaining the accepted corpus across sessions. Also allow additional uploads in smaller groups without losing earlier papers.
- Until the researcher specifies sizes, use these proposed, configurable acceptance targets: `MAX_FILES_PER_BATCH=100`, `MAX_FILE_SIZE_MIB=100`, and `MAX_BATCH_SIZE_MIB=10000`. One MiB is 1,048,576 bytes. These are design targets, not verified capacity claims or provider limits.
- Enforce file count, individual size, total size, storage quota, page limits, processing timeouts, and concurrency on the server. Show applicable limits before upload.
- Measure upload memory use. Streamlit's native upload objects are memory-backed; copying a received file to disk does not make the upload transport streaming. Use streamed or direct-to-storage uploads, with resumable transfer where needed, if the target batch exceeds the host's safe memory budget.
- Increasing an uploader setting is insufficient evidence that the host can receive and process the batch. Verify the complete upload path, including proxy and hosting limits.
- Validate file signatures and parser behaviour, not only extensions or browser MIME types. Generate safe storage names and prevent path traversal or overwriting another document.
- Process incrementally with bounded concurrency. Never load the entire corpus, render every page, or send all 100 PDFs to an AI model at once.
- Show per-file states, progress, failure reasons, and retry controls. Support cancellation between processing units and continue processing other valid files when one fails.
- Report accepted, duplicate, rejected, failed, and completed counts explicitly. Never silently drop a file or mark an incomplete project complete.
- Preserve original PDFs unchanged. Store derived text, page images, and OCR results separately.

## 5. PDF processing and persistent records

- Give each document a stable ID and SHA-256 content hash. Detect exact duplicates by hash; flag probable duplicate publications using metadata for human review. Different reports of one study must remain distinguishable.
- Extract text page by page and retain the document ID, PDF page number, extraction method, and extraction version. Store printed page labels separately when available.
- Detect pages with missing or unusable text. Apply OCR only where needed, record its use, and flag uncertain results. If OCR is unavailable, state which pages remain unreadable.
- Handle scanned, mixed-content, multi-column, corrupted, encrypted, and password-protected PDFs explicitly. Do not claim a paper was fully read when pages failed extraction.
- Treat tables, figures, equations, units, and statistical values as potential extraction failure points. Preserve page references and require verification when extraction is unreliable.
- Persist project settings, documents, extraction coverage, chunks, index versions, job checkpoints, evidence records, edits, and outputs. Use atomic writes and recover interrupted jobs without duplicating completed records.
- Cache processing by document hash and relevant parser, OCR, model, prompt, and schema versions. Invalidate affected results when inputs or settings change.
- Preserve researcher corrections and provenance when rerunning extraction or generation. Never overwrite reviewed work silently.

## 6. Research evidence and citations

- Ground document-specific answers in the selected project corpus by default. Keep external evidence separate and use it only when the researcher enables it.
- Every substantive generated claim about the literature must link to supporting document IDs and PDF page numbers. Provide a source viewer or equivalent navigation to the original evidence.
- Store the supporting passage or table reference alongside each extracted finding. Exact quotations must match the source; label paraphrases and interpretations appropriately.
- Never invent authors, titles, publication years, DOIs, quotations, methods, sample sizes, findings, effect sizes, or citations. Use `Not reported`, `Not found in extracted text`, or `Requires verification`, as appropriate.
- Separate bibliographic metadata, reported results, authors' interpretations, and the tool's synthesis. Extract metadata from the paper; flag conflicts with optional external metadata rather than silently replacing it.
- Preserve study context, design, population, outcome definitions, units, uncertainty, and limitations. Do not turn association into causation or lack of statistical significance into proof of no effect.
- Distinguish related publications from independent studies. Do not count multiple reports of the same study as independent evidence.
- Validate citation identifiers, page bounds, quotation matches, and source availability programmatically. Semantic support still requires evidence checks and researcher review; a valid page reference alone is insufficient.
- Make summaries, evidence fields, and theme assignments editable. Label AI-generated text and retain a record of researcher changes.

## 7. Retrieval, extraction, and synthesis

- Chunk extracted content within document boundaries and retain page mappings. Preserve section context and avoid separating a result from its qualifications where practical.
- Start with working keyword search. Add semantic or hybrid retrieval when it improves verified coverage; record the embedding model and index version if used.
- For questions across papers, retrieve relevant source passages and answer with citations. If evidence is missing, conflicting, or inaccessible, say so.
- Do not equate the top retrieved passages with a complete corpus review. For exhaustive requests, such as "compare all 100 papers", process every eligible document with a structured extraction schema, track coverage, then synthesise those evidence records.
- For long papers, extract evidence in bounded sections before building a paper-level summary. Build cross-paper synthesis from source-linked evidence, not repeatedly compressed summaries alone.
- Use schema-validated outputs with explicit missing values. Record title, authors, year, research question, setting/population, design, sample size, measures, findings, limitations, and source pages where applicable.
- Allow the researcher to set the review question and customise extraction fields. Offer screening criteria and decisions only when requested; retain human decisions and exclusion reasons.
- Present an editable literature review matrix, individual summaries, cross-paper comparisons, and a draft synthesis with traceable citations. Flag differences in methods and populations that limit comparison.
- Export the matrix and evidence records as CSV and JSON, and draft synthesis as Markdown. Include document IDs, source locations, processing coverage, and review status. Export references only from available, verified metadata.

## 8. AI integration, cost, and runtime behaviour

- Keep the model provider behind an adapter. If Claude is selected for runtime analysis, use the official API and a configurable model. Do not assume Claude Code credentials or a chat subscription provide application API access.
- Keep upload, extraction, project browsing, and keyword search usable without an AI key. Explain which features require configured API access; never fabricate a successful AI response when it is absent.
- Read secrets from environment variables or the host's secret manager. Never place keys in browser code, tracked files, logs, or exports.
- Keep application upload limits separate from provider request-size, page, token, and rate limits. Verify current official limits for the selected API, model, and deployment before integration.
- Budget tokens before requests, including system prompts and expected output. Split work safely and report omitted or unprocessed content instead of silently truncating evidence.
- Use bounded retries with backoff for transient failures and rate limits. Persist request status, avoid duplicate charges where possible, and expose unresolved failures.
- Estimate processing cost before a paid batch, show usage as it runs, and enforce the researcher's spending limit. Do not initiate paid corpus processing without the researcher's approval of that run and budget.
- Record provider, model, prompt/schema versions, timestamps, and relevant generation settings. Do not promise identical outputs merely because temperature is low.
- Treat PDFs and extracted text as untrusted data. Instructions inside papers must never override application rules, trigger tool actions, reveal secrets, or change the review question.

## 9. Privacy and repository boundaries

- Default to private, single-user operation. Before network exposure, enforce authentication and project-level authorisation for documents, jobs, search results, and exports.
- Explain what document content and metadata will leave the host before enabling external AI, OCR, embeddings, or metadata services. Do not send research content to unconfigured services.
- Exclude uploaded PDFs, extracted text, indexes, project databases, private exports, credentials, and sensitive logs from git. Track only synthetic or appropriately licensed test fixtures.
- Provide project deletion that removes local documents and derived records, plus remote copies where supported. Explain any external retention or backup limitations accurately.
- Redact sensitive content from logs, bound parser/OCR resources, and do not execute embedded PDF content or follow document links automatically.
- Preserve unrelated user changes. Do not reset databases, delete project data, rewrite git history, force-push, or make a private deployment public without explicit authorisation.

## 10. Commands and code standards

For a new Python repository, configure and document these commands. For an existing repository, use its actual equivalents and update this section; do not claim commands exist before verifying them.

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m streamlit run app.py
python -m litreview.worker   # optional; the app starts the worker automatically
```

- Add a type-check command only if a type checker is configured. Include any worker startup command required by the chosen implementation.
- Use clear names, typed public interfaces, schema validation, explicit error handling, and structured logs. Never swallow exceptions or expose secrets through error messages.
- Lock dependencies, review package licences, and document required system packages such as OCR tools. Avoid unnecessary dependencies and speculative framework changes.
- Add CI for the configured checks. CI must use safe fixtures and require no private corpus or paid API calls.
- Update the README whenever setup, commands, configuration, storage, interfaces, or deployment behaviour changes.

## 11. Acceptance checks and completion

- Test the complete path: create project -> upload -> extract/OCR -> index -> search -> source-linked analysis -> edit matrix -> export -> reopen project.
- Verify valid, scanned, mixed, duplicate, corrupted, encrypted, empty, and oversized inputs, plus count and total-size violations. A bad input must not erase valid completed work.
- Test browser refresh, application/worker restart, interrupted processing, retry, cancellation, insufficient disk space, missing credentials, rate limits, and external-service failure.
- Test source-page mappings, invalid citations, unsupported answers, prompt injection in document text, project isolation, and deletion of derived records.
- Compare extracted metadata, numerical findings, quotations, and citations with a manually checked sample. Test retrieval using known relevant passages and include questions whose answers are absent.
- Run an ingestion benchmark with 100 representative PDFs and a separate worst-case test for the configured size targets. Record file sizes, page counts, scanned proportion, hardware/host, peak RAM, disk use, duration, failures, and API cost where applicable.
- Synthetic files can test byte limits but cannot establish extraction quality or representative processing speed. Do not substitute 100 tiny PDFs for a large-file capacity test.
- If a target is not met, report the measured boundary and the required change. Do not lower the target, hide failures, or claim readiness without evidence.
- Deliver working source, tests, dependency configuration, setup instructions, deployment requirements, and a short validation report. Separate completed features, measured capacity, and unresolved limitations.

## 12. Official documentation

Verify version-sensitive behaviour against the current documentation:

- [Claude Code project instructions](https://code.claude.com/docs/en/memory)
- [Streamlit file uploader](https://docs.streamlit.io/develop/api-reference/widgets/st.file_uploader)
- [Claude API PDF support](https://platform.claude.com/docs/en/build-with-claude/pdf-support)
