"""Honeywell-layout evidence: real video in the record format published by Yoon & Hwang,
DFRWS USA 2026 (20-byte Custom Header per frame, little-endian; 20 zero bytes end each channel
block; dummy fill to the rounded length).

Modelled: the record header and the per-channel blocks in the video data region.
Not modelled: GPT, the Partition 1 Header, Video Block List, Video Channel List and Record State
areas, and the video region's real start at 0x80000000 (placed at 1 MiB to keep test images small).
Recovery must therefore work from the records alone, as it must on a formatted disk.

    python -m pramanshala.honeywell OUT_DIR [--seconds 60] [--codec h264|hevc]"""
import argparse
import struct
from datetime import datetime
from pathlib import Path

from .build import DEFAULT_CAMERAS, MIB, RESUMED, UTC, Clip, _interleaved_gops, _status, _summary, _write_gt
from .footage import require_ffmpeg

HDR, BLOCK_ALIGN, DUMMY = 20, 4096, 0xEE


def record(au: bytes, keyframe: bool, width: int, height: int, t_ms: int) -> bytes:
    nal = au
    if nal.startswith(b"\x00\x00\x00\x01\x09") or nal.startswith(b"\x00\x00\x00\x01\x46\x01"):
        k = nal.find(b"\x00\x00\x01", 4)          # drop the access-unit delimiter
        nal = nal[k - 1:] if k > 0 and nal[k - 1] == 0 else nal[k:]
    return bytes([0x82 if keyframe else 0x02]) + b"\x80\x01\x00" + struct.pack("<HHIQ", width, height, len(nal),
                                                                           t_ms * 1000) + nal


def build_honeywell(clips, resumed, out: Path, gt_rows: list, variant: str) -> dict:
    header = MIB
    data = bytearray()
    placed = []
    for c, g in _interleaved_gops(clips):                # one block per GOP and channel, as a recorder flushes
        for i in g:
            r = record(c.aus[i].data, c.aus[i].keyframe, c.w, c.h, c.t_ms[i])
            placed.append((c, i, header + len(data), len(r)))
            data += r
        data += b"\x00" * HDR                            # End of Channel Data
        pad = (-len(data)) % BLOCK_ALIGN
        data += bytes([DUMMY]) * pad
    size = header + ((len(data) + 8 * MIB) // MIB + 1) * MIB
    damage, resumed_placed = [], []
    if variant == "overwritten":
        a = header + int(len(data) * 0.40)
        b = a + int(len(data) * 0.25)
        damage.append((a, b))
        new = bytearray()
        for i in range(len(resumed.aus)):
            r = record(resumed.aus[i].data, resumed.aus[i].keyframe, resumed.w, resumed.h, resumed.t_ms[i])
            if len(new) + len(r) > b - a:
                new += r[:b - a - len(new)]            # torn record at the overwrite boundary
                break
            resumed_placed.append((resumed, i, a + len(new), len(r)))
            new += r
        data[a - header:b - header] = new.ljust(b - a, b"\x00")
    with out.open("wb") as f:
        f.truncate(size)
        f.seek(header)
        f.write(data)
    for c, i, off, ln in placed:
        gt_rows.append([out.name, c.cam.channel, i, c.t_ms[i], c.overlay_ms(i), int(c.aus[i].keyframe), off, ln,
                        _status(off, ln, damage), "original"])
    for c, i, off, ln in resumed_placed:
        gt_rows.append([out.name, c.cam.channel, i, c.t_ms[i], c.overlay_ms(i), int(c.aus[i].keyframe), off, ln,
                        "intact", "post-format recording"])
    return {"data_region": [header, header + len(data)], "overwritten_ranges": damage,
            "filesystem_metadata": "records only (partition header and lists not modelled)"}


def build(out_dir: Path, seconds: int = 60, fps: int = 10, width: int = 640, height: int = 360,
          start: str = "2026-09-28T21:14:00", codec: str = "h264") -> dict:
    require_ffmpeg()
    out_dir = Path(out_dir)
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    (out_dir / "images").mkdir(exist_ok=True)
    (out_dir / "ground_truth").mkdir(exist_ok=True)
    start_s = int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp())
    resumed_start = start_s + seconds + 690
    tag = "hw265" if codec == "hevc" else "hw"
    clips = [Clip(c, start_s, seconds, fps, width, height, work, f"{tag}_ch{c.channel:02d}", codec=codec)
             for c in DEFAULT_CAMERAS]
    resumed = Clip(RESUMED, resumed_start, max(60, seconds * 2 // 3), fps, width, height, work, f"{tag}_resumed",
                   codec=codec)
    images = {}
    suffix = "_h265" if codec == "hevc" else ""
    for variant in ("intact", "overwritten"):
        rows = []
        p = out_dir / "images" / f"honeywell{suffix}_{variant}.img"
        print(f"Writing {p.name}")
        info = build_honeywell(clips, resumed, p, rows, variant)
        _write_gt(out_dir / "ground_truth" / f"{p.stem}.csv", rows, hik=False)
        images[p.name] = {"family": "honeywell", "codec": codec, "variant": variant, **info, **_summary(rows)}
    return images


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m pramanshala.honeywell")
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--codec", choices=["h264", "hevc"], default="h264")
    a = ap.parse_args()
    for name, info in build(a.out, seconds=a.seconds, codec=a.codec).items():
        print(name, info["variant"], info["codec"], info["frames_total"], "frames")
