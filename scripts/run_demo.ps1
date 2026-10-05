# Start the sarvadristhi Forensic Workbench on http://127.0.0.1:8765 (local only) and open the browser.
#   powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
param([int]$Port = 8765, [switch]$NoBrowser)
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
if (-not $env:WEASYPRINT_DLL_DIRECTORIES -and (Test-Path C:\msys64\ucrt64\bin)) { $env:WEASYPRINT_DLL_DIRECTORIES = 'C:\msys64\ucrt64\bin' }
$ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if ((Test-Path $ollama) -and -not (Get-Process ollama -ErrorAction SilentlyContinue)) { Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden }
New-Item -ItemType Directory -Force cases | Out-Null
$args = @("-m", "sarvadristhi", "ui", "--cases", "cases", "--port", $Port)
if ($NoBrowser) { $args += "--no-browser" }
Write-Host "sarvadristhi workbench on http://127.0.0.1:$Port  (Ctrl+C to stop)" -ForegroundColor Green
& .\.venv\Scripts\python.exe @args
