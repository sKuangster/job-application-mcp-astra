# ApplyPilot setup for Windows (PowerShell 5+). Usage:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Need($cmd, $hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Write-Host "Missing: $cmd. $hint" -ForegroundColor Red; exit 1 }
}

# 1. Python 3.11+ virtual environment
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv .venv --python 3.12
  } else {
    Need "py" "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')."
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { py -3 -m venv .venv }
  }
}
$py = ".venv\Scripts\python.exe"
& $py -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
if (Get-Command uv -ErrorAction SilentlyContinue) {
  uv pip install --python $py -e ".[dev]"
} else {
  & $py -m pip install --upgrade pip
  & $py -m pip install -e ".[dev]"
}

# 2. Node.js (only for extension tests / packaging; the extension itself needs no build)
if (Get-Command npm -ErrorAction SilentlyContinue) {
  npm install --no-audit --no-fund
} else {
  Write-Host "Node.js not found: JS tests and 'npm run build:extension' unavailable (install from https://nodejs.org)." -ForegroundColor Yellow
}

# 3. Tokens, settings.yaml, database
& ".venv\Scripts\applypilot.exe" setup
Write-Host "`nNext: fill config\candidate.local.yaml + config\preferences.yaml, put resumes in resumes\, then .\scripts\run.ps1" -ForegroundColor Green
