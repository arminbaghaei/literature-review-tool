# Literature Review Tool

A private, single-user research tool for working with about 100 research PDFs in one review
project. It extracts text with page references (with OCR for scanned pages), builds a keyword
index, and uses the Claude API for an editable literature review matrix, questions across papers,
cross-paper synthesis, summaries and draft review sections. Every AI claim carries document and
page citations that the tool checks programmatically.

It is a research assistant, not a systematic-review platform. You remain responsible for checking
interpretations against the sources.

## What works without an API key

Upload, folder import, text extraction, OCR, duplicate detection, the source viewer, keyword search,
manual matrix editing, themes, summaries you write yourself, and CSV/JSON export all work offline.
Matrix extraction, questions, synthesis, AI summaries and drafting need a Claude API key.

## Windows setup

Requirements: Windows 10 or 11, Python 3.13 (3.11 or newer works), about 2 GB free for the
environment plus space for your PDFs. No Tesseract or other system packages are needed: OCR uses
RapidOCR, which installs with pip and runs locally.

1. Install Python 3.13 from python.org and tick **Add python.exe to PATH**.
2. Clone the repository:

   ```powershell
   git clone https://github.com/arminbaghaei/literature-review-tool.git
   cd literature-review-tool
   ```

3. Create the environment and install dependencies. The script puts the virtual environment in
   `%LOCALAPPDATA%\LitReviewTool\venv`, outside OneDrive, because sync clients interfere with
   virtual environments and SQLite databases.

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
   ```

   Add `-Dev` to also install the test tools.

4. Start the app. It opens at <http://localhost:8501> and starts its background worker
   automatically.

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\run.ps1
   ```

### Configure the Claude API

The app reads the key from the environment only. It never shows, stores, logs or exports it.
A Claude chat subscription or Claude Code login does not give API access; create a key in the
Claude Console.

```powershell
# Current window only
$env:ANTHROPIC_API_KEY = "sk-ant-..."
# Or permanently for your Windows user (then open a new terminal)
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")
```

Restart the app after setting the key, and stop any running worker so the new one inherits the
key (closing the app window does not stop the worker; use Task Manager or
`Get-CimInstance Win32_Process | ? CommandLine -match 'litreview.worker' | % { Stop-Process $_.ProcessId }`).

Optional settings (see `.env.example` for the full list):

| Variable | Default | Purpose |
|---|---|---|
| `LITREVIEW_MODEL` | `claude-opus-5-5` | Claude model for all AI features |
| `LITREVIEW_EFFORT` | `medium` | Reasoning effort (`low`, `medium`, `high`, ...) |
| `LITREVIEW_DEFAULT_BUDGET_USD` | `10` | Spending limit for new projects (editable per project) |
| `LITREVIEW_PRICE_<MODEL>` | built in | `input,output` USD per million tokens, for cost control |
| `LITREVIEW_REFUSAL_FALLBACK` | `1` | Use the API's server-side fallback if the model declines |
| `LITREVIEW_DATA_DIR` | `%LOCALAPPDATA%\LitReviewTool\data` | Where projects are stored |
| `LITREVIEW_APP_PASSWORD` | unset | Password prompt; set before any network exposure |

**What leaves your computer.** Nothing, until you approve an AI run. An approved run sends the
extracted text of the papers in scope (or their extracted evidence records) to Anthropic's API.
Anthropic's data retention policy then applies; deleting a project here cannot delete it there.

## Using the tool

1. **Projects**: create a project and, optionally, a review question.
2. **Upload & processing**: add PDFs from the browser, or use **Import from a folder** for large
   batches. Each file gets an outcome: accepted, duplicate (identical content) or rejected with a
   reason. Processing status, per-document progress, failures, retry and cancel are on the same
   page. Closing the browser or restarting the computer does not lose work; processing resumes from
   the last saved page.
3. **Documents & sources**: coverage per paper (readable, OCR and unreadable pages), probable
   duplicate review, inclusion decisions, study groups, and a source viewer showing the original
   page image beside the extracted text.
4. **Search & ask**: keyword search (no AI) and questions. *Focused* questions answer from the top
   matching passages and say so. *Exhaustive* questions examine every eligible paper and report a
   coverage row for each one.
5. **Review matrix**: editable matrix with a status for every cell (reported with verified quote,
   requires verification, not reported, not found in extracted text, heuristic, researcher).
   Your edits are kept, with history; later AI runs only add suggestions.
