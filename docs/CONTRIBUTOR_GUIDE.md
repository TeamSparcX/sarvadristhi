# Contributor guide: download, run, and change Sarvadristhi

Welcome to **ProjectR** (Sarvadristhi, our SIH26150 DVR/NVR forensic workbench). By the end of this guide you will
have the code and all demo data on your Windows PC, the workbench running in your browser, and a working
way to make and submit changes.

**Time:** 30–60 minutes, mostly downloads. **Downloads:** about 10 GB in total (tools ~1 GB, PyTorch ~2.5 GB,
demo data 3.9 GB, AI model 3.3 GB). **Disk:** 15 GB free. **System:** Windows 10 or 11, 64-bit. An NVIDIA GPU
makes the AI layers faster but is not required.

## What you are downloading

| Part | Where it lives | Size |
|---|---|---|
| Code, docs, scripts, tests | The GitHub repository `aditya36900/ProjectR` | about 3 MB |
| Demo data: real CCTV videos, poisoned DVR and USB images, models, a finished demo case | GitHub **Release** `v1.0-data` (five zip files) | 3.9 GB |

The data is kept out of the repository on purpose: several files are larger than GitHub's 100 MB
per-file limit. The setup script downloads it for you.

## Step 0 — Accept the invitation

The repository is **private**. You were added as a collaborator, so GitHub sent you an invitation.

1. Open the invitation email from GitHub, or go to https://github.com/aditya36900/ProjectR while logged in.
2. Click **Accept invitation**.

If you see "404 – page not found", you are not logged in with the invited account or have not accepted yet.

## Step 1 — Install Git and GitHub CLI, then log in

Open **PowerShell** (Start menu → type "PowerShell" → open it) and run:

```powershell
winget install --id Git.Git --exact
winget install --id GitHub.cli --exact
```

**Close PowerShell and open a new window** (so it finds the new programs), then log in:

```powershell
gh auth login
```

Answer the prompts: **GitHub.com** → **HTTPS** → **Yes** (authenticate Git) → **Login with a web browser**.
Copy the one-time code it shows, press Enter, paste the code in the browser, and approve.

Check: `gh auth status` should say `Logged in to github.com account <you>`.

> `winget` not found? Install "App Installer" from the Microsoft Store (Windows 10), then retry.

## Step 2 — Download the code

Use a short folder path. Avoid folders synced by OneDrive (Desktop and Documents often are), because
syncing several gigabytes of evidence images causes trouble.

```powershell
mkdir C:\Work -Force; cd C:\Work
gh repo clone aditya36900/ProjectR
cd ProjectR
```

