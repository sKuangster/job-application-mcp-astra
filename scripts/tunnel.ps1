# Expose ONLY the MCP endpoint to ChatGPT over HTTPS using a Cloudflare quick tunnel.
# Requires cloudflared (winget install --id Cloudflare.cloudflared). Prints the URL to paste into ChatGPT.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$token = (Get-Content .env | Where-Object { $_ -match '^APPLYPILOT_MCP_TOKEN=' }) -replace '^APPLYPILOT_MCP_TOKEN=', '' -replace '\s*#.*$', ''
$port = (Get-Content .env | Where-Object { $_ -match '^APPLYPILOT_PORT=' }) -replace '^APPLYPILOT_PORT=', '' -replace '\s*#.*$', ''
if (-not $port) { $port = "8765" }
Write-Host "When cloudflared prints https://<name>.trycloudflare.com, your ChatGPT MCP URL is:" -ForegroundColor Green
Write-Host "  https://<name>.trycloudflare.com/mcp/$token" -ForegroundColor Green
Write-Host "Keep this URL private: it grants control of ApplyPilot." -ForegroundColor Yellow
cloudflared tunnel --url "http://127.0.0.1:$port"
