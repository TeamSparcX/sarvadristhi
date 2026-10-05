# Running Sarvadristhi from A to Z (Windows 10/11)

This guide takes a fresh Windows PC to a running demo of the Sarvadristhi Forensic Workbench, including the
"Operation demo" case (real CCTV footage on deliberately poisoned evidence). No WSL is needed.

**You need:** Windows 10/11 64-bit, about 15 GB free disk, internet for the first setup, and permission to
approve a few UAC (Administrator) prompts. An NVIDIA GPU is optional: the AI layers run on the CPU
without one, just slower.

## Quick path (3 commands)

Open **PowerShell** in the project folder (Shift + right-click the folder → "Open PowerShell window here"):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1   # once: tools, Python env, data, AI models
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1        # every time: starts the workbench
```

The browser opens at **http://127.0.0.1:8765**. Click **+ New analysis**, pick
**"OPERATION DEMO · one run, every requirement"**, type your name, click **Start analysis**. It takes
about 10 minutes with a GPU. A finished copy of that case (**OP-2013**) is included, so you can explore the
results immediately from the **Cases** page.

If you received the project as a zip that already contains `kit\`, `models\` and `cases\`, the setup skips
the data download and only verifies the files.

## What the setup script does, step by step

If a step fails, you can do it by hand; each step below lists the manual equivalent.

| # | Step | Manual equivalent |
|---|---|---|
| 1 | Installs Python 3.12, Git, FFmpeg (full build with libx264), Tesseract OCR, MSYS2, Ollama and GitHub CLI with **winget**. Tesseract and MSYS2 show a UAC prompt. | `winget install --id Python.Python.3.12 --scope user` and the same for `Git.Git`, `Gyan.FFmpeg`, `UB-Mannheim.TesseractOCR`, `MSYS2.MSYS2`, `Ollama.Ollama`, `GitHub.cli` |
| 2 | Adds Tesseract to your user PATH; installs Pango (for PDF reports) in MSYS2; sets `WEASYPRINT_DLL_DIRECTORIES=C:\msys64\ucrt64\bin` | `C:\msys64\usr\bin\bash.exe -lc "pacman -S --needed mingw-w64-ucrt-x86_64-pango"` |
| 3 | Installs the DejaVu Sans Mono font for your user (the test evidence uses it for camera labels and clocks) | download `dejavu-fonts-ttf-2.37.zip` from github.com/dejavu-fonts, right-click `DejaVuSansMono.ttf` → Install |
| 4 | Creates `.venv` and installs Sarvadristhi, PyTorch (CUDA 12.8 build if an NVIDIA GPU is found), RF-DETR, pytsk3, libewf | `py -3.12 -m venv .venv`, then `.venv\Scripts\python.exe -m pip install -e ".[pdf,test]" rfdetr pytsk3` and torch from `https://download.pytorch.org/whl/cu128` |
| 5 | Gets the demo data (see next section) and verifies every file against `scripts\DATA_MANIFEST.json` | `.venv\Scripts\python.exe scripts\data_tool.py verify` |
| 6 | Pulls the local vision-language model into Ollama | `ollama pull qwen3-vl:4b` (3.3 GB) |

Open a **new** PowerShell window after setup so PATH changes take effect.

## The demo data

Several data files are larger than GitHub allows in a repository (100 MB per file), so the data is
attached to the GitHub Release **v1.0-data** as five zip files (3.9 GB total):

