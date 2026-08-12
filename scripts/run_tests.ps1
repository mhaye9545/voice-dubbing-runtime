[CmdletBinding()]
param([string]$PythonExecutable = "")

$ErrorActionPreference = "Stop"
$RuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Python = if ($PythonExecutable) {
    [System.IO.Path]::GetFullPath($PythonExecutable)
} else {
    Join-Path $RuntimeRoot ".venv-dev\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Development Python is missing. Run scripts/bootstrap_dev.ps1 first: $Python"
}
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONUTF8 = "1"
& $Python -m unittest discover -s tests -t . -p "test_*.py" -v
exit $LASTEXITCODE
