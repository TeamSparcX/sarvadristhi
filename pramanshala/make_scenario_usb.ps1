# Builds the scenario's USB stick: a 128 MB NTFS virtual disk holding the files in -Src, with
# the change journal enabled, then deletes the files named in -Delete (comma-separated).
# Windows only; run elevated (diskpart and fsutil need Administrator). Touches no physical drive.
param([Parameter(Mandatory)][string]$Vhd, [Parameter(Mandatory)][string]$Src, [string]$Delete = "",
      [string]$Log = "$Vhd.log")
$ErrorActionPreference = "Stop"
Start-Transcript -Path $Log -Force | Out-Null
try {
    if (Test-Path $Vhd) { throw "$Vhd exists; delete it first" }
    $letter = [char[]](90..70) | Where-Object { -not (Test-Path "$($_):\") } | Select-Object -First 1
    $dp = Join-Path $env:TEMP "dvrf_scenario_vhd.txt"
    @"
create vdisk file="$Vhd" maximum=128 type=fixed
select vdisk file="$Vhd"
attach vdisk
create partition primary
format fs=ntfs quick label=SHIFT_B_USB
assign letter=$letter
"@ | Set-Content -Encoding ascii $dp
    diskpart /s $dp
    Start-Sleep 2
    fsutil usn createjournal m=8388608 a=1048576 "$($letter):"
    Get-ChildItem $Src -File | Sort-Object Name | ForEach-Object {
        Copy-Item $_.FullName "$($letter):\$($_.Name)"; "copied $($_.Name)"; Start-Sleep -Milliseconds 700 }
    Start-Sleep 3
    foreach ($n in ($Delete -split ',' | Where-Object { $_ })) {
        Remove-Item "$($letter):\$n" -Force
        "deleted_utc=$((Get-Date).ToUniversalTime().ToString('o')) $n"
    }
    Start-Sleep 2
    @"
select vdisk file="$Vhd"
detach vdisk
"@ | Set-Content -Encoding ascii $dp
    diskpart /s $dp
    Remove-Item $dp
    "OK"
} catch { "ERROR: $_" } finally { Stop-Transcript | Out-Null }
