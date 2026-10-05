# Rebuild ALL demo data from scratch instead of downloading it (about 10 minutes + 726 MB download).
#   powershell -ExecutionPolicy Bypass -File scripts\build_data.ps1
# Steps:
#   1. kit\               synthetic test evidence (Hikvision/Dahua images, exports, ground truth) - byte-reproducible
#   2. kit\meva\          six real CCTV clips + human activity labels from the MEVA dataset (CC-BY-4.0, AWS, no account)
#   3. kit\realcams\      two overlapping real-footage cameras (OpenCV pedestrian sample)
#   4. kit\scenario\      the poisoned-evidence bench: DVR-A (Hikvision), DVR-B (Dahua), USB export files, scenario.json
#   5. kit\usb_demo.vhd and kit\scenario\images\USB_shiftB.vhd  - virtual NTFS "USB sticks" with a deleted file.
#      Windows needs Administrator for diskpart/fsutil, so each VHD step opens ONE UAC prompt. No physical disk is touched.
param([switch]$SkipVhd)
$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
$vpy = ".\.venv\Scripts\python.exe"
function Step($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

Step "1. Synthetic test evidence -> kit\"
$tmp = Join-Path $env:TEMP "dvrf_kit_build"; if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
& $vpy -m pramanshala $tmp
New-Item -ItemType Directory -Force kit | Out-Null
foreach ($d in "images", "exports", "ground_truth") { if (Test-Path "kit\$d") { Remove-Item -Recurse -Force "kit\$d" }; Copy-Item -Recurse "$tmp\$d" "kit\$d" }
Copy-Item "$tmp\manifest.json", "$tmp\SHA256SUMS" kit\ -Force

Step "2. Real footage from MEVA -> kit\meva\"
& $vpy -m pramanshala.fetch_meva kit\meva

Step "3. Two-camera real footage -> kit\realcams\"
New-Item -ItemType Directory -Force kit\real | Out-Null
if (-not (Test-Path kit\real\vtest.avi)) {
    Invoke-WebRequest -UseBasicParsing "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/vtest.avi" -OutFile kit\real\vtest.avi }
& $vpy -m pramanshala.realcams kit\real\vtest.avi kit\realcams

Step "4. Poisoned-evidence scenario -> kit\scenario\"
& $vpy -m pramanshala.scenario kit\meva kit\scenario

if (-not $SkipVhd) {
    Step "5a. Virtual NTFS USB (simple demo) -> kit\usb_demo.vhd  [UAC prompt]"
    if (Test-Path kit\usb_demo.vhd) { Remove-Item kit\usb_demo.vhd }
    # two clips from the realcams set stand in for CCTV exports; the second is deleted
    $a = (Resolve-Path kit\realcams\CAM02_CORRIDOR_20261004_1230.mp4).Path
    $b = (Resolve-Path kit\realcams\CAM01_LOBBY_20261004_1230.mp4).Path
    Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$root\pramanshala\make_usb_vhd.ps1`" -Root `"$root`" -ClipA `"$a`" -ClipB `"$b`""
    Get-Content kit\usb_demo.log | Select-String "deleted_utc|OK|ERROR"

    Step "5b. Virtual NTFS USB for the scenario -> kit\scenario\images\USB_shiftB.vhd  [UAC prompt]"
    $vhd = "$root\kit\scenario\images\USB_shiftB.vhd"
    if (Test-Path $vhd) { Remove-Item $vhd }
    Start-Process powershell -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$root\pramanshala\make_scenario_usb.ps1`" -Vhd `"$vhd`" -Src `"$root\kit\scenario\usb_files`" -Delete EXPORT_CH01_PARKING_20180307_165030.mp4"
    Get-Content "$vhd.log" | Select-String "copied|deleted_utc|OK|ERROR"
    # record the real deletion time in scenario.json (ground truth for the deletion-time check)
    & $vpy -c "import json,re; p='kit/scenario/scenario.json'; m=json.load(open(p)); log=open('kit/scenario/images/USB_shiftB.vhd.log',encoding='utf-8',errors='replace').read(); d=re.findall(r'deleted_utc=(\S+) (\S+)', log); m['evidence']['USB']['image']='USB_shiftB.vhd'; m['evidence']['USB']['deleted_utc']={n:t for t,n in d}; json.dump(m, open(p,'w'), indent=2); print('deletion times recorded:', m['evidence']['USB']['deleted_utc'])"
}
Write-Host "`nData built. Note: freshly built data has new hashes (NTFS images record new timestamps; new scenario" -ForegroundColor Green
Write-Host "images are byte-reproducible across rebuilds but differ from the published release, which was built before the" -ForegroundColor Green
Write-Host "encoder fix). Start the workbench: scripts\run_demo.ps1" -ForegroundColor Green
