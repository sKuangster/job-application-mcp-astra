# Build (unsigned zip) or sign (permanent install) the Firefox extension.
# Usage: .\scripts\package-extension.ps1          → dist\applypilot-1.0.0.zip
#        .\scripts\package-extension.ps1 -Sign    → signed .xpi (needs WEB_EXT_API_KEY/SECRET from addons.mozilla.org)
param([switch]$Sign)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($Sign) {
  if (-not $env:WEB_EXT_API_KEY) {
    Get-Content .env | Where-Object { $_ -match '^(WEB_EXT_API_KEY|WEB_EXT_API_SECRET)=(.+)$' } | ForEach-Object {
      $k, $v = $_ -split '=', 2; Set-Item -Path "env:$k" -Value $v.Split('#')[0].Trim() }
  }
  npx --no-install web-ext sign --source-dir firefox --artifacts-dir dist --channel unlisted --ignore-files icons/make_icons.py
} else {
  npx --no-install web-ext build --source-dir firefox --artifacts-dir dist --overwrite-dest --ignore-files icons/make_icons.py
}
