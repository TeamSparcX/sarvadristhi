# Builds kit\usb_demo.vhd: a 256 MB NTFS "USB stick" holding two CCTV exports, one of them
# deleted after the change journal ($UsnJrnl) was enabled. Windows only; run elevated
# (diskpart and fsutil need Administrator). Touches no physical drive.
param([string]$Root = (Resolve-Path "$PSScriptRoot\..").Path,
      [string]$ClipA, [string]$ClipB)
$ErrorActionPreference = "Stop"
$log = Join-Path $Root "kit\usb_demo.log"
Start-Transcript -Path $log -Force | Out-Null
try {
    $vhd = Join-Path $Root "kit\usb_demo.vhd"
    if (Test-Path $vhd) { throw "$vhd exists; delete it first" }
    $letter = [char[]](90..70) | Where-Object { -not (Test-Path "$($_):\") } | Select-Object -First 1
    $dp = Join-Path $env:TEMP "dvrf_vhd.txt"
    @"
create vdisk file="$vhd" maximum=256 type=fixed
select vdisk file="$vhd"
attach vdisk
create partition primary
format fs=ntfs quick label=EVIDENCE_USB
assign letter=$letter
"@ | Set-Content -Encoding ascii $dp
    diskpart /s $dp
    Start-Sleep 2
    fsutil usn createjournal m=8388608 a=1048576 "$($letter):"
    $keep = "$($letter):\CAM03_CORRIDOR_20260928_2110.mp4"
    $gone = "$($letter):\CAM02_PARKING_20260928_2114.mp4"
    Copy-Item $ClipB $keep
    Copy-Item $ClipA $gone
    Start-Sleep 3
    Remove-Item $gone -Force           # permanent delete: removable/VHD volumes have no Recycle Bin
    "deleted_utc=$((Get-Date).ToUniversalTime().ToString('o'))"
    Start-Sleep 2
    @"
select vdisk file="$vhd"
detach vdisk
"@ | Set-Content -Encoding ascii $dp
    diskpart /s $dp
    Remove-Item $dp
    "OK"
} catch { "ERROR: $_" } finally { Stop-Transcript | Out-Null }
