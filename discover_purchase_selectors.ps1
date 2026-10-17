# Run locally on the Windows VPS, from a regular PowerShell session.
$ErrorActionPreference = 'Stop'
$PurchasePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PurchasePython)) {
    Write-Error 'Create the project virtual environment and install requirements first.'
    exit 1
}
# No credentials, tokens or browser-debug options are passed on the command line.
& $PurchasePython -X utf8 (Join-Path $PSScriptRoot 'discover_purchase_selectors.py')
exit $LASTEXITCODE
