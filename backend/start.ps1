# Launch the Archivist FastAPI backend.
#
# Usage:
#   .\start.ps1            # dev mode (reload, INFO logs)
#   .\start.ps1 -Prod      # no reload, WARNING logs

[CmdletBinding()]
param(
    [switch]$Prod
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# Activate venv if present
$venvActivate = Join-Path $here ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    & $venvActivate
} else {
    Write-Warning "No .venv found at $venvActivate. Using system Python."
}

# Load .env so $env:API_HOST and $env:API_PORT are available for the message below
$envFile = Join-Path $here ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#=]+?)\s*=\s*(.*)$') {
            $name = $matches[1].Trim()
            $value = $matches[2].Trim()
            # Strip surrounding quotes if present
            if ($value -match '^"(.*)"$' -or $value -match "^'(.*)'$") { $value = $matches[1] }
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

$apiHost = if ($env:API_HOST) { $env:API_HOST } else { "127.0.0.1" }
$apiPort = if ($env:API_PORT) { $env:API_PORT } else { "8000" }

Write-Host ""
Write-Host "Archivist backend starting on http://${apiHost}:${apiPort}" -ForegroundColor Cyan
Write-Host "  Docs:    http://${apiHost}:${apiPort}/docs" -ForegroundColor DarkCyan
Write-Host "  Health:  http://${apiHost}:${apiPort}/api/health" -ForegroundColor DarkCyan
Write-Host ""

# Launch via run.py — sets WindowsSelectorEventLoopPolicy BEFORE uvicorn imports
# anything. Required because psycopg async refuses to run on ProactorEventLoop.
python run.py
