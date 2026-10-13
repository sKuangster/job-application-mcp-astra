# Run every test suite. Usage: .\scripts\test.ps1 [-Quick]
param([switch]$Quick)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$py = ".venv\Scripts\python.exe"
& $py -m ruff check src tests\unit tests\integration
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m mypy
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
if ($Quick) { & $py -m pytest tests\unit -q } else { & $py -m pytest tests\unit tests\integration tests\test_db.py tests\test_state.py -q }
$pyStatus = $LASTEXITCODE
if ($pyStatus -ne 0) { exit $pyStatus }
if (Get-Command node -ErrorAction SilentlyContinue) {
  node --test tests/js/
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  node --test tests/js/ui/
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  node --test tests/browser/dom.test.cjs tests/browser/ownership.test.cjs
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  npx --no-install web-ext lint --source-dir firefox --ignore-files icons/make_icons.py
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} else { Write-Host "Node.js not found: skipping extension tests" -ForegroundColor Yellow }
exit $pyStatus
