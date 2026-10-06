# Run every test suite. Usage: .\scripts\test.ps1 [-Quick]
param([switch]$Quick)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".venv\Scripts\python.exe"
& $py -m ruff check src tests\unit tests\integration
& $py -m mypy
if ($Quick) { & $py -m pytest tests\unit -q } else { & $py -m pytest -q }
$pyStatus = $LASTEXITCODE
if (Get-Command node -ErrorAction SilentlyContinue) {
  node --test tests/js/
  node --test tests/js/ui/
  npx --no-install web-ext lint --source-dir firefox --ignore-files icons/make_icons.py
} else { Write-Host "Node.js not found: skipping extension tests" -ForegroundColor Yellow }
exit $pyStatus
