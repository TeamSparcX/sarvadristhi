"""Two CCTV cameras with overlapping views cut from real surveillance footage.

Source: OpenCV sample `vtest.avi` (PETS pedestrian footage, 768x576, 10 fps). CAM-01 sees the
left 512 px, CAM-02 the right 512 px starting 10 s later, so people walking left-to-right are
handed over from one camera to the other: real people, a known geometry, a known time offset.
Each camera gets a burned-in label and clock like a DVR overlay.

    python -m evidence_builder.realcams kit/real/vtest.avi kit/real/"""
import subprocess
import sys
from pathlib import Path

from .footage import _clock, font_path

START = 1791117000          # 2026-10-04 12:30:00 UTC
CAMS = [  # (file, label, crop_x, start_s, duration_s)
    ("CAM01_LOBBY_20261004_1230.mp4", "CAM-01 LOBBY", 0, 0, 60),
    ("CAM02_CORRIDOR_20261004_1230.mp4", "CAM-02 CORRIDOR", 256, 10, 69),
]


def build(src: Path, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    font = font_path().replace("\\", "/").replace(":", r"\:")
    made = []
    for name, label, x, t0, dur in CAMS:
        dst = out / name
        graph = (f"[0:v]crop=512:576:{x}:0,drawtext=fontfile='{font}':text='{label}':x=12:y=10:fontsize=22"
                 f":fontcolor=white:box=1:boxcolor=black@0.85:boxborderw=4," + _clock(font, START + t0) + "[v]")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t0), "-t", str(dur),
                        "-i", str(src), "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-threads", "1", "-preset", "fast",
                        "-crf", "20", "-pix_fmt", "yuv420p", "-g", "10", "-movflags", "+faststart", str(dst)], check=True)
        made.append(dst)
    return made


if __name__ == "__main__":
    for p in build(Path(sys.argv[1]), Path(sys.argv[2])):
        print(p)
