[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$VenvPath = ""
)

$ErrorActionPreference = "Stop"
$RuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Venv = if ($VenvPath) { [System.IO.Path]::GetFullPath($VenvPath) } else { Join-Path $RuntimeRoot ".venv-cpu" }
$Python = Join-Path $Venv "Scripts\python.exe"
$Lock = Join-Path $RuntimeRoot "requirements-cpu.lock.txt"
$Vendor = Join-Path $RuntimeRoot "vendor\TTS-ff217b3f27b294de194cc59c5119d1e08b06413c"

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
    throw "CPU lock is missing: $Lock"
}
if (-not (Test-Path -LiteralPath (Join-Path $Vendor "TTS\tts\models\xtts.py") -PathType Leaf)) {
    throw "Pinned vendored TTS source is incomplete: $Vendor"
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $BasePython = Resolve-Python311 $PythonExecutable
    & $BasePython -m venv $Venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

& $Python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 2)"
if ($LASTEXITCODE -ne 0) { throw "Existing CPU environment is not Python 3.11: $Python" }
& $Python -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m pip install --require-hashes --no-deps -r $Lock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:PYTHONPATH = $Vendor
& $Python -c "import importlib.metadata as m, pathlib, TTS, torch, torchaudio; names={str(d.metadata.get('Name','')).lower() for d in m.distributions()}; assert 'tts' not in names and 'coqui-tts' not in names; assert not torch.cuda.is_available(); assert pathlib.Path(TTS.__file__).resolve().is_relative_to(pathlib.Path(r'$Vendor').resolve()); from TTS.tts.configs.xtts_config import XttsConfig; from TTS.tts.models.xtts import Xtts; print(torch.__version__, torchaudio.__version__, 'VENDOR_TTS_IMPORT_PASS')"
$ExitCode = $LASTEXITCODE
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
exit $ExitCode
