"""Package, verify and unpack the sarvadristhi demo data (footage, disk images, models, demo case).

The data is too large for git (several files exceed GitHub's 100 MB limit), so it travels as
zip files attached to a GitHub Release, with scripts/DATA_MANIFEST.json (in git) recording the
SHA-256 of every file. Anyone can check that their copy is bit-for-bit what we tested.

    python scripts/data_tool.py package     # maintainers: build dist/*.zip + DATA_MANIFEST.json
    python scripts/data_tool.py verify      # anyone: check every data file against the manifest
    python scripts/data_tool.py unpack DIR  # unzip the downloaded archives into the project"""
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "DATA_MANIFEST.json"
RFDETR = Path.home() / ".roboflow" / "models" / "rf-detr-base.pth"
# archive -> list of (path relative to the project, include rule)
ARCHIVES = {
    "sarvadristhi-data-meva.zip": ["kit/meva"],
    "sarvadristhi-data-scenario.zip": ["kit/scenario/images", "kit/scenario/usb_files", "kit/scenario/ground_truth",
                               "kit/scenario/scenario.json"],
    "sarvadristhi-data-synthetic.zip": ["kit/images", "kit/exports", "kit/ground_truth", "kit/manifest.json",
                                "kit/SHA256SUMS", "kit/real", "kit/realcams", "kit/usb_demo.vhd"],
    "sarvadristhi-models.zip": ["models", "@rfdetr"],
    "sarvadristhi-demo-case-OP-2013.zip": ["cases/OP-2013"],
}


def _files(rel: str):
    if rel == "@rfdetr":
        if RFDETR.exists():
            yield RFDETR, "models/rf-detr-base.pth"
        return
    p = ROOT / rel
    if p.is_file():
        yield p, rel
    elif p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file() and "_work" not in f.parts and "__pycache__" not in f.parts:
                yield f, f.relative_to(ROOT).as_posix()


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def package(out="dist"):
    out = ROOT / out
    out.mkdir(exist_ok=True)
    manifest = {"note": "SHA-256 of every demo data file. Verify with: python scripts/data_tool.py verify",
                "archives": {}}
    for name, rules in ARCHIVES.items():
        entries = {}
        with zipfile.ZipFile(out / name, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for rule in rules:
                for f, rel in _files(rule):
                    z.write(f, rel)
                    entries[rel] = {"bytes": f.stat().st_size, "sha256": _sha(f)}
        manifest["archives"][name] = {"bytes": (out / name).stat().st_size, "files": entries}
        print(f"{name}: {len(entries)} files, {(out / name).stat().st_size / 1e6:.0f} MB", flush=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1))
    print(f"manifest: {MANIFEST}")


def verify() -> int:
    m = json.loads(MANIFEST.read_text())
    ok = missing = bad = 0
    for name, a in m["archives"].items():
        for rel, e in a["files"].items():
            p = ROOT / rel
            if rel == "models/rf-detr-base.pth" and not p.exists():
                p = RFDETR
            if not p.exists():
                missing += 1
                print(f"MISSING   {rel}  (from {name})")
            elif p.stat().st_size != e["bytes"] or _sha(p) != e["sha256"]:
                bad += 1
                print(f"MISMATCH  {rel}")
            else:
                ok += 1
    print(f"\n{ok} files verified, {missing} missing, {bad} mismatched")
    return 1 if (missing or bad) else 0


def unpack(src_dir: str):
    src = Path(src_dir)
    for name in ARCHIVES:
        z = src / name
        if not z.exists():
            print(f"skip {name} (not downloaded)")
            continue
        with zipfile.ZipFile(z) as zf:
            zf.extractall(ROOT)
        print(f"unpacked {name}")
    w = ROOT / "models" / "rf-detr-base.pth"
    if w.exists():                       # RF-DETR looks for its weights in ~/.roboflow/models
        RFDETR.parent.mkdir(parents=True, exist_ok=True)
        if not RFDETR.exists():
            shutil.copy2(w, RFDETR)
            print(f"RF-DETR weights -> {RFDETR}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    if cmd == "package":
        package()
    elif cmd == "unpack":
        unpack(sys.argv[2] if len(sys.argv) > 2 else "dist")
    else:
        sys.exit(verify())
