# Optional: run the background worker in its own window (the app also starts one automatically).
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $env:LOCALAPPDATA "LitReviewTool\venv\Scripts\python.exe"
Set-Location $root
& $python -m litreview.worker
