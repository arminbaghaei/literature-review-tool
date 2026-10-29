# One-time setup on Windows. Creates a virtual environment OUTSIDE OneDrive and installs dependencies.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-Dev]
param([switch]$Dev)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $env:LOCALAPPDATA "LitReviewTool\venv"
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "Creating virtual environment at $venv"
    if ($py) { & py -3.13 -m venv $venv } else { & python -m venv $venv }
}
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --upgrade pip
if ($Dev) { & $python -m pip install -r (Join-Path $root "requirements-dev.txt") }
else { & $python -m pip install -r (Join-Path $root "requirements.txt") }
Write-Host "Warming up OCR models (first run downloads nothing; models ship with rapidocr)..."
& $python -c "from rapidocr import RapidOCR; RapidOCR(); print('OCR ready')"
Write-Host "Setup complete. Start the app with: powershell -ExecutionPolicy Bypass -File scripts\run.ps1"
