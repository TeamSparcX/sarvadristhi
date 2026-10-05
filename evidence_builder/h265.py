"""H.265 variant of the evidence set: the same cameras, timeline and disk layouts as build.py,
encoded with x265. Most DVRs sold in India record H.265 by default, so every native and
universal path has to be measured on it too.

    python -m evidence_builder.h265 OUT_DIR [--seconds 60]"""
import argparse
from datetime import datetime
from pathlib import Path

from . import hikfs
from .build import (DEFAULT_CAMERAS, MIB, RESUMED, UTC, Clip, _summary, _write_gt, build_dahua, build_hik)
from .footage import require_ffmpeg


def build_h265(out_dir: Path, seconds: int = 60, fps: int = 10, width: int = 640, height: int = 360,
               start: str = "2026-09-28T21:14:00", block_mib: int = 4, pages: int = 1) -> dict:
    require_ffmpeg()
    out_dir = Path(out_dir)
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    img_dir, gt_dir = out_dir / "images", out_dir / "ground_truth"
    img_dir.mkdir(exist_ok=True)
    gt_dir.mkdir(exist_ok=True)
    start_s = int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp())
    end_s = start_s + seconds
    format_time = end_s + 600
    resumed_start = format_time + 90
    print(f"Rendering {len(DEFAULT_CAMERAS)} cameras x {seconds}s in H.265 ...")
    clips = [Clip(c, start_s, seconds, fps, width, height, work, f"hevc_ch{c.channel:02d}", codec="hevc")
             for c in DEFAULT_CAMERAS]
    resumed = Clip(RESUMED, resumed_start, max(60, seconds * 2 // 3), fps, width, height, work, "hevc_resumed",
                   codec="hevc")
    images = {}
    for variant in ("intact", "overwritten"):
        rows = []
        p = img_dir / f"dahua_h265_{variant}.img"
        print(f"Writing {p.name}")
        info = build_dahua(clips, resumed, p, rows, variant)
        _write_gt(gt_dir / f"{p.stem}.csv", rows, hik=False)
        images[p.name] = {"family": "dahua-dhav", "codec": "hevc", "variant": variant, **info, **_summary(rows)}
    layout = hikfs.Layout(block_size=block_mib * MIB, pages=pages)
    for variant in ("intact", "formatted"):
        rows = []
        p = img_dir / f"hikvision_h265_{variant}.img"
        print(f"Writing {p.name}")
        info = build_hik(clips, resumed, layout, p, rows, variant, start_s - 60, end_s, format_time)
        _write_gt(gt_dir / f"{p.stem}.csv", rows, hik=True)
        images[p.name] = {"family": "hikvision", "codec": "hevc", "variant": variant, **info, **_summary(rows)}
    return images


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m evidence_builder.h265")
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=int, default=60)
    a = ap.parse_args()
    for name, info in build_h265(a.out, seconds=a.seconds).items():
        print(name, {k: v for k, v in info.items() if k in ("variant", "codec")})
