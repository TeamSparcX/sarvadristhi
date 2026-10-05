# Sarvadristhi user manual

## Install

**Linux** (Ubuntu 22.04+):
```bash
sudo apt install ffmpeg tesseract-ocr ewf-tools
pip install -e ".[pdf]"          # from the project folder; add ,test for the test suite
```
**Windows 11 (native)**:

1. Install Python 3.12, Git, the full FFmpeg build, and Tesseract with English data. For
   this project, FFmpeg must expose `libx264`; add FFmpeg and Tesseract to `PATH`, then open
   a fresh PowerShell and check `ffmpeg -hide_banner -encoders` and `tesseract --version`.
2. Install DejaVu Sans Mono for the current user. The test evidence uses this font for its
   OCR labels and clock overlays.
3. From the project directory, create and populate a virtual environment:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -e ".[pdf,test]"
   ```

4. For PDF reports, install MSYS2 with its default options, open the MSYS2 UCRT64 shell,
   and run `pacman -S mingw-w64-ucrt-x86_64-pango`. In PowerShell, set
   `$env:WEASYPRINT_DLL_DIRECTORIES = 'C:\msys64\ucrt64\bin'` and check
   `.\.venv\Scripts\python.exe -m weasyprint --info`. WeasyPrint's HTML report remains
   available if PDF rendering cannot load its native libraries.
5. E01 support is optional. Try `.\.venv\Scripts\python.exe -m pip install libewf-python`;
   Windows wheels may not be available. In that case, create E01 images with FTK Imager;
   Sarvadristhi can read E01 when `pyewf` is available.

These Windows instructions describe the supported setup path; the Windows validation
results depend on the machine's installed tools. Attach recorder disks through a hardware
write-blocker. Opening `\\.\PhysicalDriveN` requires an Administrator terminal.

Check the install: `python -m pytest -q` (about 3 minutes; builds its own test evidence).

## The UI

`python -m sarvadristhi ui --cases ./cases` opens the browser on `http://127.0.0.1:8765`. The server
listens only on this computer.

- **New analysis**: give the evidence path (image, `.E01`, export file or block device), a
  case ID and your name. *Universal only* ignores vendor parsers (use it for unknown
  recorders or to cross-check). *AI triage* runs optional detectors.
- **Jobs**: progress log while analysis runs.
- **Case page**: findings, timeline, every recovered clip with a player and its SHA-256,
  links to the PDF/HTML report, and *verify* (re-checks the custody ledger and clip hashes).

## Command line

| Task | Command |
| --- | --- |
| Identify format | `python -m sarvadristhi detect disk.img` |
| Image a disk (E01) | `python -m sarvadristhi acquire /dev/sdX case1/evidence --case case1 --format e01` |
| Image a disk (raw) | `python -m sarvadristhi acquire /dev/sdX case1/evidence.raw --case case1` |
| Full analysis | `python -m sarvadristhi analyze case1/evidence.E01 --case case1 --case-id C-1 --examiner "Name"` |
| Unknown recorder | add `--mode universal` |
| Faster run | add `--no-analytics`, `--no-overlay`, `--no-pdf` |
| AI triage | add `--ai` (uses RF-DETR / YuNet / Ollama if installed, else OpenCV fallbacks) |
| Live pull | `python -m sarvadristhi live --host 192.168.1.64 --user admin --password ... --channels 1,2 --start "2026-09-28 21:00:00" --end "2026-09-28 22:00:00" --case case1` |
| Check custody | `python -m sarvadristhi verify-custody case1` |
| Score on test evidence | `python -m pramanshala kit/` then `python -m sarvadristhi validate kit/ --universal --out results/` |

## What a case folder contains

| File | Content |
| --- | --- |
| `report.pdf`, `report.html` | Court report with s.63(4) data sheet and custody annex |
| `case.json` | Everything in the report, machine-readable |
| `frames.csv` | Every recovered frame: offset, length, camera, time, time source, evidence level |
| `clips/*.mp4`, `clips/*.h264` | Playable copy and complete raw stream per clip |
| `custody.jsonl` | Hash-chained log of every action |
| `work/evidence.raw` | Verified raw copy when the input was E01 |

## Reading the report

- **Evidence level** of each clip: *index-confirmed* (in the recorder's live index), *carved
  + timestamp* (recovered with a recorder-written timestamp), *content-only* (camera/time from
  the picture, or unknown).
- **Time source**: `index`, `container` or `ofni` are recorder-written; `overlay` was read
  from the burned-in clock by OCR; `none` means unknown.
- **Clock drift**: overlay clock minus recorder clock per camera. A *clock change* finding
  means the difference jumped during recording; the report gives the interval.
- **Sensor cluster A/B/...**: cameras separated by sensor-noise fingerprint when footage has
  no overlay. Cluster letters are arbitrary; map them to physical cameras by viewing the scene.

## Troubleshooting

| Symptom | Cause and action |
| --- | --- |
| Family `unknown` with frames recovered | Recorder format has no native plugin; results come from the universal path |
| No frames at all | Disk may be encrypted, or the image is of the wrong partition; run `detect` |
| Clock drift says "insufficient overlay readings" | Overlay hard to read; check clips manually |
| Clip shows "no keyframe" | Remnant of overwritten footage; raw `.h264` keeps it but it cannot play alone |
| PDF missing | WeasyPrint not installed; the HTML report is complete |
