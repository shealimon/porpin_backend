# Pehle API:  .\run_api.ps1 -LoadTest   (1000 user / rate limit band — sirf local test)
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

foreach ($name in @(".venv", "venv")) {
    $act = Join-Path $Root "$name\Scripts\Activate.ps1"
    if (Test-Path $act) {
        . $act
        break
    }
}

$pyArgs = @(
    "load_test.py",
    "--presets",
    "--quiet",
    "--file", "sample.txt",
    "--url", "http://127.0.0.1:8000/translate"
)
if ($Rest -and $Rest.Count -gt 0) {
    $pyArgs += $Rest
}

$env:PYTHONUNBUFFERED = "1"
& python -u @pyArgs
exit $LASTEXITCODE
