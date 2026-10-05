"""Build one self-contained zip of the project for hand-over: code, docs, scripts, all demo data
(footage, evidence images, VHDs), models and the finished demo case. Excludes .venv (machine-
specific; scripts/setup_windows.ps1 rebuilds it) and scratch cases.

    python scripts/make_bundle.py [output.zip]"""
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RFDETR = Path.home() / ".roboflow" / "models" / "rf-detr-base.pth"
DATA = ["kit/meva", "kit/scenario/images", "kit/scenario/usb_files", "kit/scenario/ground_truth",
        "kit/scenario/scenario.json", "kit/images", "kit/exports", "kit/ground_truth", "kit/manifest.json",
        "kit/SHA256SUMS", "kit/real", "kit/realcams", "kit/usb_demo.vhd", "models", "cases/OP-2013"]
START = """sarvadristhi - Forensic Workbench
=====================================
Everything is in this folder: code, docs, all videos, evidence images, models and a finished demo case.

1. Unzip to a short path, e.g. C:\\ProjectR  (Windows 10/11, ~15 GB free).
2. Open PowerShell in that folder and run once:
      powershell -ExecutionPolicy Bypass -File scripts\\setup_windows.ps1
   (installs Python, FFmpeg, Tesseract, MSYS2/Pango, Ollama, the Python environment and the AI model;
    approve the UAC prompts; the data is already here, so nothing large is downloaded except tools,
    PyTorch and the 3.3 GB qwen3-vl model)
3. Start the workbench:
      powershell -ExecutionPolicy Bypass -File scripts\\run_demo.ps1
   Browser: http://127.0.0.1:8765  ->  Cases -> OP-2013 (finished demo), or
   + New analysis -> "OPERATION DEMO" preset -> Start (about 10 minutes with a GPU).

Full guide: docs\\RUN_A_TO_Z.md      What was done + limitations: docs\\EXECUTION_REPORT.md
Check the data is intact: .venv\\Scripts\\python.exe scripts\\data_tool.py verify
"""


def main(out: str):
    out = Path(out)
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    n = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        z.writestr("ProjectR/START_HERE.txt", START.replace("\n", "\r\n"))
        for rel in tracked:
            z.write(ROOT / rel, f"ProjectR/{rel}")
            n += 1
        for rel in DATA:
            p = ROOT / rel
            files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file() and "_work" not in f.parts)
            for f in files:
                z.write(f, f"ProjectR/{f.relative_to(ROOT).as_posix()}")
                n += 1
        if RFDETR.exists() and not (ROOT / "models" / "rf-detr-base.pth").exists():
            z.write(RFDETR, "ProjectR/models/rf-detr-base.pth")
            n += 1
    print(f"{out}: {n} files, {out.stat().st_size / 1e9:.2f} GB")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "dist" / "ProjectR_full.zip"))