| Archive | Contents | Size |
|---|---|---|
| `sarvadristhi-data-meva.zip` | 6 real CCTV clips + human activity labels + licence (`kit\meva\`) | 727 MB |
| `sarvadristhi-data-scenario.zip` | Poisoned-evidence bench: DVR-A (Hikvision), DVR-B (Dahua), USB stick VHD, export files, `scenario.json` (`kit\scenario\`) | 557 MB |
| `sarvadristhi-data-synthetic.zip` | Synthetic test evidence (`kit\images\` …), two-camera real footage (`kit\realcams\`), simple USB demo VHD | 1.5 GB |
| `sarvadristhi-models.zip` | YuNet + SFace face models (`models\`) and RF-DETR weights (copied to `%USERPROFILE%\.roboflow\models`) | 412 MB |
| `sarvadristhi-demo-case-OP-2013.zip` | A finished Operation-demo case (`cases\OP-2013\`) | 706 MB |

**Download options:**
- automatic: `setup_windows.ps1` runs `gh release download v1.0-data -R aditya36900/ProjectR` (log in first
  with `gh auth login`; the repository is private, so your GitHub account needs access)
- by hand: download the zips from the Releases page into `.\dist\`, then run
  `.venv\Scripts\python.exe scripts\data_tool.py unpack dist`

**Or build it all yourself** (about 10 minutes plus a 726 MB download from the public MEVA bucket):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_data.ps1
```

This builds the synthetic kit, downloads the MEVA clips, cuts the two-camera footage, builds the poisoned
scenario, and creates both virtual NTFS USB sticks. Each USB stick needs **one UAC prompt**, because Windows
only lets Administrators run `diskpart` (create, format, attach and detach a virtual disk) and `fsutil`
(enable the NTFS change journal). Only `.vhd` files are touched, never a physical disk. The scripts that
build them are `evidence_builder\make_usb_vhd.ps1` and `evidence_builder\make_scenario_usb.ps1`. Rebuilt data gets new
hashes, because NTFS records new timestamps, so the manifest check will report mismatches for the
rebuilt files; the analysis results are equivalent.

## Using the workbench

| Screen | What to show |
|---|---|
| **New analysis** | Presets, or paste evidence paths (one per line). One analysis runs at a time; more queue up. |
| **Live job** | The stage tracker is driven by events from the analysis itself; the log shows every step. |
| **Command** | Requirement flag board, poisoned-evidence scorecard (planted vs caught), evidence cards with format coverage, verified AI summary, AI accuracy against human labels |
| **Timeline & video** | All cameras on one recorder clock; synchronised video wall with AI boxes; click the timeline to jump |
| **Disk & hex** | Where each kind of data sits on each image; click a cell to read the original bytes (read-only) |
| **Provenance** | Each USB export matched frame-by-frame to recorder footage: only-surviving-copy and removed seconds |
| **Time** | Camera clocks against the recorder clock: drift and the 15-minute clock change |
| **AI reasoning** | All four layers: motion, detections and tracks, VLM prompts and answers, cross-camera decisions, activity rules |
| **Review & report** | Accept or reject each AI lead; regenerate the report (only accepted leads become findings) |
| **Custody** | The hash-chained ledger, re-verified on open |

The right-hand **Proof inspector** shows, for any selected clip, finding or lead, how it was derived:
source image and hash → byte offset → method → frames → clip hash → time basis → AI → examiner → report.

## Command line (without the UI)

```powershell
.venv\Scripts\python.exe -m sarvadristhi detect kit\images\hikvision_overwritten.img
.venv\Scripts\python.exe -m sarvadristhi analyze kit\images\hikvision_overwritten.img --case cases\C-1 --case-id C-1 --examiner "Name"
.venv\Scripts\python.exe -m sarvadristhi verify-custody cases\C-1
.venv\Scripts\python.exe -m sarvadristhi acquire \\.\PhysicalDrive2 kit\usb.img --case cases\USB   # Administrator terminal; hardware write-blocker for originals
.venv\Scripts\python.exe -m pytest -q                                                       # test suite, about 2 minutes
```

Analysing a physical drive on Windows always means imaging it first (`acquire`), then analysing the image.

## A real USB stick, end to end

1. Use an ordinary USB pen drive (not a USB SSD: TRIM can erase deleted data immediately).
2. Format it **NTFS**. In an Administrator PowerShell, enable the change journal (replace `E:`):
   `fsutil usn createjournal m=8388608 a=1048576 E:`
3. Copy two CCTV clips onto it, then **Shift+Delete** one. Copy nothing else onto it afterwards.
4. Find its disk number on the **New analysis** page under "Attached drives" (for example `\\.\PhysicalDrive2`).
5. In an Administrator PowerShell, from the project folder:
   `.venv\Scripts\python.exe -m sarvadristhi acquire \\.\PhysicalDrive2 kit\usb.img --case cases\USB-LIVE`
6. In the workbench: **New analysis** → paste `kit\usb.img` → **Start analysis**.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ffmpeg` / `tesseract` not found | Open a new PowerShell window (PATH was updated), or re-run the setup |
| PDF report missing (HTML present) | Pango not installed: re-run setup step 2; check `WEASYPRINT_DLL_DIRECTORIES` |
| AI layers say "Ollama not reachable" | Start Ollama (`run_demo.ps1` does it) and run `ollama pull qwen3-vl:4b` |
| AI runs slowly | No CUDA GPU detected: expected on CPU. Untick "Run AI layers" for a fast run |
| Videos stay black in the browser | Press Ctrl+F5; keep the workbench tab in the foreground (Chrome does not load video in background tabs) |
| "Case already exists" | Choose a new case ID; evidence folders are never overwritten |
| Starting from a zip: data verify reports MISSING | The zip did not include `kit\`, `models\` or `cases\`: download the release zips (above) |
