<#
.SYNOPSIS
    Builds the Hinglish speech models for LocalNotes (Oriserve Whisper-Hindi2Hinglish, Apache-2.0).

.DESCRIPTION
    - Apex  (Whisper large-v3-turbo fine-tune) — the after-call accuracy pass for Hinglish meetings.
    - Swift (Whisper base fine-tune)           — the live transcript for Hinglish meetings.
    Both are published in Hugging Face Transformers format; LocalNotes' speech engine (faster-whisper) needs
    CTranslate2 format, so they are converted once into %APPDATA%\LocalNotes\models. The converter needs
    PyTorch, so it runs in a throwaway virtual environment that is deleted afterwards — LocalNotes itself
    never depends on PyTorch. Safe to re-run; models that are already built are skipped.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\tools\build_hinglish_model.ps1
#>
param([string]$Python = "")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Models = Join-Path $env:APPDATA "LocalNotes\models"
$Work = Join-Path $env:TEMP "localnotes-model-convert"
$Wanted = @(
    @{ Repo = "Oriserve/Whisper-Hindi2Hinglish-Apex";  Out = "whisper-hinglish-apex" },
    @{ Repo = "Oriserve/Whisper-Hindi2Hinglish-Swift"; Out = "whisper-hinglish-swift" }
) | Where-Object { -not (Test-Path (Join-Path $Models "$($_.Out)\model.bin")) }

if (-not $Wanted) { Write-Host "Hinglish models already built in $Models"; exit 0 }
$AppPy = Join-Path (Split-Path $PSScriptRoot) ".venv\Scripts\python.exe"
if (-not $Python) { $Python = Join-Path (& $AppPy -c "import sys; print(sys.base_prefix)").Trim() "python.exe" }
$ct2 = (& $AppPy -c "import ctranslate2; print(ctranslate2.__version__)").Trim()

Write-Host "==> Temporary converter environment (PyTorch CPU, transformers, ctranslate2 $ct2)"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
& $Python -m venv $Work
$py = Join-Path $Work "Scripts\python.exe"
& $py -m pip install --quiet --disable-pip-version-check --upgrade pip
& $py -m pip install --quiet --disable-pip-version-check torch --index-url https://download.pytorch.org/whl/cpu
& $py -m pip install --quiet --disable-pip-version-check "ctranslate2==$ct2" "transformers>=4.40,<5" safetensors sentencepiece
if ($LASTEXITCODE -ne 0) { throw "Installing the converter failed." }

foreach ($m in $Wanted) {
    $out = Join-Path $Models $m.Out
    $tmp = "$out.partial"
    Write-Host "==> Downloading $($m.Repo) and converting to CTranslate2 (int8)"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    & (Join-Path $Work "Scripts\ct2-transformers-converter.exe") --model $m.Repo --output_dir $tmp `
        --quantization int8 --copy_files tokenizer.json preprocessor_config.json
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $tmp "model.bin"))) { throw "Converting $($m.Repo) failed." }
    if (Test-Path $out) { Remove-Item -Recurse -Force $out }
    Move-Item $tmp $out
    # the original weights aren't needed after conversion
    $cache = Join-Path $env:USERPROFILE (".cache\huggingface\hub\models--" + $m.Repo.Replace("/", "--"))
    if (Test-Path $cache) { Remove-Item -Recurse -Force $cache }
    $mb = [math]::Round(((Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum) / 1MB)
    Write-Host "    built $out ($mb MB)" -ForegroundColor Green
}

Write-Host "==> Cleaning up the temporary environment"
Remove-Item -Recurse -Force $Work
Write-Host "Done." -ForegroundColor Green
