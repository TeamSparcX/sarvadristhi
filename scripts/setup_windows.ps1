# sarvadristhi one-time setup on Windows 10/11 (no WSL). Run from the project folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
# Steps it performs (each one is skipped if already done):
#   1. system tools via winget: Python 3.12, Git, FFmpeg (with libx264), Tesseract OCR, MSYS2, Ollama, GitHub CLI
#      (Tesseract and MSYS2 show a UAC prompt - approve them)
#   2. Pango for PDF reports (MSYS2) + WEASYPRINT_DLL_DIRECTORIES; Tesseract on the user PATH
#   3. DejaVu Sans Mono font for the current user (used by the test evidence overlays)
#   4. .venv with sarvadristhi, PyTorch (CUDA 12.8 if an NVIDIA GPU is present, else CPU), RF-DETR, pytsk3, libewf
#   5. demo data: kept if already present, else downloaded from the GitHub Release (or build it yourself:
#      scripts\build_data.ps1), then verified file by file against scripts\DATA_MANIFEST.json
#   6. local AI models in Ollama: qwen3-vl:4b (VLM + narrative)
param([switch]$SkipInstall, [switch]$SkipData, [switch]$SkipOllama, [string]$Repo = "aditya36900/ProjectR",
      [string]$Release = "v1.0-data")
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root
function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function RefreshPath { $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') }
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

if (-not $SkipInstall) {
    Step "1. System tools (winget)"
    $pkgs = @(
        @{id="Python.Python.3.12"; scope="user"}, @{id="Git.Git"}, @{id="Gyan.FFmpeg"},
        @{id="UB-Mannheim.TesseractOCR"}, @{id="MSYS2.MSYS2"}, @{id="Ollama.Ollama"}, @{id="GitHub.cli"})
    foreach ($p in $pkgs) {
        $installed = winget list --id $p.id --exact --accept-source-agreements 2>$null | Select-String $p.id
        if ($installed) { Write-Host "ok  $($p.id)"; continue }
        Write-Host "installing $($p.id) ..."
        $args = @("install", "--id", $p.id, "--exact", "--silent", "--accept-package-agreements", "--accept-source-agreements")
        if ($p.scope) { $args += @("--scope", $p.scope) }
        winget @args
    }
    RefreshPath

    Step "2. Tesseract on PATH, Pango for PDF reports"
    $tess = "C:\Program Files\Tesseract-OCR"
    $u = [Environment]::GetEnvironmentVariable('Path','User')
    if ((Test-Path "$tess\tesseract.exe") -and (($u -split ';') -notcontains $tess)) {
        [Environment]::SetEnvironmentVariable('Path', ($u.TrimEnd(';') + ';' + $tess), 'User') }
    if (Test-Path C:\msys64\usr\bin\bash.exe) {
        if (-not (Test-Path C:\msys64\ucrt64\bin\libpango-1.0-0.dll)) {
            C:\msys64\usr\bin\bash.exe -lc "pacman -Sy --noconfirm --needed mingw-w64-ucrt-x86_64-pango" }
        [Environment]::SetEnvironmentVariable('WEASYPRINT_DLL_DIRECTORIES', 'C:\msys64\ucrt64\bin', 'User')
        $env:WEASYPRINT_DLL_DIRECTORIES = 'C:\msys64\ucrt64\bin'   # this session too (step 4 imports WeasyPrint)
        # Tesseract ships its own, older Pango DLLs. WeasyPrint resolves some libraries through PATH, so
        # MSYS2's ucrt64\bin must come first or PDF rendering fails with error 0x7f (missing procedure).
        $u = [Environment]::GetEnvironmentVariable('Path','User')
        $rest = ($u -split ';') | Where-Object { $_ -and $_ -ne 'C:\msys64\ucrt64\bin' }
        [Environment]::SetEnvironmentVariable('Path', ((@('C:\msys64\ucrt64\bin') + $rest) -join ';'), 'User')
    } else { Write-Warning "MSYS2 not found: PDF reports disabled (HTML reports still work)" }
    RefreshPath
    foreach ($t in "ffmpeg", "ffprobe", "tesseract") { if (-not (Has $t)) { Write-Warning "$t not on PATH - open a new terminal and re-run" } }

    Step "3. DejaVu Sans Mono font (current user)"
    $fonts = "$env:LOCALAPPDATA\Microsoft\Windows\Fonts"
    if (-not (Test-Path "$fonts\DejaVuSansMono.ttf")) {
        $tmp = Join-Path $env:TEMP "dejavu"; New-Item -ItemType Directory -Force $tmp | Out-Null
        Invoke-WebRequest -UseBasicParsing "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip" -OutFile "$tmp\d.zip"
        Expand-Archive "$tmp\d.zip" $tmp -Force
        New-Item -ItemType Directory -Force $fonts | Out-Null
        Get-ChildItem $tmp -Recurse -Filter "DejaVuSansMono*.ttf" | ForEach-Object {
            Copy-Item $_.FullName $fonts -Force
            New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows NT\CurrentVersion\Fonts" -Force -PropertyType String `
                -Name (($_.BaseName -replace 'DejaVuSansMono','DejaVu Sans Mono' -replace '-',' ') + " (TrueType)") -Value "$fonts\$($_.Name)" | Out-Null }
    }
    Write-Host "ok  DejaVu Sans Mono"

    Step "4. Python environment (.venv)"
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) { $py = (Get-Command python).Source }
    if (-not (Test-Path .venv\Scripts\python.exe)) { & $py -m venv .venv }
    $vpy = ".\.venv\Scripts\python.exe"
    & $vpy -m pip install -q --upgrade pip
    $gpu = [bool](Get-CimInstance Win32_VideoController | Where-Object Name -match "NVIDIA")
    if ($gpu) { & $vpy -m pip install -q torch torchvision --index-url https://download.pytorch.org/whl/cu128 }
    else { & $vpy -m pip install -q torch torchvision }
    & $vpy -m pip install -q -e ".[pdf,test]" rfdetr pytsk3
    & $vpy -m pip install -q libewf-python 2>$null
    & $vpy -c "import torch, rfdetr, pytsk3, cv2, weasyprint; print('python ok, CUDA:', torch.cuda.is_available())"
}

if (-not $SkipData) {
    Step "5. Demo data"
    $vpy = ".\.venv\Scripts\python.exe"
    & $vpy scripts\data_tool.py verify *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "ok  all demo data present and verified" }
    else {
        if (Has gh) {
            New-Item -ItemType Directory -Force dist | Out-Null
            Write-Host "downloading release $Release from $Repo (about 3.5 GB) ..."
            gh release download $Release -R $Repo -D dist --skip-existing
            & $vpy scripts\data_tool.py unpack dist
        } else { Write-Warning "GitHub CLI not found: download the release zips into .\dist manually, then run: .venv\Scripts\python.exe scripts\data_tool.py unpack dist   (or build the data: scripts\build_data.ps1)" }
        & $vpy scripts\data_tool.py verify
    }
}

if (-not $SkipOllama) {
    Step "6. Local AI models (Ollama)"
    RefreshPath
    $ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ollama)) { $ollama = "ollama" }
    & $ollama pull qwen3-vl:4b
}
Write-Host "`nSetup complete. Start the workbench with:  powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1" -ForegroundColor Green