6. **Compare & themes**: themes, comparisons, conflicting findings and possible research gaps,
   built from the matrix evidence of every eligible paper. Themes are editable.
7. **Summaries & draft**: editable paper summaries and draft review sections with `[D001 p.3]`
   citations, a citation check and a reference list built only from extracted metadata.
8. **Export**: matrix and evidence as CSV and JSON; summaries, answers, synthesis and drafts as
   Markdown.

Every paid run shows an estimate first and needs explicit approval of a maximum spend. The
project spending limit is enforced before each call.

### Citation format and checks

Citations use the tool's document label and the **PDF page number** (not the journal's printed page
number; printed labels are shown in the source viewer when the PDF provides them). The tool checks
that each cited document is in scope, the page exists and was readable, and quotations match the
page text. These checks do not prove that a page supports a claim; read the source.

## Commands

```powershell
$py = "$env:LOCALAPPDATA\LitReviewTool\venv\Scripts\python.exe"
& $py -m pytest                       # test suite (synthetic PDFs, fake AI provider; no network)
& $py -m ruff check .                 # lint
& $py -m ruff format --check .        # formatting
& $py -m streamlit run app.py         # app
& $py -m litreview.worker             # worker on its own (optional; the app starts one)
& $py benchmarks\ingest_benchmark.py --pdf-dir <folder> --data-dir <empty temp folder>
& $py benchmarks\e2e_browser.py --url http://localhost:8501 --workdir <temp> --db <data>\litreview.db
```

`benchmarks\e2e_browser.py` needs `pip install playwright` and `python -m playwright install chromium`.

## Architecture

| Module | Responsibility |
|---|---|
| `app.py`, `litreview/ui/` | Streamlit pages, password gate, worker autostart |
| `litreview/ingest.py`, `filestore.py` | Batch limits, signature check, streamed hashing, safe storage names |
| `litreview/pdf_extract.py`, `ocr.py` | Page-by-page extraction (pypdfium2), OCR (RapidOCR), quality flags |
| `litreview/chunking.py`, `search.py` | Page-bounded chunks, SQLite FTS5 keyword search |
| `litreview/jobs.py`, `worker.py` | Persisted jobs, recovery, cancellation, bounded worker |
| `litreview/ai/` | Claude adapter, budget, caching, versioned prompts and schemas |
| `litreview/analysis/` | Matrix extraction, summaries, questions, synthesis, drafting, runs |
| `litreview/citations.py`, `matrix.py` | Citation and quotation checks, matrix provenance |
| `litreview/export.py` | CSV, JSON and Markdown exports |

Records live in one SQLite database (WAL mode) and original PDFs in
`<data dir>\projects\<project id>\originals\` under generated names. Originals are never modified.
Each PDF is processed in its own child process with a timeout, so a bad file only fails itself.

## Deployment

The supported deployment is **local, single user**: the app binds to `localhost` only
(`.streamlit/config.toml`).

To run it for yourself on another Windows machine or a Windows VM, repeat the setup steps there.
To make it reachable over a network:

- Set `LITREVIEW_APP_PASSWORD`, and put the app behind an HTTPS reverse proxy (for example IIS
  with URL Rewrite, or Caddy). Change `server.address` only after both are in place.
- Raise the proxy's request-body limit to at least the per-file limit (100 MiB), and its timeouts
  for long uploads. Streamlit uploads each file in a separate request.
- Use persistent local disk for `LITREVIEW_DATA_DIR`. The app is single-user: everyone who knows
  the password sees every project.
- Run the worker as a service (for example with NSSM: `python -m litreview.worker`) and set
  `LITREVIEW_NO_WORKER_AUTOSTART=1` for the app.

Streamlit Community Cloud and similar free hosts are not suitable: they have small memory limits,
ephemeral storage and public-by-default apps, which conflicts with private research data.

## Measured capacity

See the validation report below.

## Licences

Dependencies use permissive licences: Streamlit, pypdfium2/PDFium, RapidOCR (and its PaddleOCR
models), opencv-python-headless and pytest (Apache-2.0 or MIT); ONNX Runtime and the Anthropic SDK
(MIT); pandas, NumPy, psutil, reportlab and pypdf (BSD); Pillow (MIT-CMU). opencv-python-headless
bundles LGPL FFmpeg libraries. PyMuPDF was avoided because of its AGPL licence.