You now have `C:\Work\ProjectR` with folders `sarvadristhi\` (the tool), `pramanshala\` (test-evidence builder),
`tests\`, `docs\` and `scripts\`.

## Step 3 — Run the setup (once)

From `C:\Work\ProjectR`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

It works through six steps and prints a heading for each. **Approve the UAC (Administrator) prompts**;
Tesseract and MSYS2 need them.

| Step | What happens | You'll see |
|---|---|---|
| 1 | Installs Python 3.12, Git, FFmpeg, Tesseract OCR, MSYS2, Ollama, GitHub CLI with winget (skips anything already installed) | `ok  <package>` or `installing <package> ...` |
| 2 | Puts Tesseract on your PATH; installs Pango (needed for PDF reports) | pacman output |
| 3 | Installs the DejaVu Sans Mono font for your user | `ok  DejaVu Sans Mono` |
| 4 | Creates `.venv` and installs Sarvadristhi, PyTorch (GPU build if you have an NVIDIA card), RF-DETR, The Sleuth Kit bindings | `python ok, CUDA: True` (or `False` without a GPU) |
| 5 | Downloads the five data zips from release `v1.0-data` into `dist\`, unpacks them, and checks every file | `888 files verified, 0 missing, 0 mismatched` |
| 6 | Downloads the local vision-language model into Ollama | `pulling ... success` |

When it finishes: `Setup complete.` **Open a new PowerShell window** before continuing.

**If step 1 asks to restart the terminal** (tools not on PATH yet), close PowerShell, open a new one, `cd C:\Work\ProjectR`,
and run the setup again; it skips what is done.

**Options** (add to the command): `-SkipInstall` (tools already installed), `-SkipData` (data already
present), `-SkipOllama` (no AI model yet).

**Downloading the data by hand** (if step 5 fails):

```powershell
gh release download v1.0-data -R aditya36900/ProjectR -D dist
.venv\Scripts\python.exe scripts\data_tool.py unpack dist
.venv\Scripts\python.exe scripts\data_tool.py verify
```

After unpacking you will have `kit\` (videos and evidence images), `models\` and `cases\OP-2013\`. You can
delete `dist\` afterwards to free 3.9 GB.

## Step 4 — Run the workbench

```powershell
cd C:\Work\ProjectR
powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1
```

Your browser opens **http://127.0.0.1:8765**. The server runs only on your machine; nothing is uploaded.
Leave the PowerShell window open while you use it (Ctrl+C stops it).

**See finished results first:** click **Cases** → **OP-2013**. This is a complete run of our demo:
real CCTV footage on a deliberately poisoned Hikvision DVR, Dahua DVR and USB stick, with all 8 planted
manipulations caught.

**Run the demo yourself:** **+ New analysis** → click **OPERATION DEMO** (the orange card) → type your
name as examiner → **Start analysis**. Expect about 10 minutes with a GPU, longer without one. The stage
tracker and the live log show progress; open the case when it says done.

**What each tab shows:**

| Tab | What to look at |
|---|---|
| Command | Flag board of SIH26150 requirements, planted-vs-caught scorecard, evidence cards, verified AI summary, AI accuracy against human labels |
| Timeline & video | Every camera on one recorder clock; press Play for the synchronised video wall with AI boxes; click the timeline to jump |
| Disk & hex | Where each kind of data sits on each disk image; click a cell to read the original bytes |
| Provenance | USB exports matched frame-by-frame to the DVR: "only surviving copy" and "6.0 s removed" |
| Time | Camera clocks against the recorder clock (drift and a 15-minute clock change) |
| AI reasoning | The four AI layers: motion, detections, the VLM's exact prompts and answers, cross-camera decisions |
| Review & report | Accept or reject AI leads; regenerate the PDF/HTML report |
| Custody | The hash-chained chain-of-custody ledger |

Click any clip, finding or lead and the **Proof inspector** on the right shows how it was derived, from
the source bytes to the report.

## Step 5 — Check your install

```powershell
cd C:\Work\ProjectR
.venv\Scripts\python.exe -m pytest -q
```

Expected after about 6 minutes: **`37 passed, 1 skipped`** (the skipped test needs Linux-only E01 tools).
If pytest reports `PermissionError` on `AppData\Local\Temp\pytest-of-<you>`, add `--basetemp C:\Work\pytest-tmp`.
Anything else means your setup differs; see Troubleshooting.

## Without GitHub (zip hand-over)

If you received `ProjectR_full.zip` instead: unzip it to `C:\Work`, read `START_HERE.txt`, run
`scripts\setup_windows.ps1` (it finds the data already present and only verifies it), then
`scripts\run_demo.ps1`. Pull later code changes with Git only if you also clone the repository.

## Making changes

### The rules
1. **Never commit data.** Videos, disk images (`.img`, `.vhd`, `.E01`), `kit\`, `cases\`, `models\`
   and `dist\` are in `.gitignore`; keep it that way. Data changes go to the release (see below).
2. **Never weaken a test or a threshold to make it pass.** If a test fails, find out why; if you think a
   result is a real bug, raise it with the team.
3. **Run the test suite before every push** (`pytest -q` → 37 passed, 1 skipped).
4. **Keep claims honest.** Anything synthetic is labelled synthetic; AI output stays a lead until an
   examiner accepts it.
5. Work on a branch and open a pull request; do not push straight to `main`.

### The workflow

```powershell
cd C:\Work\ProjectR
git checkout main; git pull                    # start from the latest code
git checkout -b feature/h265-carver            # one branch per piece of work
# ... edit, then:
.venv\Scripts\python.exe -m pytest -q          # must stay green (37 passed, 1 skipped)
git add -A; git status                         # check: no data files listed
git commit -m "Carve H.265 access units in the universal sweep"
git push -u origin feature/h265-carver
gh pr create --fill                            # opens a pull request for review
```

If `git status` lists `.img`, `.vhd`, `.avi` or anything under `kit\` or `cases\`, stop and remove it from
the commit (`git restore --staged <file>`).

### Where things live

| Folder / file | What it is |
|---|---|
| `sarvadristhi\source.py`, `acquire.py`, `ewf.py` | Read-only evidence access, imaging with MD5/SHA-256, E01 |
| `sarvadristhi\detect.py`, `plugins\` | Format identification; Hikvision, Dahua and generic (any brand) plugins |
| `sarvadristhi\formats\` | DHAV, MPEG-PS and H.264 parsing/carving |
| `sarvadristhi\fsrecover.py` | USB/SD recovery with The Sleuth Kit; NTFS change journal (deletion times) |
| `sarvadristhi\media.py`, `multicase.py`, `provenance.py` | Video-file cases, multi-item cases, export-vs-recorder matching |
| `sarvadristhi\ocr.py`, `prnu.py`, `clockdrift.py` | Camera/time from overlays, sensor fingerprints, clock checks |
| `sarvadristhi\ai\` | AI layers L1–L4, activity rules, scoring against labels |
| `sarvadristhi\report\` | HTML/PDF report template (BSA s.63(4) data sheet) |
| `sarvadristhi\ui.py`, `ui_static\app.html` | The workbench server and single-page UI (D3 bundled for offline use) |
| `sarvadristhi\jobrun.py` | Runs each analysis as its own process (the UI stays responsive) |
| `pramanshala\` | Builds test evidence: synthetic kit, MEVA scenario, virtual NTFS USB disks |
| `scripts\` | Setup, demo launcher, data rebuild, data packaging/verification, zip bundle |
| `tests\test_dvrf.py` | The test suite |

Background reading: `docs\EXECUTION_REPORT.md` (what is done and its limits), `docs\ARCHITECTURE.md`,
`docs\RUN_A_TO_Z.md` (full run guide, including a real USB stick end to end).

### Useful commands

```powershell
.venv\Scripts\python.exe -m sarvadristhi analyze kit\images\hikvision_overwritten.img --case cases\T-1 --case-id T-1 --examiner "Me"
.venv\Scripts\python.exe -m sarvadristhi verify-custody cases\T-1
.venv\Scripts\python.exe -m pramanshala C:\Temp\kit             # rebuild synthetic evidence (byte-identical every time)
.venv\Scripts\python.exe scripts\data_tool.py verify          # check demo data against the manifest
powershell -ExecutionPolicy Bypass -File scripts\build_data.ps1   # rebuild ALL data, incl. virtual USB disks (UAC prompts)
```

### Changing the demo data (maintainers)

1. Rebuild or edit data under `kit\` / `cases\`.
2. `.venv\Scripts\python.exe scripts\data_tool.py package` → new zips in `dist\` and a new `scripts\DATA_MANIFEST.json`.
3. Upload as a **new** release (for example `v1.1-data`):
   `gh release create v1.1-data dist\*.zip -R aditya36900/ProjectR --title "Demo data v1.1" --notes "what changed"`
4. Commit the new manifest and update the release name in `scripts\setup_windows.ps1` (`$Release`) and in the docs.

## Troubleshooting

| Problem | What to do |
|---|---|
| `gh repo clone` says repository not found | Accept the invitation (Step 0); check `gh auth status` shows the invited account |
| `ffmpeg` / `tesseract` / `python` not recognised | Open a new PowerShell window; if still missing, re-run the setup |
| A UAC prompt was cancelled | Re-run the setup and approve it |
| `data_tool verify` reports MISSING | The data download did not finish: run the manual download commands in Step 3 |
| `data_tool verify` reports MISMATCH | A file is corrupted or was rebuilt locally: re-download that zip and unpack again |
| PDF report missing, HTML present | Pango not installed: re-run setup step 2, open a new window |
| AI layers say "Ollama not reachable" | `run_demo.ps1` starts Ollama; also run `ollama pull qwen3-vl:4b` |
| Analysis is very slow | No NVIDIA GPU (`CUDA: False`): expected. Untick "Run AI layers" for a fast run |
| Videos are black in the browser | Press Ctrl+F5 and keep the workbench tab in the foreground |
| "Case already exists" | Use a new case ID; existing evidence folders are never overwritten |
| Port 8765 in use | `powershell -ExecutionPolicy Bypass -File scripts\run_demo.ps1 -Port 8780` |
| Tests fail | Run `pytest -q -x` to see the first failure, then ask the team; do not change thresholds |
