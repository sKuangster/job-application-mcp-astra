# Start the ApplyPilot backend (workers + Firefox bridge + dashboard + MCP endpoint).
# Usage: .\scripts\run.ps1            (normal)
#        .\scripts\run.ps1 -Pair      (let the Firefox extension fetch its token for 5 minutes)
param([switch]$Pair, [string]$LogLevel = "")
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$exe = ".venv\Scripts\applypilot.exe"
if (-not (Test-Path $exe)) { Write-Host "Run .\scripts\setup.ps1 first." -ForegroundColor Red; exit 1 }
$argsList = @("run")
if ($Pair) { $argsList += "--pair" }
if ($LogLevel) { $argsList += @("--log-level", $LogLevel) }
& $exe @argsList
