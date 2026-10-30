# Store your Anthropic API key for this Windows user (not in the repository, not in any file here).
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\set-api-key.ps1
$secure = Read-Host "Paste your Anthropic API key (starts with sk-ant-)" -AsSecureString
$key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
if (-not $key.StartsWith("sk-ant-")) { throw "That does not look like an Anthropic API key." }
[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $key, "User")
# Stop any running worker so the next one picks up the key.
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'litreview\.worker' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Saved. Close this window, open a new PowerShell window, and start the app with scripts\run.ps1."
