<#
.SYNOPSIS
    One-command setup for LocalNotes on Windows. Safe to re-run: each step skips what's already done.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup.ps1

.PARAMETER NoShortcuts
    Don't create Start Menu / desktop shortcuts.
.PARAMETER SkipOllama
    Don't install Ollama or download the AI model (AI notes won't work until you do).
.PARAMETER SkipSpeechModels
    Don't pre-download the Whisper speech models (they download on first use instead).
#>
param(
    [switch]$NoShortcuts,
    [switch]$SkipOllama,
    [switch]$SkipSpeechModels
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is very slow with the progress bar on

$Root = $PSScriptRoot
$Data = Join-Path $env:APPDATA "LocalNotes"
$Models = Join-Path $Data "models"
$OllamaModel = "llama3.2:3b"
$SegUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
$EmbUrl = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    ok  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    !!  $msg" -ForegroundColor Yellow }
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Write-Host "LocalNotes setup" -ForegroundColor White
Write-Host "Installs into: $Root   |   Data lives in: $Data"

# --- 1. Python 3.12 --------------------------------------------------------
Step "Python 3.12"
function Find-Python {
    if (Have "py") {
        $p = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
    }
    $default = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $default) { return $default }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    if (-not (Have "winget")) { throw "Python 3.12 is required. Install it from https://www.python.org/downloads/ and run setup again." }
    Write-Host "    installing Python 3.12 with winget..."
    winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    $Python = Find-Python
    if (-not $Python) { throw "Python 3.12 was installed but can't be found yet. Open a new terminal and run setup again." }
}
Ok $Python

# --- 2. Virtual environment + packages --------------------------------------
Step "Python packages"
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { & $Python -m venv (Join-Path $Root ".venv") }
& $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
& $VenvPy -m pip install -r (Join-Path $Root "requirements.txt") --quiet --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "pip install failed - see the errors above." }
Ok "installed from requirements.txt"

# --- 3. Speaker-recognition models (~45 MB) ----------------------------------
Step "Speaker-recognition models"
New-Item -ItemType Directory -Force $Models | Out-Null
$SegModel = Join-Path $Models "sherpa-onnx-pyannote-segmentation-3-0\model.onnx"
$EmbModel = Join-Path $Models "nemo_en_titanet_small.onnx"
if (-not (Test-Path $SegModel)) {
    $tmp = Join-Path $env:TEMP "localnotes-seg.tar.bz2"
    Invoke-WebRequest $SegUrl -OutFile $tmp
    tar -xjf $tmp -C $Models
    Remove-Item $tmp -Force
}
if (-not (Test-Path $EmbModel)) { Invoke-WebRequest $EmbUrl -OutFile $EmbModel }
Ok $Models

# --- 4. Ollama + local AI model (~2 GB) -------------------------------------
if ($SkipOllama) {
    Warn "skipped Ollama (AI notes need it: https://ollama.com, then 'ollama pull $OllamaModel')"
} else {
    Step "Ollama and the $OllamaModel AI model"
    $OllamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Have "ollama") { $OllamaExe = (Get-Command ollama).Source }
    if (-not (Test-Path $OllamaExe)) {
        if (-not (Have "winget")) { throw "Install Ollama from https://ollama.com and run setup again (or use -SkipOllama)." }
        Write-Host "    installing Ollama with winget..."
        winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
    }
    try { Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null }
    catch { Start-Process $OllamaExe -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep -Seconds 5 }
    $models = (Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 10).models | ForEach-Object { $_.name }
    if ($models -notcontains $OllamaModel) {
        Write-Host "    downloading $OllamaModel (about 2 GB)..."
        & $OllamaExe pull $OllamaModel
    }
    Ok "$OllamaModel ready"
}

# --- 5. Speech models (~1.8 GB, otherwise downloaded on first use) ----------
if ($SkipSpeechModels) {
    Warn "skipped speech models (they download the first time they're needed)"
} else {
    Step "Speech-to-text models"
    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
    & $VenvPy -c "from faster_whisper import WhisperModel as W; [W(m, device='cpu', compute_type='int8') for m in ('base.en', 'large-v3-turbo')]" 2>$null
    if ($LASTEXITCODE -ne 0) { Warn "couldn't pre-download speech models; they'll download on first use" } else { Ok "base.en (live) and large-v3-turbo (accuracy pass)" }
}

# --- 6. Shortcuts -------------------------------------------------------------
if (-not $NoShortcuts) {
    Step "Shortcuts"
    $shell = New-Object -ComObject WScript.Shell
    $targets = @(
        (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\LocalNotes.lnk"),
        (Join-Path ([Environment]::GetFolderPath("Desktop")) "LocalNotes.lnk")
    )
    foreach ($t in $targets) {
        $s = $shell.CreateShortcut($t)
        $s.TargetPath = Join-Path $Root ".venv\Scripts\pythonw.exe"
        $s.Arguments = '"' + (Join-Path $Root "app.py") + '"'
        $s.WorkingDirectory = $Root
        $s.IconLocation = Join-Path $Root "assets\localnotes.ico"
        $s.Description = "Private, local meeting notes"
        $s.Save()
    }
    Ok "Start Menu and desktop"
}

# --- 7. Self-check ---------------------------------------------------------------
Step "Self-check"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"
& $VenvPy (Join-Path $Root "selfcheck.py")
if ($LASTEXITCODE -ne 0) { throw "Self-check failed - see above." }

Write-Host "`nDone. Launch LocalNotes from the Start Menu, or run: .\LocalNotes.bat" -ForegroundColor Green
