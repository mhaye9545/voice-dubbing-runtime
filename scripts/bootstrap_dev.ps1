[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$VenvPath = ""
)

$ErrorActionPreference = "Stop"
$RuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Venv = if ($VenvPath) { [System.IO.Path]::GetFullPath($VenvPath) } else { Join-Path $RuntimeRoot ".venv-dev" }
$Python = Join-Path $Venv "Scripts\python.exe"
$Lock = Join-Path $RuntimeRoot "requirements-dev.lock.txt"

function Resolve-Python311([string]$Requested) {
    $Candidate = if ($Requested) { $Requested } else { (Get-Command python -ErrorAction Stop).Source }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        throw "Python executable does not exist: $Candidate"
    }
    $Version = & $Candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0 -or $Version.Trim() -ne "3.11") {
        throw "Python 3.11 is required; received $Version from $Candidate"
    }
    return [System.IO.Path]::GetFullPath($Candidate)
}

if (-not (Test-Path -LiteralPath $Lock -PathType Leaf)) {
    throw "Development lock is missing: $Lock"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $BasePython = Resolve-Python311 $PythonExecutable
    & $BasePython -m venv $Venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $Python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 2)"
if ($LASTEXITCODE -ne 0) { throw "Existing development environment is not Python 3.11: $Python" }
& $Python -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install --require-hashes --no-deps -r $Lock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install --no-deps -e $RuntimeRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -c "import PySide6, numpy, voice_dubbing_app, voice_dubbing_runtime; print('DEV_IMPORT_PASS')"
exit $LASTEXITCODE
