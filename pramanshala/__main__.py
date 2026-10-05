import argparse
from pathlib import Path

from .build import build


def main():
    ap = argparse.ArgumentParser(prog="python -m pramanshala",
                                 description="Build DVR/NVR test evidence")
    ap.add_argument("out", type=Path, help="output directory")
    ap.add_argument("--seconds", type=int, default=180, help="recording length per camera")
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--size", default="640x360")
    ap.add_argument("--start", default="2026-09-28T21:14:00", help="true recording start, UTC")
    ap.add_argument("--pages", type=int, default=1, help="HIKBTREE pages (83 data blocks each)")
    ap.add_argument("--block-mib", type=int, default=4, help="Hikvision data block size")
    a = ap.parse_args()
    w, h = map(int, a.size.lower().split("x"))
    m = build(a.out, a.seconds, a.fps, w, h, a.start, a.pages, a.block_mib)
    for name, img in m["images"].items():
        print(f"{name:28} {img['bytes'] / 2**20:7.1f} MiB  frames intact "
              f"{img['frames_intact']}/{img['frames_total']}  sha256 {img['sha256'][:16]}")
    print(f"Manifest: {a.out / 'manifest.json'}")


main()
