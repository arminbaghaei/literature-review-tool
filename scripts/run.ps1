# Start the app (and its background worker) on http://localhost:8501
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\run.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $env:LOCALAPPDATA "LitReviewTool\venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run scripts\setup.ps1 first." }
if (-not $env:ANTHROPIC_API_KEY) { Write-Host "Note: ANTHROPIC_API_KEY is not set. AI features will be disabled; upload, extraction and search still work." }
Set-Location $root
& $python -m streamlit run app.py
