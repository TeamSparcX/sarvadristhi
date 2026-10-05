"""Real-footage demo disks for the newer capabilities: the same MEVA cameras as the scenario
(Kitware / IARPA, CC-BY-4.0), written into an H.265 Hikvision layout (formatted), an H.265 Dahua
layout (overwritten), a Honeywell layout (overwritten), and a relabelled copy of DVR-A. Footage is
real; the disks and every manipulation are modelled, and each image's ground truth is written.

    python -m evidence_builder.realdemos kit/meva kit/scenario kit/realdemo [--seconds 120]"""
import argparse
import json
from pathlib import Path

from . import hikfs
from .build import MIB, _summary, _write_gt, build_dahua, build_hik
from .footage import Camera, require_ffmpeg
from .honeywell import build_honeywell
from .labels import relabel
from .scenario import RealClip, epoch, meva_file

CAMS = [(1, "CAM-01 PARKING", "school.G328", "16-50-01", 0, None),
        (2, "CAM-02 ENTRANCE ROAD", "school.G336", "16-50-06", 47, None),
        (3, "CAM-03 ADMIN DOOR", "admin.G326", "16-50-01", 0, (0.7, -900))]
HW_CAMS = [(1, "CAM-01 BUS LOBBY", "bus.G331", "16-50-00", 0, None),
           (2, "CAM-02 HOSPITAL REAR", "hospital.G301", "16-50-07", 12, None)]


def _clips(src, cams, seconds, work, tag, codec):
    out = []
    for ch, label, cam, hms, drift, chg in cams:
        c = Camera(ch, label, "", drift, 0.0, **({"change_at_frac": chg[0], "changed_drift_s": chg[1]} if chg else {}))
        print(f"  encoding {tag} CH{ch} {label} ({codec}) <- MEVA {cam}", flush=True)
        out.append(RealClip(c, meva_file(src, cam), epoch(hms), 0, seconds, work, f"{tag}_ch{ch:02d}", codec=codec))
    return out


def build(meva: Path, scenario: Path, out: Path, seconds: int = 120) -> dict:
    require_ffmpeg()
    out = Path(out)
    work = out / "_work"
    for d in (work, out / "images", out / "ground_truth"):
        d.mkdir(parents=True, exist_ok=True)
    images = {}
    start = min(epoch(c[3]) for c in CAMS)
    end = start + seconds
    fmt = end + 600
    print("H.265 clips (real footage) ...", flush=True)
    h265 = _clips(meva, CAMS, seconds, work, "rd265", "hevc")
    res265 = RealClip(Camera(1, "CAM-01 PARKING", "", 0, 0.0), meva_file(meva, "school.G328"), fmt + 90, seconds,
                      max(60, seconds // 2), work, "rd265_resumed", codec="hevc")
    p = out / "images" / "hikvision_h265_formatted.img"
    rows = []
    print(f"Writing {p.name}", flush=True)
    info = build_hik(h265, res265, hikfs.Layout(block_size=4 * MIB, pages=1), p, rows, "formatted", start - 60, end, fmt)
    _write_gt(out / "ground_truth" / f"{p.stem}.csv", rows, hik=True)
    images[p.name] = {"family": "hikvision", "codec": "hevc", "variant": "formatted", **info, **_summary(rows)}
    p = out / "images" / "dahua_h265_overwritten.img"
    rows = []
    print(f"Writing {p.name}", flush=True)
    info = build_dahua(h265, res265, p, rows, "overwritten")
    _write_gt(out / "ground_truth" / f"{p.stem}.csv", rows, hik=False)
    images[p.name] = {"family": "dahua-dhav", "codec": "hevc", "variant": "overwritten", **info, **_summary(rows)}
    print("H.264 clips for the Honeywell layout ...", flush=True)
    hw = _clips(meva, HW_CAMS, seconds, work, "rdhw", "h264")
    reshw = RealClip(Camera(1, "CAM-01 BUS LOBBY", "", 0, 0.0), meva_file(meva, "bus.G331"), fmt + 90, seconds,
                     max(60, seconds // 2), work, "rdhw_resumed")
    p = out / "images" / "honeywell_overwritten.img"
    rows = []
    print(f"Writing {p.name}", flush=True)
    info = build_honeywell(hw, reshw, p, rows, "overwritten")
    _write_gt(out / "ground_truth" / f"{p.stem}.csv", rows, hik=False)
    images[p.name] = {"family": "honeywell", "codec": "h264", "variant": "overwritten", **info, **_summary(rows)}
    src_a = Path(scenario) / "images" / "DVR-A_hikvision.img"
    lab = out / "images" / "cpplus_label_on_real_hikvision.img"
    if src_a.exists() and not lab.exists():
        print(f"Writing {lab.name}", flush=True)
        images[lab.name] = {"family": "hikvision", "variant": "relabelled", **relabel(src_a, lab)}
    (out / "realdemo.json").write_text(json.dumps({
        "nature": "REAL footage (MEVA, CC-BY-4.0); recorder disks and manipulations SIMULATED in vendor formats",
        "footage": {"dataset": "MEVA, Kitware / IARPA DIVA", "license": "CC-BY-4.0", "home": "https://mevadata.org"},
        "seconds": seconds, "images": images}, indent=1, default=str), encoding="utf-8")
    return images


if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="python -m evidence_builder.realdemos")
    ap.add_argument("meva", type=Path)
    ap.add_argument("scenario", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--seconds", type=int, default=120)
    a = ap.parse_args()
    for name, info in build(a.meva, a.scenario, a.out, a.seconds).items():
        print(name, info.get("variant"), info.get("codec", ""))
