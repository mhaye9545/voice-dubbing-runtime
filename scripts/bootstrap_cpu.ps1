[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$RuntimeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Uv = Join-Path $RuntimeRoot ".tools\uv\uv.exe"
$PythonRoot = Join-Path $RuntimeRoot ".python"
$Venv = Join-Path $RuntimeRoot ".venv-cpu"
$Python = Join-Path $Venv "Scripts\python.exe"
$Cache = Join-Path $RuntimeRoot ".cache\uv"
$Vendor = Join-Path $RuntimeRoot "vendor\TTS-ff217b3f27b294de194cc59c5119d1e08b06413c"
$VendorRequirements = Join-Path $Vendor "requirements.txt"

if (-not (Test-Path -LiteralPath $Uv)) {
    throw "Local uv is missing: $Uv"
}

if (-not (Test-Path -LiteralPath $Python)) {
    & $Uv python install 3.11 --install-dir $PythonRoot --cache-dir $Cache --no-bin --no-registry --managed-python
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $ManagedPython = Get-ChildItem -LiteralPath $PythonRoot -Directory -Filter "cpython-3.11.*-windows-x86_64-none" |
        Sort-Object Name -Descending |
        Select-Object -First 1 |
        ForEach-Object { Join-Path $_.FullName "python.exe" }
    if (-not $ManagedPython -or -not (Test-Path -LiteralPath $ManagedPython)) {
        throw "uv-managed Python 3.11 was not found under $PythonRoot"
    }

    & $Uv venv $Venv --python $ManagedPython --no-project
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if (-not (Test-Path -LiteralPath $VendorRequirements)) {
    throw "Pinned TTS source is missing: $VendorRequirements"
}

& $Uv pip install --python $Python --cache-dir $Cache --no-progress `
    torch==2.6.0 torchaudio==2.6.0 `
    --index-url "https://download.pytorch.org/whl/cpu"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $Uv pip install --python $Python --cache-dir $Cache --no-progress `
    -r $VendorRequirements `
    "numpy==1.26.4" `
    "transformers==4.49.0" `
    "huggingface-hub==0.36.2" `
    "psutil==7.2.2"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$env:PYTHONPATH = $Vendor
& $Python -c "import torch, torchaudio; from TTS.tts.models.xtts import Xtts; print(torch.__version__, torchaudio.__version__, 'TTS_IMPORT_PASS')"
exit $LASTEXITCODE
