param([ValidateSet('install','start','stop','doctor','logs')][string]$Action = 'doctor', [switch]$HostOllama)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install Docker Desktop with WSL2 integration first.' }
if (-not $env:APPLYPILOT_SEARCH_SECRET) {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  $rng.GetBytes($bytes)
  $rng.Dispose()
  $env:APPLYPILOT_SEARCH_SECRET = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
}
if ($HostOllama) { $env:APPLYPILOT_RESEARCH_OLLAMA_URL = 'http://host.docker.internal:11434' }
else { $env:APPLYPILOT_RESEARCH_OLLAMA_URL = 'http://ollama:11434' }
function Compose {
  & docker compose -f services/research/compose.yaml @args
  if ($LASTEXITCODE -ne 0) { throw 'Docker Compose operation failed.' }
}
switch ($Action) {
  'install' { Compose pull }
  'start' {
    if ($HostOllama) { Compose up -d searxng perplexica }
    else {
      Compose up -d
      Compose exec -T ollama ollama pull qwen2.5:7b
      Compose exec -T ollama ollama pull nomic-embed-text
    }
    Write-Host 'Research UI: http://127.0.0.1:3001. Complete upstream setup once with local Ollama.'
  }
  'stop' { Compose stop }
  'doctor' { Compose ps; Invoke-RestMethod http://127.0.0.1:3001/api/providers -TimeoutSec 10; Invoke-RestMethod http://127.0.0.1:8081/healthz -TimeoutSec 10 }
  'logs' { Compose logs --tail 100 }
}
