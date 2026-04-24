# Run API without --reload (better for load tests). Optional multi-process on non-Windows.
# Windows: 1000+ concurrent connections may need -Workers 2+ to spread sockets, or set
#   HTTP_RATE_LIMIT_ENABLED=false / HTTP_TRANSLATE_RATE_LIMIT=2000/minute for load tests.
# Usage:
#   .\run_api.ps1
#   .\run_api.ps1 -LoadTest              # disables SlowAPI limits for this run only (1000+ user tests)
#   .\run_api.ps1 -Workers 4 -LoadTest   # recommended for heavy load on Windows
#   .\run_api.ps1 -BindHost 0.0.0.0 -Port 8000   # (-Host is reserved in PowerShell)

param(
    [int]$Workers = 1,
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [switch]$LoadTest
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if ($LoadTest) {
    $env:HTTP_RATE_LIMIT_ENABLED = "false"
    Write-Host "[LoadTest] HTTP_RATE_LIMIT_ENABLED=false (sirf is process ke liye)" -ForegroundColor Cyan
}

foreach ($name in @(".venv", "venv")) {
    $act = Join-Path $PSScriptRoot "$name\Scripts\Activate.ps1"
    if (Test-Path $act) {
        . $act
        break
    }
}

$env:PYTHONUNBUFFERED = "1"
if ($Workers -gt 1) {
    & python -m uvicorn app.main:app --host $BindHost --port $Port --workers $Workers
} else {
    & python -m uvicorn app.main:app --host $BindHost --port $Port
}
exit $LASTEXITCODE
